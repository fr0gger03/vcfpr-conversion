# SPDX-License-Identifier: Apache-2.0
"""Mapping Matrix: list and edit `source_network -> target NSX segment`
mappings on a loaded manifest, persisting edits back to the manifest JSON file.
"""

from html import escape

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from src.models.manifest import Manifest, NetworkMapping
from src.ui.manifest_store import default_manifest_path, load_manifest, save_manifest

router = APIRouter(tags=["mapping-matrix"])


class MappingUpdate(BaseModel):
    target_nsx_segment_failover: str
    target_nsx_segment_test: str | None = None


def _apply_update(manifest: Manifest, source_network: str, update: MappingUpdate) -> NetworkMapping:
    for mapping in manifest.network_mappings:
        if mapping.source_network == source_network:
            mapping.target_nsx_segment_failover = update.target_nsx_segment_failover
            mapping.target_nsx_segment_test = update.target_nsx_segment_test
            return mapping
    raise HTTPException(status_code=404, detail=f"No mapping found for source network '{source_network}'")


@router.get("/api/mappings")
def list_mappings_json(manifest_path: str | None = None) -> dict:
    manifest = load_manifest(manifest_path)
    return {
        "manifest_path": manifest_path or default_manifest_path(),
        "network_mappings": [m.model_dump() for m in manifest.network_mappings],
    }


@router.post("/api/mappings/{source_network}")
def update_mapping_json(source_network: str, update: MappingUpdate, manifest_path: str | None = None) -> dict:
    manifest = load_manifest(manifest_path)
    mapping = _apply_update(manifest, source_network, update)
    save_manifest(manifest, manifest_path)
    return mapping.model_dump()


@router.get("/mappings", response_class=HTMLResponse)
def mapping_matrix_view(manifest_path: str | None = None) -> str:
    manifest = load_manifest(manifest_path)
    path = manifest_path or default_manifest_path()
    rows = "".join(
        f"""<tr>
              <td>{escape(m.source_network)}</td>
              <td colspan="3">
                <form method="get" action="/mappings/update" style="display:flex;gap:4px;">
                  <input type="hidden" name="manifest_path" value="{escape(path)}">
                  <input type="hidden" name="source_network" value="{escape(m.source_network)}">
                  <input name="target_nsx_segment_failover" value="{escape(m.target_nsx_segment_failover)}">
                  <input name="target_nsx_segment_test" value="{escape(m.target_nsx_segment_test or '')}">
                  <button type="submit">Save</button>
                </form>
              </td>
            </tr>"""
        for m in manifest.network_mappings
    )
    return f"""<html><head><title>Mapping Matrix</title></head>
<body>
<h1>Mapping Matrix</h1>
<p>Manifest: {escape(path)}</p>
<table border="1" cellpadding="4">
<tr><th>Source Network</th><th colspan="3">Target NSX Segment (Failover / Test)</th></tr>
{rows}
</table>
</body></html>"""


@router.get("/mappings/update")
def mapping_matrix_update_form(
    source_network: str,
    target_nsx_segment_failover: str,
    target_nsx_segment_test: str = "",
    manifest_path: str = "",
) -> RedirectResponse:
    resolved_path = manifest_path or None
    manifest = load_manifest(resolved_path)
    _apply_update(
        manifest,
        source_network,
        MappingUpdate(
            target_nsx_segment_failover=target_nsx_segment_failover,
            target_nsx_segment_test=target_nsx_segment_test or None,
        ),
    )
    save_manifest(manifest, resolved_path)
    suffix = f"?manifest_path={manifest_path}" if manifest_path else ""
    return RedirectResponse(url=f"/mappings{suffix}", status_code=303)
