# SPDX-License-Identifier: Apache-2.0
"""Pre-Flight Dashboard: loads a manifest, runs `engine.validator.run_preflight_checks()`,
and renders green/red pass/fail results per `CheckResult`."""

from html import escape

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from src.engine.validator import run_preflight_checks
from src.ui.manifest_store import default_manifest_path, load_manifest
from src.ui.vcenter_session import get_vcenter_session

router = APIRouter(tags=["preflight-dashboard"])


@router.get("/api/preflight")
def preflight_json(manifest_path: str | None = None) -> dict:
    manifest = load_manifest(manifest_path)
    results = run_preflight_checks(manifest, get_vcenter_session())
    return {
        "manifest_path": manifest_path or default_manifest_path(),
        "results": [{"check_name": r.check_name, "passed": r.passed, "message": r.message} for r in results],
        "all_passed": all(r.passed for r in results),
    }


@router.get("/preflight", response_class=HTMLResponse)
def preflight_view(manifest_path: str | None = None) -> str:
    manifest = load_manifest(manifest_path)
    path = manifest_path or default_manifest_path()
    results = run_preflight_checks(manifest, get_vcenter_session())
    rows = "".join(
        f"""<tr style="background-color:{'#c8f7c5' if r.passed else '#f7c5c5'}">
              <td>{escape(r.check_name)}</td>
              <td>{'PASS' if r.passed else 'FAIL'}</td>
              <td>{escape(r.message)}</td>
            </tr>"""
        for r in results
    )
    return f"""<html><head><title>Pre-Flight Dashboard</title></head>
<body>
<h1>Pre-Flight Dashboard</h1>
<p>Manifest: {escape(path)}</p>
<table border="1" cellpadding="4">
<tr><th>Check</th><th>Result</th><th>Message</th></tr>
{rows}
</table>
</body></html>"""
