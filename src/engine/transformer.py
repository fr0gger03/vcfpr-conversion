# SPDX-License-Identifier: Apache-2.0
"""Adapter-agnostic conversion of raw discovery payloads into a validated Manifest.

Source adapters (ZertoAdapter, RecoverPointAdapter) call `build_manifest()` from their
`export_protection_manifest()` implementation after normalizing their provider-specific
payloads into the plain dict shapes documented below. Keep this signature stable --
other adapters/worktrees depend on it.
"""

from datetime import datetime, timezone
from typing import Any

from src.config import compute_dest_paths
from src.models.manifest import (
    Disk,
    IPCustomization,
    Manifest,
    ManifestMetadata,
    NetworkMapping,
    ProtectionGroup,
    SourceEngine,
    VirtualMachine,
)


def resolve_seed_path(ds_name: str, vm_name: str, source_raw_path: str) -> str:
    """Return the `[Datastore] VM_Name/VMDK` path a seed disk lands at on the target
    datastore, per the convention shared with `src.config.compute_dest_paths`."""
    _src, _folder, dest_vmdk_path, _filename = compute_dest_paths(ds_name, vm_name, source_raw_path)
    return dest_vmdk_path


def _build_disk(raw: dict[str, Any]) -> Disk:
    seed_file_path = raw.get("seed_file_path")
    if seed_file_path is None and raw.get("datastore") and raw.get("vm_name") and raw.get("source_raw_path"):
        seed_file_path = resolve_seed_path(raw["datastore"], raw["vm_name"], raw["source_raw_path"])
    return Disk(
        disk_id=raw["disk_id"],
        vm_id=raw["vm_id"],
        capacity_bytes=raw["capacity_bytes"],
        controller_index=raw.get("controller_index", 0),
        seed_file_path=seed_file_path,
    )


def build_manifest(
    source_engine: SourceEngine | str,
    cluster_id: str,
    groups: list[dict[str, Any]],
    vms: list[dict[str, Any]],
    disks: list[dict[str, Any]],
    network_mappings: list[dict[str, Any]] | None = None,
    ip_customizations: list[dict[str, Any]] | None = None,
    extraction_timestamp: datetime | None = None,
) -> Manifest:
    """Build a validated Manifest from adapter-agnostic raw dicts. Extra keys in each
    raw dict are ignored by pydantic, so adapters may pass through provider-specific
    fields without a separate filtering step.

    Expected raw shapes:
      groups: {name, rpo_seconds, boot_priority?, startup_delay_seconds?, vm_ids}
      vms: {vm_id, name, vcenter_moref?, tags?}
      disks: {disk_id, vm_id, capacity_bytes, controller_index?, seed_file_path?}
             OR {..., datastore, vm_name, source_raw_path} to have seed_file_path
             resolved via the `[Datastore] VM_Name/VMDK` convention.
      network_mappings: {source_network, target_nsx_segment_failover, target_nsx_segment_test?}
      ip_customizations: {vm_id, subnet?, gateway?, dns?, static_ip?}
    """
    metadata = ManifestMetadata(
        source_engine=SourceEngine(source_engine),
        source_cluster_id=cluster_id,
        extraction_timestamp=extraction_timestamp or datetime.now(timezone.utc),
    )
    return Manifest(
        metadata=metadata,
        protection_groups=[ProtectionGroup(**g) for g in groups],
        virtual_machines=[VirtualMachine(**v) for v in vms],
        disks=[_build_disk(d) for d in disks],
        network_mappings=[NetworkMapping(**n) for n in (network_mappings or [])],
        ip_customizations=[IPCustomization(**i) for i in (ip_customizations or [])],
    )
