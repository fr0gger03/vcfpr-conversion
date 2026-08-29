# SPDX-License-Identifier: Apache-2.0
"""Centralised, type-safe settings loader. Values come from environment variables /
a git-ignored .env file (see .env.example). Fields are optional here since different
CLI subcommands need different subsets; use `require()` to validate per-command."""

from pydantic_settings import BaseSettings, SettingsConfigDict

# Applied to every outbound requests call in the adapters so an unresponsive
# Zerto/RP4VM/VR Gateway endpoint can't hang a CLI run indefinitely.
DEFAULT_HTTP_TIMEOUT_SECONDS = 30


class ConfigError(RuntimeError):
    """Raised when a command's required configuration is missing."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Zerto ZVMA
    zvm_ip: str | None = None
    zvm_client_id: str | None = None
    zvm_client_secret: str | None = None

    # Dell RecoverPoint for VMs plugin server
    rp4vm_ip: str | None = None
    rp4vm_user: str | None = None
    rp4vm_password: str | None = None

    # vCenter / VCF Protection & Recovery (vSphere Replication REST API Gateway)
    vcenter_ip: str | None = None
    vcenter_user: str | None = None
    vcenter_password: str | None = None
    vcenter_datacenter: str | None = None
    vr_gateway_ip: str | None = None  # defaults to vcenter_ip if unset

    # Shared
    manifest_file: str = "manifest.json"
    verify_ssl: bool = False
    vr_pairing_id: str | None = None  # vSphere Replication REST API Gateway pairing ID


def require(settings: Settings, *names: str) -> dict[str, str]:
    """Fetch several required settings at once, reporting all missing ones together."""
    values: dict[str, str] = {}
    missing: list[str] = []
    for name in names:
        value = getattr(settings, name, None)
        if not value:
            missing.append(name.upper())
        values[name] = value or ""
    if missing:
        raise ConfigError(
            "Missing required setting(s): "
            + ", ".join(missing)
            + ". Copy .env.example to .env and populate them, or export in your shell."
        )
    return values


def compute_dest_paths(
    ds_name: str, vm_name: str, source_raw_path: str
) -> tuple[str, str, str, str]:
    """Derive the "[Datastore] VM_Name/VMDK" seed path convention shared by the VCF
    adapter's seed-copy and descriptor-cleanup steps. Returns:
        (normalized_src_path, dest_folder_path, dest_vmdk_path, vmdk_filename)
    """
    normalized_src_path = source_raw_path
    if not normalized_src_path.startswith("["):
        normalized_src_path = f"[{ds_name}] {normalized_src_path}"

    dest_folder_path = f"[{ds_name}] {vm_name}"
    vmdk_filename = normalized_src_path.rsplit("/", 1)[-1]
    dest_vmdk_path = f"[{ds_name}] {vm_name}/{vmdk_filename}"

    return normalized_src_path, dest_folder_path, dest_vmdk_path, vmdk_filename
