# SPDX-License-Identifier: Apache-2.0
"""FastAPI TestClient coverage for src.ui -- no Docker, no live vCenter needed."""

import json

import pytest
from fastapi.testclient import TestClient

from src.ui.app import create_app


@pytest.fixture
def sample_manifest_path(tmp_path):
    manifest = {
        "schema_version": "1.0",
        "metadata": {
            "source_engine": "zerto",
            "source_cluster_id": "cluster-1",
            "extraction_timestamp": "2024-01-01T00:00:00Z",
        },
        "protection_groups": [{"name": "grp-1", "rpo_seconds": 900, "vm_ids": ["vm-1"]}],
        "virtual_machines": [{"vm_id": "vm-1", "name": "web-01", "tags": ["network:VLAN100"]}],
        "disks": [
            {
                "disk_id": "disk-1",
                "vm_id": "vm-1",
                "capacity_bytes": 1024,
                "controller_index": 0,
                "seed_file_path": "[ds1] web-01/web-01.vmdk",
            }
        ],
        "network_mappings": [{"source_network": "VLAN100", "target_nsx_segment_failover": "seg-100"}],
        "ip_customizations": [],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return str(path)


@pytest.fixture
def client():
    return TestClient(create_app())


def test_index(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["service"] == "vcf-migrator-ui"


# -- Mapping Matrix -----------------------------------------------------


def test_list_mappings_json(client, sample_manifest_path):
    resp = client.get("/api/mappings", params={"manifest_path": sample_manifest_path})
    assert resp.status_code == 200
    body = resp.json()
    assert body["network_mappings"] == [
        {"source_network": "VLAN100", "target_nsx_segment_failover": "seg-100", "target_nsx_segment_test": None}
    ]


def test_mapping_matrix_html_view(client, sample_manifest_path):
    resp = client.get("/mappings", params={"manifest_path": sample_manifest_path})
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "VLAN100" in resp.text
    assert "seg-100" in resp.text


def test_update_mapping_json_persists(client, sample_manifest_path):
    resp = client.post(
        "/api/mappings/VLAN100",
        params={"manifest_path": sample_manifest_path},
        json={"target_nsx_segment_failover": "seg-200", "target_nsx_segment_test": "seg-200-test"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["target_nsx_segment_failover"] == "seg-200"
    assert body["target_nsx_segment_test"] == "seg-200-test"

    # Persisted back to disk.
    persisted = json.loads(open(sample_manifest_path).read())
    mapping = persisted["network_mappings"][0]
    assert mapping["target_nsx_segment_failover"] == "seg-200"
    assert mapping["target_nsx_segment_test"] == "seg-200-test"

    # Reflected on a fresh GET too.
    resp2 = client.get("/api/mappings", params={"manifest_path": sample_manifest_path})
    assert resp2.json()["network_mappings"][0]["target_nsx_segment_failover"] == "seg-200"


def test_update_mapping_unknown_network_404s(client, sample_manifest_path):
    resp = client.post(
        "/api/mappings/does-not-exist",
        params={"manifest_path": sample_manifest_path},
        json={"target_nsx_segment_failover": "seg-x"},
    )
    assert resp.status_code == 404


def test_mapping_matrix_form_update_redirects(client, sample_manifest_path):
    resp = client.get(
        "/mappings/update",
        params={
            "source_network": "VLAN100",
            "target_nsx_segment_failover": "seg-300",
            "target_nsx_segment_test": "",
            "manifest_path": sample_manifest_path,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    persisted = json.loads(open(sample_manifest_path).read())
    assert persisted["network_mappings"][0]["target_nsx_segment_failover"] == "seg-300"


def test_missing_manifest_file_404s(client):
    resp = client.get("/api/mappings", params={"manifest_path": "/tmp/does-not-exist-manifest.json"})
    assert resp.status_code == 404


# -- Pre-Flight Dashboard -------------------------------------------------


def test_preflight_json_runs_all_checks(client, sample_manifest_path):
    resp = client.get("/api/preflight", params={"manifest_path": sample_manifest_path})
    assert resp.status_code == 200
    body = resp.json()
    check_names = [r["check_name"] for r in body["results"]]
    assert check_names == [
        "seed_disk_path_exists",
        "datastore_capacity",
        "rdm_conflict",
        "network_mapping_present",
        "seed_geometry_match",
    ]
    # network_mapping_present is the one manifest-only check and should pass
    # for the sample manifest (VLAN100 has a mapping).
    by_name = {r["check_name"]: r for r in body["results"]}
    assert by_name["network_mapping_present"]["passed"] is True


def test_preflight_html_view_renders_pass_fail(client, sample_manifest_path):
    resp = client.get("/preflight", params={"manifest_path": sample_manifest_path})
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "seed_disk_path_exists" in resp.text
    assert "network_mapping_present" in resp.text


def test_preflight_flags_missing_network_mapping(client, tmp_path):
    manifest = {
        "schema_version": "1.0",
        "metadata": {
            "source_engine": "zerto",
            "source_cluster_id": "cluster-1",
            "extraction_timestamp": "2024-01-01T00:00:00Z",
        },
        "protection_groups": [],
        "virtual_machines": [{"vm_id": "vm-1", "name": "web-01", "tags": ["network:VLAN999"]}],
        "disks": [],
        "network_mappings": [],
        "ip_customizations": [],
    }
    path = tmp_path / "manifest2.json"
    path.write_text(json.dumps(manifest))

    resp = client.get("/api/preflight", params={"manifest_path": str(path)})
    body = resp.json()
    by_name = {r["check_name"]: r for r in body["results"]}
    assert by_name["network_mapping_present"]["passed"] is False
    assert "VLAN999" in by_name["network_mapping_present"]["message"]
    assert body["all_passed"] is False


# -- Migration Console ------------------------------------------------------


def test_console_html_view_reachable(client):
    resp = client.get("/console")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "EventSource" in resp.text


def test_console_stream_reachable_and_streams_connected_event(client):
    # A synchronous TestClient only returns once the ASGI app coroutine
    # finishes, so an unbounded SSE stream would hang forever here.
    # max_events=0 makes the endpoint close right after the initial
    # "connected" event, which is enough to prove the stream is reachable
    # and correctly framed as SSE.
    resp = client.get("/console/stream", params={"max_events": 0})
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert "connected" in resp.text


def test_console_publish_and_simulate_endpoints(client):
    resp = client.post("/console/publish", json={"message": "hello"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "published", "message": "hello"}

    resp2 = client.post("/console/simulate", params={"steps": 3})
    assert resp2.status_code == 200
    assert resp2.json() == {"status": "simulated", "steps": 3}
