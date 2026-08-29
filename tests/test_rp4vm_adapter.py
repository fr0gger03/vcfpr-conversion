# SPDX-License-Identifier: Apache-2.0
"""Unit tests for RecoverPointAdapter manifest-building, plus a docker-gated auth-failure test."""

import pytest
import requests

from src.adapters.recoverpoint import RecoverPointAdapter
from src.config import Settings

CANNED_INVENTORY = [
    {
        "id": "cg-1",
        "name": "CG-Prod-Web",
        "rpoSeconds": 30,
        "bootPriority": 1,
        "startupDelaySeconds": 60,
        "vms": [
            {
                "id": "vm-1",
                "name": "web-01",
                "moref": "vm-100",
                "tags": ["prod"],
                "volumes": [
                    {
                        "id": "vol-1",
                        "capacityBytes": 42949672960,
                        "controllerIndex": 0,
                        "targetDatastorePath": "[ds1] web-01/web-01.vmdk",
                    }
                ],
            }
        ],
    },
    {
        # Alternate key naming (protectedVms/replicaVolumes/sizeInBytes/vmUid/volumeUid)
        # to exercise the adapter's fallback lookups.
        "consistencyGroupUid": "cg-2",
        "name": "CG-Prod-DB",
        "rpo": 15,
        "protectedVms": [
            {
                "vmUid": "vm-2",
                "name": "db-01",
                "replicaVolumes": [
                    {"volumeUid": "vol-2", "sizeInBytes": 107374182400},
                ],
            }
        ],
    },
]


def _adapter() -> RecoverPointAdapter:
    settings = Settings(rp4vm_ip="rp4vm.lab.local", rp4vm_user="admin", rp4vm_password="secret")
    return RecoverPointAdapter(settings, cluster_id="cluster-1")


def test_export_protection_manifest_maps_groups_vms_disks():
    manifest = _adapter().export_protection_manifest(CANNED_INVENTORY)

    assert manifest.metadata.source_cluster_id == "cluster-1"
    assert [g.name for g in manifest.protection_groups] == ["CG-Prod-Web", "CG-Prod-DB"]
    assert manifest.protection_groups[0].rpo_seconds == 30
    assert manifest.protection_groups[0].vm_ids == ["vm-1"]
    assert manifest.protection_groups[1].rpo_seconds == 15

    assert {vm.vm_id for vm in manifest.virtual_machines} == {"vm-1", "vm-2"}
    web_vm = next(vm for vm in manifest.virtual_machines if vm.vm_id == "vm-1")
    assert web_vm.name == "web-01"
    assert web_vm.tags == ["prod"]

    assert {d.disk_id for d in manifest.disks} == {"vol-1", "vol-2"}
    web_disk = next(d for d in manifest.disks if d.disk_id == "vol-1")
    assert web_disk.capacity_bytes == 42949672960
    assert web_disk.seed_file_path == "[ds1] web-01/web-01.vmdk"
    db_disk = next(d for d in manifest.disks if d.disk_id == "vol-2")
    assert db_disk.capacity_bytes == 107374182400
    assert db_disk.vm_id == "vm-2"


def test_export_protection_manifest_handles_empty_inventory():
    manifest = _adapter().export_protection_manifest([])

    assert manifest.protection_groups == []
    assert manifest.virtual_machines == []
    assert manifest.disks == []


@pytest.mark.docker
def test_authenticate_raises_on_failed_session_request():
    """Points authenticate() at a live (non-RP4VM) container so the session POST 404s,
    asserting the adapter surfaces the failure instead of silently proceeding."""
    try:
        from testcontainers.core.container import DockerContainer
    except ImportError:
        pytest.skip("testcontainers not installed")

    try:
        container = DockerContainer("nginx:alpine").with_exposed_ports(80)
        container.start()
    except Exception as exc:  # Docker daemon not available/running
        pytest.skip(f"Docker not available: {exc}")

    try:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(80)
        settings = Settings(
            rp4vm_ip=f"http://{host}:{port}",
            rp4vm_user="admin",
            rp4vm_password="wrong-password",
        )
        adapter = RecoverPointAdapter(settings)
        with pytest.raises(requests.exceptions.HTTPError):
            adapter.authenticate()
    finally:
        container.stop()
