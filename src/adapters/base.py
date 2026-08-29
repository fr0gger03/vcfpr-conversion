# SPDX-License-Identifier: Apache-2.0
"""Abstract contract every DR engine adapter (Zerto, RecoverPoint, VCF) must implement."""

from abc import ABC, abstractmethod
from typing import Any

from src.models.manifest import Manifest


class BaseDREngine(ABC):
    """ETL contract: authenticate -> discover -> export manifest -> quiesce/cleanup source."""

    @abstractmethod
    def authenticate(self) -> None:
        """Establish an authenticated session with the source/target engine."""

    @abstractmethod
    def discover_inventory(self) -> list[dict[str, Any]]:
        """Batch-query protected VMs/groups/volumes. Returns raw provider payloads
        (adapters should cache this via src.cache to avoid redundant API calls)."""

    @abstractmethod
    def export_protection_manifest(self, inventory: list[dict[str, Any]]) -> Manifest:
        """Transform raw inventory into a validated Manifest (via src.engine.transformer)."""

    @abstractmethod
    def quiesce_replication(self, group_id: str) -> None:
        """Pause replication for a protection group/VPG/consistency group."""

    @abstractmethod
    def cleanup_source(self, group_id: str, *, keep_target_disks: bool = True) -> None:
        """Unprotect/delete the source protection group. Must default to preserving
        target-side seed disks so they remain usable for VCF seed-based provisioning."""
