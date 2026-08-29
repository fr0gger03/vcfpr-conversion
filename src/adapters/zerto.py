# SPDX-License-Identifier: Apache-2.0
"""Zerto ZVMA adapter: Keycloak auth, VRA/VM/volume discovery, VPG manifest export."""

from datetime import datetime, timezone
from typing import Any

import requests

from src import cache
from src.adapters.base import BaseDREngine
from src.config import Settings, compute_dest_paths, require
from src.models.manifest import (
    Disk,
    Manifest,
    ManifestMetadata,
    ProtectionGroup,
    SourceEngine,
    VirtualMachine,
)


def get_val(obj: Any, key: str, default: Any = None) -> Any:
    """Case-insensitive dict getter tolerating Zerto's inconsistent field casing."""
    if not isinstance(obj, dict):
        return default
    if key in obj:
        return obj[key]
    pascal = key[0].upper() + key[1:] if key else key
    if pascal in obj:
        return obj[pascal]
    camel = key[0].lower() + key[1:] if key else key
    if camel in obj:
        return obj[camel]
    return default


class ZertoAdapter(BaseDREngine):
    """DR engine adapter for a Zerto Virtual Manager Appliance (ZVMA)."""

    TOKEN_PATH = "/auth/realms/zerto/protocol/openid-connect/token"

    def __init__(
        self,
        settings: Settings | None = None,
        cluster_id: str = "default",
        base_url: str | None = None,
    ) -> None:
        self._settings = settings or Settings()
        self.cluster_id = cluster_id
        self._base_url = base_url or f"https://{self._settings.zvm_ip}"
        self._token: str | None = None

    def _headers(self) -> dict[str, str]:
        if not self._token:
            raise RuntimeError("authenticate() must be called before making API calls")
        return {"Authorization": f"Bearer {self._token}"}

    def authenticate(self) -> None:
        creds = require(self._settings, "zvm_client_id", "zvm_client_secret")
        resp = requests.post(
            f"{self._base_url}{self.TOKEN_PATH}",
            data={
                "grant_type": "client_credentials",
                "client_id": creds["zvm_client_id"],
                "client_secret": creds["zvm_client_secret"],
            },
            verify=self._settings.verify_ssl,
        )
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not token:
            raise RuntimeError(f"Zerto authentication failed: {payload or resp.status_code}")
        self._token = token

    def discover_inventory(self) -> list[dict[str, Any]]:
        cache_key = f"zerto:{self.cluster_id}"
        cached = cache.get_cached_inventory(cache_key)
        if cached is not None:
            return cached

        headers = self._headers()
        verify = self._settings.verify_ssl
        vras = requests.get(f"{self._base_url}/v1/vras", headers=headers, verify=verify).json()
        vms = requests.get(f"{self._base_url}/v1/vms", headers=headers, verify=verify).json()
        volumes = requests.get(f"{self._base_url}/v1/volumes", headers=headers, verify=verify).json()

        payload = [{"vras": vras, "vms": vms, "volumes": volumes}]
        cache.set_cached_inventory(cache_key, payload)
        return payload

    def export_protection_manifest(self, inventory: list[dict[str, Any]]) -> Manifest:
        raw = inventory[0] if inventory else {}
        vms_data = raw.get("vms", [])
        volumes_data = raw.get("volumes", [])

        vm_map: dict[str, dict[str, Any]] = {}
        for vm in vms_data:
            vm_id = (
                get_val(vm, "vmIdentifier")
                or get_val(get_val(vm, "link"), "identifier")
                or get_val(vm, "identifier")
            )
            if vm_id:
                vm_map[vm_id] = {
                    "name": get_val(vm, "vmName", "Unknown_VM"),
                    "vpg_name": get_val(vm, "vpgName", "Unknown_VPG"),
                }

        vpg_vm_ids: dict[str, list[str]] = {}
        virtual_machines: list[VirtualMachine] = []
        for vm_id, info in vm_map.items():
            virtual_machines.append(VirtualMachine(vm_id=vm_id, name=info["name"]))
            vpg_vm_ids.setdefault(info["vpg_name"], []).append(vm_id)

        protection_groups = [
            # rpo_seconds defaults to 0: discover_inventory doesn't query /v1/vpgs (out of
            # scope here, matching the legacy script), so per-VPG RPO isn't available yet.
            ProtectionGroup(name=vpg_name, rpo_seconds=0, vm_ids=vm_ids)
            for vpg_name, vm_ids in vpg_vm_ids.items()
        ]

        disks: list[Disk] = []
        for vol in volumes_data:
            if get_val(vol, "volumeType") != "Recovery":
                continue
            prot_vm = get_val(vol, "protectedVm") or get_val(vol, "owningVm") or {}
            vm_id = get_val(prot_vm, "identifier")
            vm_name = vm_map.get(vm_id, {}).get("name") or get_val(prot_vm, "name", "Unknown_VM")

            ds_name = get_val(get_val(vol, "datastore") or {}, "name", "Unknown_DS")
            path = get_val(vol, "path") or {}
            raw_full = get_val(path, "full", "")
            vmdk_file = get_val(path, "fileName", "")
            source_raw_path = raw_full or f"unknown/{vmdk_file}"
            _, _, dest_vmdk_path, _ = compute_dest_paths(ds_name, vm_name, source_raw_path)

            disk_id = (
                get_val(vol, "volumeIdentifier")
                or get_val(get_val(vol, "link"), "identifier")
                or f"{vm_id}:{vmdk_file}"
            )
            disks.append(
                Disk(
                    disk_id=disk_id,
                    vm_id=vm_id or "",
                    # "sizeInBytes"/"controllerIndex": field names unconfirmed against the
                    # live ZVM's Volume resource schema; verify via /swagger/index.html.
                    capacity_bytes=get_val(vol, "sizeInBytes", 0),
                    controller_index=get_val(vol, "controllerIndex", 0),
                    seed_file_path=dest_vmdk_path,
                    # Zerto's replica disk physically lives here until `provision` copies
                    # it to seed_file_path (see VCFProtectionAdapter.copy_and_prepare_seed).
                    source_datastore=ds_name,
                    source_raw_path=source_raw_path,
                )
            )

        return Manifest(
            metadata=ManifestMetadata(
                source_engine=SourceEngine.ZERTO,
                source_cluster_id=self.cluster_id,
                extraction_timestamp=datetime.now(timezone.utc),
            ),
            protection_groups=protection_groups,
            virtual_machines=virtual_machines,
            disks=disks,
        )

    def quiesce_replication(self, group_id: str) -> None:
        requests.post(
            f"{self._base_url}/v1/vpgs/{group_id}/pause",
            headers=self._headers(),
            verify=self._settings.verify_ssl,
        ).raise_for_status()

    def cleanup_source(self, group_id: str, *, keep_target_disks: bool = True) -> None:
        # Body field name is best-effort: Zerto's public docs don't confirm the exact
        # DELETE /v1/vpgs/{id} payload key mirroring the UI's "Keep the recovery disks
        # at the peer site" checkbox. Verify against the live ZVM's /swagger/index.html
        # before relying on this in production.
        body = {"KeepTheRecoveryDisks": keep_target_disks}
        requests.delete(
            f"{self._base_url}/v1/vpgs/{group_id}",
            headers=self._headers(),
            json=body,
            verify=self._settings.verify_ssl,
        ).raise_for_status()
