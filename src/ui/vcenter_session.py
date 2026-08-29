# SPDX-License-Identifier: Apache-2.0
"""VCenterSession implementation used by the pre-flight dashboard.

`engine.validator.run_preflight_checks()` needs a `VCenterSession` to answer
live datastore/RDM questions. No adapter/CLI code exposes a shared, reusable
vCenter connection helper yet (the only pyVmomi session lives inside
`VCFProtectionAdapter.authenticate()`), so this module provides a permissive
stub: it reports "nothing contradicts the manifest" for every live-infra
check. That keeps `check_network_mappings` (the one pure manifest-only check)
meaningful in the dashboard today, while the datastore/RDM/seed-geometry
checks trivially pass until a real session is plugged in via
`get_vcenter_session()`.
"""

from src.models.manifest import Disk


class StubVCenterSession:
    """No-op VCenterSession: always reports "no live conflicts found"."""

    def datastore_file_exists(self, datastore: str, path: str) -> bool:
        return True

    def datastore_free_bytes(self, datastore: str) -> int | None:
        return None

    def seed_disk_size_bytes(self, datastore: str, path: str) -> int | None:
        return None

    def list_rdm_canonical_names(self) -> set[str]:
        return set()

    def disk_canonical_name(self, disk: Disk) -> str | None:
        return None


def get_vcenter_session() -> StubVCenterSession:
    """Return the VCenterSession the dashboard route should use.

    Swap this for a real pyVmomi-backed session once the CLI exposes a shared
    vCenter connection helper -- the route calling this doesn't need to change.
    """
    return StubVCenterSession()
