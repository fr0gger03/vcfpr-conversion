# SPDX-License-Identifier: Apache-2.0
"""Pre-flight checks run before `provision` calls `Configure Replication`.

Each check returns a structured CheckResult so failures surface clearly in the CLI
and (later) the UI dashboard instead of failing deep inside a vSphere Replication API
call. `VCenterSession` is a narrow protocol so the pure-logic checks stay unit
testable (see tests/test_transformer.py) without a live pyVmomi connection.
"""

from dataclasses import dataclass
from typing import Protocol

from src.models.manifest import Disk, Manifest


@dataclass
class CheckResult:
    check_name: str
    passed: bool
    message: str = ""


class VCenterSession(Protocol):
    """Minimal live-vCenter surface the checks below need. A real implementation
    wraps pyVmomi datastore browsing/HostVirtualDisk queries; tests use a fake."""

    def datastore_file_exists(self, datastore: str, path: str) -> bool: ...
    def datastore_free_bytes(self, datastore: str) -> int | None: ...
    def seed_disk_size_bytes(self, datastore: str, path: str) -> int | None: ...
    def list_rdm_canonical_names(self) -> set[str]: ...
    def disk_canonical_name(self, disk: Disk) -> str | None: ...


def _parse_datastore_path(seed_file_path: str) -> tuple[str, str] | None:
    """Split a `[Datastore] VM_Name/VMDK` path into (datastore, relative_path)."""
    if not seed_file_path.startswith("["):
        return None
    ds_name, _, rel = seed_file_path[1:].partition("] ")
    if not rel:
        return None
    return ds_name, rel


def check_seed_paths_exist(manifest: Manifest, session: VCenterSession) -> CheckResult:
    missing = []
    for disk in manifest.disks:
        if not disk.seed_file_path:
            continue
        parsed = _parse_datastore_path(disk.seed_file_path)
        if not parsed or not session.datastore_file_exists(*parsed):
            missing.append(disk.seed_file_path)
    if missing:
        return CheckResult("seed_disk_path_exists", False, f"Missing seed disk(s): {', '.join(missing)}")
    return CheckResult("seed_disk_path_exists", True, "All seed disk paths found.")


def check_datastore_capacity(manifest: Manifest, session: VCenterSession) -> CheckResult:
    needed_by_ds: dict[str, int] = {}
    for disk in manifest.disks:
        if not disk.seed_file_path:
            continue
        parsed = _parse_datastore_path(disk.seed_file_path)
        if not parsed:
            continue
        ds_name, _ = parsed
        needed_by_ds[ds_name] = needed_by_ds.get(ds_name, 0) + disk.capacity_bytes

    shortfalls = []
    for ds_name, needed in needed_by_ds.items():
        free = session.datastore_free_bytes(ds_name)
        if free is not None and free < needed:
            shortfalls.append(f"{ds_name} needs {needed} bytes, has {free} free")
    if shortfalls:
        return CheckResult("datastore_capacity", False, "; ".join(shortfalls))
    return CheckResult("datastore_capacity", True, "Sufficient free space on all target datastores.")


def check_rdm_conflicts(manifest: Manifest, session: VCenterSession) -> CheckResult:
    rdm_names = session.list_rdm_canonical_names()
    conflicts = [
        disk.disk_id
        for disk in manifest.disks
        if (canonical := session.disk_canonical_name(disk)) and canonical in rdm_names
    ]
    if conflicts:
        return CheckResult("rdm_conflict", False, f"Disk(s) collide with existing RDM(s): {', '.join(conflicts)}")
    return CheckResult("rdm_conflict", True, "No RDM conflicts detected.")


def check_network_mappings(manifest: Manifest) -> CheckResult:
    """Pure logic: flags any `network:<name>` VM tag (how adapters record the source
    network a VM is attached to) with no corresponding NetworkMapping."""
    mapped = {m.source_network for m in manifest.network_mappings}
    used = {
        tag.split("network:", 1)[1]
        for vm in manifest.virtual_machines
        for tag in vm.tags
        if tag.startswith("network:")
    }
    missing = sorted(used - mapped)
    if missing:
        return CheckResult("network_mapping_present", False, f"No target mapping for network(s): {', '.join(missing)}")
    return CheckResult("network_mapping_present", True, "All referenced networks have a target mapping.")


def check_seed_geometry(manifest: Manifest, session: VCenterSession) -> CheckResult:
    """`Configure Replication` with `use_seeds=true` fails unrecoverably if the seed
    VMDK's actual size doesn't match the source disk's reported capacity. Catch that
    here rather than mid-provision."""
    mismatches = []
    for disk in manifest.disks:
        if not disk.seed_file_path:
            continue
        parsed = _parse_datastore_path(disk.seed_file_path)
        if not parsed:
            continue
        actual = session.seed_disk_size_bytes(*parsed)
        if actual is not None and actual != disk.capacity_bytes:
            mismatches.append(f"{disk.disk_id}: manifest={disk.capacity_bytes} seed={actual}")
    if mismatches:
        return CheckResult("seed_geometry_match", False, "Seed/manifest size mismatch: " + "; ".join(mismatches))
    return CheckResult("seed_geometry_match", True, "Seed disk sizes match manifest capacities.")


def run_preflight_checks(manifest: Manifest, vcenter_session: VCenterSession) -> list[CheckResult]:
    """Single entrypoint the CLI (`validate`) and future UI dashboard both call."""
    return [
        check_seed_paths_exist(manifest, vcenter_session),
        check_datastore_capacity(manifest, vcenter_session),
        check_rdm_conflicts(manifest, vcenter_session),
        check_network_mappings(manifest),
        check_seed_geometry(manifest, vcenter_session),
    ]
