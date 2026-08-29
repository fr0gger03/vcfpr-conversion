# SPDX-License-Identifier: Apache-2.0
"""VCF Protection & Recovery target-side adapter.

Combines the legacy pyVmomi seed-copy (`02_vcf_seed_copy.py`) and VMDK descriptor
cleanup (`03_vmdk_descriptor_cleanup.py`) logic with the vSphere Replication REST API
Gateway (`/api/rest/vr/v1/...`) so seeded disks get registered as protected VMs
instead of being left as a manual follow-up step.

VCF is always the migration *target*, never a discovery source, so
`export_protection_manifest()` isn't meaningful here -- see its docstring.
"""

import ssl
from typing import Any
from urllib.parse import quote, urlencode

import requests
from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim

from src.adapters.base import BaseDREngine
from src.cache import get_cached_inventory, set_cached_inventory
from src.config import DEFAULT_HTTP_TIMEOUT_SECONDS, Settings, compute_dest_paths, require
from src.models.manifest import Disk, Manifest, ProtectionGroup

VR_API_PREFIX = "/api/rest/vr/v1"

# Descriptor keys that can point at stale I/O filter sidecars after a seed copy
# (Broadcom KB 334555). Ported from 03_vmdk_descriptor_cleanup.py.
_DESCRIPTOR_FILTER_KEYS = ("ddb.iofilters", "ddb.sidecars")


class VCFProtectionAdapter(BaseDREngine):
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self._si = None
        self._datacenter = None
        self._vr_gateway: str | None = None
        self._session_header: str | None = None  # single reused "x-dr-session" value

    # -- BaseDREngine -----------------------------------------------------

    def authenticate(self) -> None:
        """Open the pyVmomi vCenter session and the vSphere Replication REST API
        Gateway session. If `vr_pairing_id` is configured (needed to query the
        *remote* site's datastores/storage policies for cross-site provisioning),
        also call `remote-session` -- per the VR Gateway API this reuses the same
        `x-dr-session` header rather than minting a second token."""
        env = require(self.settings, "vcenter_ip", "vcenter_user", "vcenter_password", "vcenter_datacenter")
        self._vr_gateway = self.settings.vr_gateway_ip or env["vcenter_ip"]

        ssl_context = ssl.create_default_context() if self.settings.verify_ssl else ssl._create_unverified_context()
        self._si = SmartConnect(
            host=env["vcenter_ip"], user=env["vcenter_user"], pwd=env["vcenter_password"], sslContext=ssl_context
        )
        self._datacenter = self._find_datacenter(env["vcenter_datacenter"])

        self._session_header = self._vr_login(env["vcenter_user"], env["vcenter_password"])

        pairing_id = getattr(self.settings, "vr_pairing_id", None)
        if pairing_id:
            self._vr_remote_session(pairing_id, env["vcenter_user"], env["vcenter_password"])

    def discover_inventory(self) -> list[dict[str, Any]]:
        """VCF is the migration target; there's no upstream protection state to
        discover. Returns target datastores visible in vCenter (candidate seed-copy
        destinations for the CLI/UI), cached via src.cache to avoid re-querying."""
        if self._datacenter is None:
            raise RuntimeError("authenticate() must be called before discover_inventory()")

        cache_key = f"vcf:{self._datacenter.name}"
        cached = get_cached_inventory(cache_key)
        if cached is not None:
            return cached

        inventory = [
            {"name": ds.name, "free_space": ds.summary.freeSpace, "capacity": ds.summary.capacity}
            for ds in self._datacenter.datastore
        ]
        set_cached_inventory(cache_key, inventory)
        return inventory

    def export_protection_manifest(self, inventory: list[dict[str, Any]]) -> Manifest:
        """Not applicable: VCF Protection & Recovery is the migration target, so it
        has no source-side protection state to export as a Manifest. Manifests are
        always built by the *source* adapter (Zerto/RecoverPoint) via
        `engine.transformer.build_manifest()`; raising here (rather than silently
        returning an empty Manifest) makes a caller's mistake obvious immediately."""
        raise NotImplementedError(
            "VCFProtectionAdapter is a target-only engine; it has no protection state "
            "to export as a Manifest. Build the Manifest from the source adapter instead."
        )

    def quiesce_replication(self, group_id: str) -> None:
        """Pause an already-configured replication. Sub-resource path assumed as
        `{replications}/{id}/pause` by analogy with `Configure Replication`'s
        `.../replications` collection -- confirm against the VR Gateway's own
        Swagger UI if this 404s in your environment."""
        self._vr_request("POST", f"/replications/{group_id}/pause")

    def cleanup_source(self, group_id: str, *, keep_target_disks: bool = True) -> None:
        """Unconfigure replication for `group_id`. `keep_target_disks=True` (default)
        leaves the already-provisioned target VM/disks in place -- only the
        replication pairing itself is removed."""
        self._vr_request("DELETE", f"/replications/{group_id}", json={"retainDisks": keep_target_disks})

    # -- VCF-specific: seed-based provisioning -----------------------------

    def copy_and_prepare_seed(self, ds_name: str, vm_name: str, source_raw_path: str) -> str:
        """Copy a seed VMDK to its target-side destination (migrated from
        `02_vcf_seed_copy.py`) and scrub any lingering I/O filter descriptor
        references (migrated from `03_vmdk_descriptor_cleanup.py`). Returns the
        `[Datastore] VM_Name/VMDK` destination path -- the value that should end up
        in `Disk.seed_file_path`."""
        dest_vmdk_path = self._copy_seed_disk(ds_name, vm_name, source_raw_path)
        self._scrub_descriptor(ds_name, vm_name, dest_vmdk_path)
        return dest_vmdk_path

    def provision_protection_group(self, group: ProtectionGroup, disks: list[Disk], *, pairing_id: str) -> dict[str, Any]:
        """Register `group`'s already-seeded disks via `Configure Replication`
        (`use_seeds=true`), pointing each disk at the seed VMDK path
        `copy_and_prepare_seed()` already populated -- this is what lets VCF skip a
        full sync and use the pre-copied data instead."""
        body = {
            "name": group.name,
            "rpoSeconds": group.rpo_seconds,
            "useSeeds": True,
            "disks": [
                {"diskId": disk.disk_id, "destinationPath": disk.seed_file_path}
                for disk in disks
                if disk.seed_file_path
            ],
        }
        return self._vr_request("POST", f"/pairings/{pairing_id}/replications", json=body)

    def disconnect(self) -> None:
        if self._si is not None:
            Disconnect(self._si)
            self._si = None

    # -- src.engine.validator.VCenterSession protocol (duck-typed, no inheritance) --

    def _cookie_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({"Cookie": self._si._stub.cookie})
        return session

    def datastore_file_exists(self, datastore: str, path: str) -> bool:
        vm_name, filename = path.split("/", 1)
        url = self._datastore_file_url(datastore, vm_name, filename)
        resp = self._cookie_session().get(url, verify=self.settings.verify_ssl, timeout=DEFAULT_HTTP_TIMEOUT_SECONDS)
        return resp.status_code == 200

    def datastore_free_bytes(self, datastore: str) -> int | None:
        for ds in self._datacenter.datastore:
            if ds.name == datastore:
                return ds.summary.freeSpace
        return None

    def seed_disk_size_bytes(self, datastore: str, path: str) -> int | None:
        vm_name, filename = path.split("/", 1)
        url = self._datastore_file_url(datastore, vm_name, filename)
        resp = self._cookie_session().head(url, verify=self.settings.verify_ssl, timeout=DEFAULT_HTTP_TIMEOUT_SECONDS)
        length = resp.headers.get("Content-Length")
        return int(length) if length is not None else None

    def list_rdm_canonical_names(self) -> set[str]:
        # Not yet implemented: RDM detection requires walking RawDiskMappingVer1BackingInfo
        # across every VM in the datacenter. Returning an empty set means
        # check_rdm_conflicts() always passes rather than raising -- validate() stays
        # usable, but RDM conflicts will not actually be caught until this is filled in.
        return set()

    def disk_canonical_name(self, disk: Disk) -> str | None:
        return None  # paired with the list_rdm_canonical_names() stub above.

    # -- migrated from 02_vcf_seed_copy.py ---------------------------------

    def _copy_seed_disk(self, ds_name: str, vm_name: str, source_raw_path: str) -> str:
        src_path, dest_folder_path, dest_vmdk_path, _filename = compute_dest_paths(ds_name, vm_name, source_raw_path)
        content = self._si.RetrieveContent()
        try:
            content.fileManager.MakeDirectory(
                name=dest_folder_path, datacenter=self._datacenter, createParentDirectories=True
            )
        except Exception as exc:
            if "already exists" not in str(exc).lower():
                raise

        task = content.virtualDiskManager.CopyVirtualDisk_Task(
            sourceName=src_path,
            sourceDatacenter=self._datacenter,
            destName=dest_vmdk_path,
            destDatacenter=self._datacenter,
            force=False,
        )
        self._wait_for_task(task)
        return dest_vmdk_path

    @staticmethod
    def _wait_for_task(task) -> None:
        while True:
            if task.info.state == "success":
                return
            if task.info.state == "error":
                raise RuntimeError(f"vSphere task failed: {task.info.error.msg}")

    # -- migrated from 03_vmdk_descriptor_cleanup.py -----------------------

    def _scrub_descriptor(self, ds_name: str, vm_name: str, dest_vmdk_path: str) -> None:
        vmdk_filename = dest_vmdk_path.rsplit("/", 1)[-1]
        url = self._datastore_file_url(ds_name, vm_name, vmdk_filename)
        session = self._cookie_session()

        resp = session.get(url, verify=self.settings.verify_ssl, timeout=DEFAULT_HTTP_TIMEOUT_SECONDS)
        if resp.status_code == 404:
            return
        resp.raise_for_status()

        new_text, changed = self._comment_out_filter_lines(resp.text)
        if not changed:
            return

        put_resp = session.put(
            url,
            data=new_text.encode("utf-8"),
            headers={"Content-Type": "application/octet-stream"},
            verify=self.settings.verify_ssl,
            timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
        )
        put_resp.raise_for_status()

    def _datastore_file_url(self, ds_name: str, vm_name: str, vmdk_filename: str) -> str:
        relative_path = quote(f"{vm_name}/{vmdk_filename}")
        query = urlencode({"dcPath": self.settings.vcenter_datacenter, "dsName": ds_name})
        return f"https://{self.settings.vcenter_ip}/folder/{relative_path}?{query}"

    @staticmethod
    def _comment_out_filter_lines(text: str) -> tuple[str, list[str]]:
        changed: list[str] = []
        out_lines: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            is_target = any(stripped.lower().startswith(key) for key in _DESCRIPTOR_FILTER_KEYS)
            if is_target and not stripped.startswith("#"):
                out_lines.append(f"#{line}")
                changed.append(line)
            else:
                out_lines.append(line)
        new_text = "\n".join(out_lines)
        if text.endswith("\n"):
            new_text += "\n"
        return new_text, changed

    # -- vSphere Replication REST API Gateway ------------------------------

    def _vr_login(self, user: str, password: str) -> str:
        # Assumed: the session token comes back in the `x-dr-session` response
        # header (per plan research); some VR Gateway versions may return it in the
        # JSON body instead -- confirm against the gateway's own Swagger UI.
        resp = requests.post(
            f"https://{self._vr_gateway}{VR_API_PREFIX}/session",
            auth=(user, password),
            verify=self.settings.verify_ssl,
            timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return resp.headers.get("x-dr-session") or resp.json().get("x-dr-session")

    def _vr_remote_session(self, pairing_id: str, target_user: str, target_password: str) -> None:
        resp = requests.post(
            f"https://{self._vr_gateway}{VR_API_PREFIX}/pairings/{pairing_id}/remote-session",
            auth=(target_user, target_password),
            headers={"x-dr-session": self._session_header},
            verify=self.settings.verify_ssl,
            timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        # There is only one session header throughout (no separate "remote" token);
        # pick up a rotated value if the gateway returns one, otherwise keep ours.
        self._session_header = resp.headers.get("x-dr-session", self._session_header)

    def _vr_request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        resp = requests.request(
            method,
            f"https://{self._vr_gateway}{VR_API_PREFIX}{path}",
            headers={"x-dr-session": self._session_header},
            verify=self.settings.verify_ssl,
            timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
            **kwargs,
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def _find_datacenter(self, name: str):
        content = self._si.RetrieveContent()
        for child in content.rootFolder.childEntity:
            if isinstance(child, vim.Datacenter) and child.name == name:
                return child
        raise RuntimeError(f"Datacenter '{name}' not found in vCenter")
