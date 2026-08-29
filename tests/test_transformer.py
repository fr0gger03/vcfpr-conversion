# SPDX-License-Identifier: Apache-2.0
"""Pure unit tests for engine.transformer.build_manifest and engine.validator's
pure-logic checks -- no Docker/testcontainers, no live vCenter/API calls."""

from src.engine.transformer import build_manifest, resolve_seed_path
from src.engine.validator import (
    CheckResult,
    check_datastore_capacity,
    check_network_mappings,
    check_rdm_conflicts,
    check_seed_geometry,
    check_seed_paths_exist,
    run_preflight_checks,
)
from src.models.manifest import Disk, SourceEngine


class FakeVCenterSession:
    """Implements the VCenterSession protocol with canned in-memory data."""

    def __init__(self, existing_files=None, free_bytes=None, rdm_names=None, disk_canonical_names=None, seed_sizes=None):
        self.existing_files = existing_files or set()
        self.free_bytes = free_bytes or {}
        self.rdm_names = rdm_names or set()
        self.disk_canonical_names = disk_canonical_names or {}
        self.seed_sizes = seed_sizes or {}

    def datastore_file_exists(self, datastore, path):
        return (datastore, path) in self.existing_files

    def datastore_free_bytes(self, datastore):
        return self.free_bytes.get(datastore)

    def seed_disk_size_bytes(self, datastore, path):
        return self.seed_sizes.get((datastore, path))

    def list_rdm_canonical_names(self):
        return self.rdm_names

    def disk_canonical_name(self, disk):
        return self.disk_canonical_names.get(disk.disk_id)


def _sample_manifest(**overrides):
    groups = overrides.get("groups", [{"name": "grp-1", "rpo_seconds": 900, "vm_ids": ["vm-1"]}])
    vms = overrides.get("vms", [{"vm_id": "vm-1", "name": "web-01", "tags": ["network:VLAN100"]}])
    disks = overrides.get(
        "disks",
        [{"disk_id": "disk-1", "vm_id": "vm-1", "capacity_bytes": 1024, "seed_file_path": "[ds1] web-01/web-01.vmdk"}],
    )
    network_mappings = overrides.get(
        "network_mappings", [{"source_network": "VLAN100", "target_nsx_segment_failover": "seg-100"}]
    )
    return build_manifest(SourceEngine.ZERTO, "cluster-1", groups, vms, disks, network_mappings)


def test_build_manifest_basic_shape():
    manifest = _sample_manifest()
    assert manifest.metadata.source_engine == SourceEngine.ZERTO
    assert manifest.metadata.source_cluster_id == "cluster-1"
    assert [g.name for g in manifest.protection_groups] == ["grp-1"]
    assert [v.vm_id for v in manifest.virtual_machines] == ["vm-1"]
    assert manifest.disks[0].seed_file_path == "[ds1] web-01/web-01.vmdk"


def test_build_manifest_resolves_seed_path_from_datastore_fields():
    disks = [
        {
            "disk_id": "disk-2",
            "vm_id": "vm-1",
            "capacity_bytes": 2048,
            "datastore": "ds1",
            "vm_name": "web-01",
            "source_raw_path": "zerto-vra/web-01_1.vmdk",
        }
    ]
    manifest = _sample_manifest(disks=disks)
    assert manifest.disks[0].seed_file_path == "[ds1] web-01/web-01_1.vmdk"


def test_resolve_seed_path_matches_bracketed_source():
    dest = resolve_seed_path("ds1", "web-01", "[ds1] zerto-vra/web-01_1.vmdk")
    assert dest == "[ds1] web-01/web-01_1.vmdk"


def test_check_seed_paths_exist_pass_and_fail():
    manifest = _sample_manifest()
    ok = check_seed_paths_exist(manifest, FakeVCenterSession(existing_files={("ds1", "web-01/web-01.vmdk")}))
    assert ok == CheckResult("seed_disk_path_exists", True, "All seed disk paths found.")

    missing = check_seed_paths_exist(manifest, FakeVCenterSession())
    assert missing.passed is False
    assert "[ds1] web-01/web-01.vmdk" in missing.message


def test_check_datastore_capacity_shortfall():
    manifest = _sample_manifest()
    result = check_datastore_capacity(manifest, FakeVCenterSession(free_bytes={"ds1": 100}))
    assert result.passed is False
    assert "ds1" in result.message

    result_ok = check_datastore_capacity(manifest, FakeVCenterSession(free_bytes={"ds1": 10_000}))
    assert result_ok.passed is True


def test_check_rdm_conflicts():
    manifest = _sample_manifest()
    session = FakeVCenterSession(rdm_names={"naa.123"}, disk_canonical_names={"disk-1": "naa.123"})
    result = check_rdm_conflicts(manifest, session)
    assert result.passed is False
    assert "disk-1" in result.message

    clean = check_rdm_conflicts(manifest, FakeVCenterSession(rdm_names={"naa.999"}, disk_canonical_names={"disk-1": "naa.123"}))
    assert clean.passed is True


def test_check_network_mappings_missing():
    manifest = _sample_manifest(network_mappings=[])
    result = check_network_mappings(manifest)
    assert result.passed is False
    assert "VLAN100" in result.message


def test_check_network_mappings_present():
    manifest = _sample_manifest()
    result = check_network_mappings(manifest)
    assert result.passed is True


def test_check_seed_geometry_mismatch_fails_loudly():
    manifest = _sample_manifest()
    session = FakeVCenterSession(seed_sizes={("ds1", "web-01/web-01.vmdk"): 2048})
    result = check_seed_geometry(manifest, session)
    assert result.passed is False
    assert "disk-1" in result.message
    assert "manifest=1024" in result.message
    assert "seed=2048" in result.message


def test_check_seed_geometry_match_passes():
    manifest = _sample_manifest()
    session = FakeVCenterSession(seed_sizes={("ds1", "web-01/web-01.vmdk"): 1024})
    result = check_seed_geometry(manifest, session)
    assert result.passed is True


def test_run_preflight_checks_runs_all_five():
    manifest = _sample_manifest()
    session = FakeVCenterSession(
        existing_files={("ds1", "web-01/web-01.vmdk")},
        free_bytes={"ds1": 10_000},
        seed_sizes={("ds1", "web-01/web-01.vmdk"): 1024},
    )
    results = run_preflight_checks(manifest, session)
    assert [r.check_name for r in results] == [
        "seed_disk_path_exists",
        "datastore_capacity",
        "rdm_conflict",
        "network_mapping_present",
        "seed_geometry_match",
    ]
    assert all(r.passed for r in results)


def test_disk_model_capacity_bytes_field_present():
    # Sanity check that Disk exposes the field the seed geometry check compares.
    disk = Disk(disk_id="d1", vm_id="vm-1", capacity_bytes=555, controller_index=0)
    assert disk.capacity_bytes == 555
