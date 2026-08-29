# SPDX-License-Identifier: Apache-2.0
"""File-based caching so repeated dev/test/UI runs don't re-hit source/target APIs.

- inventory.json: raw discovery payloads keyed by "{engine}:{cluster_id}", with a TTL.
- migration_state.json: per-protection-group content hash + status, for `provision`
  delta execution (skip groups that are already PROVISIONED and unchanged).
"""

import hashlib
import json
import time
from pathlib import Path
from typing import Any

CACHE_DIR = Path(".cache")
INVENTORY_FILE = CACHE_DIR / "inventory.json"
STATE_FILE = CACHE_DIR / "migration_state.json"

DEFAULT_TTL_SECONDS = 900


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))


def get_cached_inventory(key: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> Any | None:
    """Return cached discovery payload for `key` (e.g. "zerto:cluster-1") if still fresh."""
    entry = _read_json(INVENTORY_FILE).get(key)
    if not entry or time.time() - entry.get("cached_at", 0) > ttl_seconds:
        return None
    return entry.get("payload")


def set_cached_inventory(key: str, payload: Any) -> None:
    data = _read_json(INVENTORY_FILE)
    data[key] = {"cached_at": time.time(), "payload": payload}
    _write_json(INVENTORY_FILE, data)


def content_hash(content: Any) -> str:
    """Stable SHA-256 hash of a JSON-serializable object (e.g. a ProtectionGroup dict)."""
    canonical = json.dumps(content, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_group_state(group_name: str) -> dict[str, Any] | None:
    return _read_json(STATE_FILE).get(group_name)


def set_group_state(group_name: str, *, status: str, content: Any) -> None:
    data = _read_json(STATE_FILE)
    data[group_name] = {
        "hash": content_hash(content),
        "status": status,
        "timestamp": time.time(),
    }
    _write_json(STATE_FILE, data)


def is_unchanged_and_provisioned(group_name: str, content: Any) -> bool:
    """True if `group_name` is already PROVISIONED with this exact content (safe to skip)."""
    state = get_group_state(group_name)
    if not state or state.get("status") != "PROVISIONED":
        return False
    return state.get("hash") == content_hash(content)
