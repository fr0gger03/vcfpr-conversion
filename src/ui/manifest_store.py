# SPDX-License-Identifier: Apache-2.0
"""Manifest load/save helpers shared by the UI's mapping and pre-flight routes.

Keeps a single, obvious convention: manifests are plain JSON files on disk (the
same files produced by the CLI's `export` command / consumed by `provision`), so
the UI never duplicates the source of truth or invents its own storage format.
"""

from pathlib import Path

from fastapi import HTTPException

from src.config import Settings
from src.models.manifest import Manifest


def default_manifest_path() -> str:
    """Manifest path to use when a request doesn't supply `manifest_path`."""
    return Settings().manifest_file


def load_manifest(path: str | None = None) -> Manifest:
    manifest_path = Path(path or default_manifest_path())
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail=f"Manifest file not found: {manifest_path}")
    try:
        return Manifest.model_validate_json(manifest_path.read_text())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid manifest file '{manifest_path}': {exc}") from exc


def save_manifest(manifest: Manifest, path: str | None = None) -> None:
    manifest_path = Path(path or default_manifest_path())
    manifest_path.write_text(manifest.model_dump_json(indent=2))
