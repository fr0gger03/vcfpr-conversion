# SPDX-License-Identifier: Apache-2.0
"""Dell RecoverPoint for VMs (RP4VM 5.3+) adapter.

Exact REST sub-resource paths beyond `consistency-groups` are not published in public
docs — only the plugin server's self-hosted Swagger UI (`https://{plugin-server}/ui`)
has the authoritative shape. Paths below are best-effort based on RP4VM CLI verb naming
(`pause_transfer`, remove-VM-from-CG) and must be confirmed there before production use.
"""

from datetime import datetime, timezone
from typing import Any

import requests

from src.adapters.base import BaseDREngine
from src.cache import get_cached_inventory, set_cached_inventory
from src.config import DEFAULT_HTTP_TIMEOUT_SECONDS, Settings, require
from src.models.manifest import (
    Disk,
    Manifest,
    ManifestMetadata,
    ProtectionGroup,
    SourceEngine,
    VirtualMachine,
)


class RecoverPointAdapter(BaseDREngine):
    """authenticate -> discover_inventory -> export_protection_manifest -> quiesce/cleanup."""

    def __init__(self, settings: Settings, cluster_id: str = "default"):
        self.settings = settings
        self.cluster_id = cluster_id
        self.base_url: str | None = None
        self.session = requests.Session()

    def authenticate(self) -> None:
        creds = require(self.settings, "rp4vm_ip", "rp4vm_user", "rp4vm_password")
        host = creds["rp4vm_ip"]
        # Defaults to https; allows an explicit "http://host:port" override for tests/dev.
        host = host if "://" in host else f"https://{host}"
        self.base_url = f"{host}/api/v2"
        # Best-effort based on CLI verb naming — confirm against
        # https://{plugin-server}/ui Swagger before production use.
        resp = self.session.post(
            f"{self.base_url}/sessions",
            json={"username": creds["rp4vm_user"], "password": creds["rp4vm_password"]},
            verify=self.settings.verify_ssl,
            timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        token = resp.json().get("token") or resp.headers.get("X-RP-SESSION-TOKEN")
        if not token:
            raise RuntimeError("RP4VM authentication succeeded but no session token was returned")
        self.session.headers.update({"X-RP-SESSION-TOKEN": token})

    def discover_inventory(self) -> list[dict[str, Any]]:
        cache_key = f"recoverpoint:{self.cluster_id}"
        cached = get_cached_inventory(cache_key)
        if cached is not None:
            return cached

        if self.base_url is None:
            raise RuntimeError("authenticate() must be called before discover_inventory()")

        # Best-effort based on CLI verb naming — confirm against
        # https://{plugin-server}/ui Swagger before production use.
        groups = self.session.get(
            f"{self.base_url}/consistency-groups", verify=self.settings.verify_ssl, timeout=DEFAULT_HTTP_TIMEOUT_SECONDS
        ).json()
        groups_list = groups.get("consistencyGroups", groups) if isinstance(groups, dict) else groups

        inventory: list[dict[str, Any]] = []
        for group in groups_list:
            group_id = group.get("id") or group.get("consistencyGroupUid")
            # Sub-resource batched detail fetch — best-effort path, confirm via Swagger.
            detail = self.session.get(
                f"{self.base_url}/consistency-groups/{group_id}",
                verify=self.settings.verify_ssl,
                timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
            ).json()
            group_payload = {**group, **detail}
            inventory.append(group_payload)

        set_cached_inventory(cache_key, inventory)
        return inventory

    def export_protection_manifest(self, inventory: list[dict[str, Any]]) -> Manifest:
        protection_groups: list[ProtectionGroup] = []
        virtual_machines: list[VirtualMachine] = []
        disks: list[Disk] = []

        for group in inventory:
            group_id = group.get("id") or group.get("consistencyGroupUid") or ""
            group_name = group.get("name") or group_id
            vm_entries = group.get("vms") or group.get("protectedVms") or []
            vm_ids: list[str] = []

            for vm in vm_entries:
                vm_id = vm.get("id") or vm.get("vmUid") or vm.get("uid") or ""
                vm_ids.append(vm_id)
                virtual_machines.append(
                    VirtualMachine(
                        vm_id=vm_id,
                        name=vm.get("name", "Unknown_VM"),
                        vcenter_moref=vm.get("moref"),
                        tags=vm.get("tags", []),
                    )
                )
                for idx, volume in enumerate(vm.get("volumes") or vm.get("replicaVolumes") or []):
                    disks.append(
                        Disk(
                            disk_id=volume.get("id") or volume.get("volumeUid") or f"{vm_id}-disk-{idx}",
                            vm_id=vm_id,
                            capacity_bytes=int(volume.get("capacityBytes") or volume.get("sizeInBytes") or 0),
                            controller_index=volume.get("controllerIndex", idx),
                            seed_file_path=volume.get("targetDatastorePath"),
                        )
                    )

            protection_groups.append(
                ProtectionGroup(
                    name=group_name,
                    rpo_seconds=int(group.get("rpoSeconds") or group.get("rpo", 0)),
                    boot_priority=group.get("bootPriority", 0),
                    startup_delay_seconds=group.get("startupDelaySeconds", 0),
                    vm_ids=vm_ids,
                )
            )

        return Manifest(
            metadata=ManifestMetadata(
                source_engine=SourceEngine.RECOVERPOINT,
                source_cluster_id=self.cluster_id,
                extraction_timestamp=datetime.now(timezone.utc),
            ),
            protection_groups=protection_groups,
            virtual_machines=virtual_machines,
            disks=disks,
        )

    def quiesce_replication(self, group_id: str) -> None:
        if self.base_url is None:
            raise RuntimeError("authenticate() must be called before quiesce_replication()")
        # REST equivalent of RP4VM CLI `pause_transfer` — best-effort path, confirm via Swagger.
        resp = self.session.put(
            f"{self.base_url}/consistency-groups/{group_id}/pause-transfer",
            verify=self.settings.verify_ssl,
            timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()

    def cleanup_source(self, group_id: str, *, keep_target_disks: bool = True) -> None:
        if self.base_url is None:
            raise RuntimeError("authenticate() must be called before cleanup_source()")
        # REST equivalent of removing/unprotecting a CG copy — best-effort path, confirm via
        # Swagger. `preserveReplicaVolumes` field name is an assumption mirroring the RP4VM
        # UI's "keep target volumes" checkbox; verify exact body field before production use.
        resp = self.session.delete(
            f"{self.base_url}/consistency-groups/{group_id}",
            json={"preserveReplicaVolumes": keep_target_disks},
            verify=self.settings.verify_ssl,
            timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
