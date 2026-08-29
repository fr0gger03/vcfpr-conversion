# SPDX-License-Identifier: Apache-2.0
"""Tests for ZertoAdapter: manifest-building unit tests + a docker-based auth-failure test."""

import pytest

from src.adapters.zerto import ZertoAdapter, get_val
from src.config import Settings

VMS = [
    {"vmIdentifier": "vm-1", "vmName": "AppServer01", "vpgName": "VPG-Prod"},
    {"vmIdentifier": "vm-2", "vmName": "DBServer01", "vpgName": "VPG-Prod"},
    {"vmIdentifier": "vm-3", "vmName": "WebServer01", "vpgName": "VPG-Web"},
]

VOLUMES = [
    {
        "volumeType": "Recovery",
        "protectedVm": {"identifier": "vm-1"},
        "datastore": {"name": "DS01"},
        "path": {
            "full": "[DS01] AppServer01_replica/AppServer01_1.vmdk",
            "fileName": "AppServer01_1.vmdk",
        },
        "sizeInBytes": 42949672960,
    },
    {
        "volumeType": "Recovery",
        "owningVm": {"identifier": "vm-3"},
        "datastore": {"name": "DS02"},
        "path": {"fileName": "WebServer01_1.vmdk"},
    },
    {
        "volumeType": "Journal",  # not a recovery disk -> must be excluded
        "protectedVm": {"identifier": "vm-2"},
        "datastore": {"name": "DS01"},
        "path": {"full": "[DS01] journal/j.vmdk", "fileName": "j.vmdk"},
    },
]


def make_adapter() -> ZertoAdapter:
    settings = Settings(zvm_ip="10.0.0.5", zvm_client_id="id", zvm_client_secret="secret")
    return ZertoAdapter(settings=settings)


def test_get_val_case_insensitive():
    assert get_val({"Foo": 1}, "foo") == 1
    assert get_val({"foo": 1}, "Foo") == 1
    assert get_val({}, "foo", "default") == "default"
    assert get_val(None, "foo", "default") == "default"


def test_export_protection_manifest_groups_vpgs_and_vms():
    manifest = make_adapter().export_protection_manifest([{"vras": [], "vms": VMS, "volumes": VOLUMES}])

    assert {pg.name for pg in manifest.protection_groups} == {"VPG-Prod", "VPG-Web"}
    prod_pg = next(pg for pg in manifest.protection_groups if pg.name == "VPG-Prod")
    assert set(prod_pg.vm_ids) == {"vm-1", "vm-2"}
    assert {vm.vm_id for vm in manifest.virtual_machines} == {"vm-1", "vm-2", "vm-3"}


def test_export_protection_manifest_builds_seed_paths_and_excludes_non_recovery():
    manifest = make_adapter().export_protection_manifest([{"vras": [], "vms": VMS, "volumes": VOLUMES}])

    assert len(manifest.disks) == 2  # Journal volume excluded
    disk = next(d for d in manifest.disks if d.vm_id == "vm-1")
    assert disk.seed_file_path == "[DS01] AppServer01/AppServer01_1.vmdk"
    assert disk.capacity_bytes == 42949672960

    disk_no_full_path = next(d for d in manifest.disks if d.vm_id == "vm-3")
    assert disk_no_full_path.seed_file_path == "[DS02] WebServer01/WebServer01_1.vmdk"
    assert disk_no_full_path.capacity_bytes == 0  # sizeInBytes absent -> default


def test_export_protection_manifest_handles_empty_inventory():
    manifest = make_adapter().export_protection_manifest([])
    assert manifest.protection_groups == []
    assert manifest.virtual_machines == []
    assert manifest.disks == []


def test_authenticate_success(monkeypatch):
    class FakeResponse:
        def json(self):
            return {"access_token": "tok-123"}

    monkeypatch.setattr("src.adapters.zerto.requests.post", lambda *a, **kw: FakeResponse())

    adapter = make_adapter()
    adapter.authenticate()
    assert adapter._token == "tok-123"
    assert adapter._headers() == {"Authorization": "Bearer tok-123"}


def test_authenticate_missing_token_raises(monkeypatch):
    class FakeResponse:
        status_code = 401

        def json(self):
            return {"error": "invalid_client"}

    monkeypatch.setattr("src.adapters.zerto.requests.post", lambda *a, **kw: FakeResponse())

    adapter = make_adapter()
    with pytest.raises(RuntimeError, match="Zerto authentication failed"):
        adapter.authenticate()


def test_headers_without_authenticate_raises():
    with pytest.raises(RuntimeError, match="authenticate\\(\\) must be called"):
        make_adapter()._headers()


def test_discover_inventory_uses_cache(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    adapter = make_adapter()
    adapter._token = "tok"

    call_count = {"n": 0}

    class FakeResponse:
        def json(self):
            return []

    def fake_get(*args, **kwargs):
        call_count["n"] += 1
        return FakeResponse()

    monkeypatch.setattr("src.adapters.zerto.requests.get", fake_get)

    first = adapter.discover_inventory()
    second = adapter.discover_inventory()

    assert first == second
    assert call_count["n"] == 3  # vras + vms + volumes, only on the first (uncached) call


def test_quiesce_replication_posts_pause(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

    def fake_post(url, headers=None, verify=None, **kwargs):
        captured["url"] = url
        return FakeResponse()

    monkeypatch.setattr("src.adapters.zerto.requests.post", fake_post)

    adapter = make_adapter()
    adapter._token = "tok"
    adapter.quiesce_replication("vpg-1")
    assert captured["url"] == "https://10.0.0.5/v1/vpgs/vpg-1/pause"


def test_cleanup_source_deletes_with_keep_target_disks_flag(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

    def fake_delete(url, headers=None, json=None, verify=None, **kwargs):
        captured["url"] = url
        captured["json"] = json
        return FakeResponse()

    monkeypatch.setattr("src.adapters.zerto.requests.delete", fake_delete)

    adapter = make_adapter()
    adapter._token = "tok"
    adapter.cleanup_source("vpg-1", keep_target_disks=True)
    assert captured["url"] == "https://10.0.0.5/v1/vpgs/vpg-1"
    assert captured["json"] == {"KeepTheRecoveryDisks": True}


_AUTH_FAIL_SERVER_SCRIPT = """
import http.server, json, sys

class Handler(http.server.BaseHTTPRequestHandler):
    def _reply(self):
        body = json.dumps({"error": "invalid_client"}).encode()
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply()

    def do_POST(self):
        self._reply()

    def log_message(self, *args):
        pass

print("ready", flush=True)
http.server.HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
"""


@pytest.mark.docker
def test_authenticate_handles_auth_failure_via_container():
    try:
        from testcontainers.core.container import DockerContainer
        from testcontainers.core.waiting_utils import wait_for_logs
    except ImportError:
        pytest.skip("testcontainers not installed")

    try:
        container = DockerContainer("python:3.12-alpine").with_command(
            ["python3", "-c", _AUTH_FAIL_SERVER_SCRIPT]
        ).with_exposed_ports(8000)
        container.start()
    except Exception as exc:  # Docker daemon unavailable / image pull failed
        pytest.skip(f"Docker not available: {exc}")

    try:
        wait_for_logs(container, "ready", timeout=30)
        host = container.get_container_host_ip()
        port = container.get_exposed_port(8000)

        settings = Settings(zvm_ip=f"{host}:{port}", zvm_client_id="id", zvm_client_secret="secret")
        adapter = ZertoAdapter(settings=settings, base_url=f"http://{host}:{port}")

        with pytest.raises(RuntimeError, match="Zerto authentication failed"):
            adapter.authenticate()
    finally:
        container.stop()
