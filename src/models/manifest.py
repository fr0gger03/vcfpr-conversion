# SPDX-License-Identifier: Apache-2.0
"""Standardized protection manifest schema (see README "Manifest Schema" section)."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

MANIFEST_SCHEMA_VERSION = "1.0"


class SourceEngine(str, Enum):
    ZERTO = "zerto"
    RECOVERPOINT = "recoverpoint"


class ManifestMetadata(BaseModel):
    source_engine: SourceEngine
    source_cluster_id: str
    extraction_timestamp: datetime


class Disk(BaseModel):
    disk_id: str
    vm_id: str
    capacity_bytes: int
    controller_index: int
    seed_file_path: str | None = None  # "[Datastore] VM_Name/VMDK" on the target datastore


class VirtualMachine(BaseModel):
    vm_id: str
    name: str
    vcenter_moref: str | None = None
    tags: list[str] = Field(default_factory=list)


class ProtectionGroup(BaseModel):
    name: str
    rpo_seconds: int
    boot_priority: int = 0
    startup_delay_seconds: int = 0
    vm_ids: list[str] = Field(default_factory=list)


class NetworkMapping(BaseModel):
    source_network: str
    target_nsx_segment_failover: str
    target_nsx_segment_test: str | None = None


class IPCustomization(BaseModel):
    vm_id: str
    subnet: str | None = None
    gateway: str | None = None
    dns: list[str] = Field(default_factory=list)
    static_ip: str | None = None


class Manifest(BaseModel):
    schema_version: str = MANIFEST_SCHEMA_VERSION
    metadata: ManifestMetadata
    protection_groups: list[ProtectionGroup] = Field(default_factory=list)
    virtual_machines: list[VirtualMachine] = Field(default_factory=list)
    disks: list[Disk] = Field(default_factory=list)
    network_mappings: list[NetworkMapping] = Field(default_factory=list)
    ip_customizations: list[IPCustomization] = Field(default_factory=list)
