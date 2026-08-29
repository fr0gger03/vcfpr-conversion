# SPDX-License-Identifier: Apache-2.0
"""FastAPI application factory for the migration tool's web UI.

Wires up the three UI surfaces from the migration-engine plan:
  * Mapping Matrix        -- src.ui.mapping   (/mappings, /api/mappings)
  * Pre-Flight Dashboard  -- src.ui.preflight (/preflight, /api/preflight)
  * Migration Console     -- src.ui.console   (/console, /console/stream)

The CLI's `ui` subcommand should launch this via uvicorn against the factory,
e.g.:
    uvicorn src.ui.app:create_app --factory --port 8000
"""

from fastapi import FastAPI

from src.ui.console import router as console_router
from src.ui.mapping import router as mapping_router
from src.ui.preflight import router as preflight_router


def create_app() -> FastAPI:
    app = FastAPI(title="VCF Migration Tool", version="0.1.0")
    app.include_router(mapping_router)
    app.include_router(preflight_router)
    app.include_router(console_router)

    @app.get("/", include_in_schema=False)
    def index() -> dict:
        return {
            "service": "vcf-migrator-ui",
            "surfaces": {
                "mapping_matrix": "/mappings",
                "preflight_dashboard": "/preflight",
                "migration_console": "/console",
            },
        }

    return app


# Convenience module-level instance, e.g. for `uvicorn src.ui.app:app`.
app = create_app()
