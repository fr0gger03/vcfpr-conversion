# SPDX-License-Identifier: Apache-2.0
"""vcf-migrator CLI: export, validate, provision, rollback, ui."""

import sys

import click

from src import cache
from src.adapters.recoverpoint import RecoverPointAdapter
from src.adapters.vcf_target import VCFProtectionAdapter
from src.adapters.zerto import ZertoAdapter
from src.config import ConfigError, Settings
from src.engine.validator import CheckResult, run_preflight_checks
from src.models.manifest import Manifest

SOURCE_ADAPTERS = {"zerto": ZertoAdapter, "recoverpoint": RecoverPointAdapter}


def _load_manifest(path: str) -> Manifest:
    try:
        return Manifest.model_validate_json(open(path).read())
    except FileNotFoundError:
        raise click.ClickException(f"Manifest not found: {path}. Run `export` first.")


def _write_manifest(path: str, manifest: Manifest) -> None:
    with open(path, "w") as f:
        f.write(manifest.model_dump_json(indent=2))


def _print_check_results(results: list[CheckResult]) -> None:
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        click.echo(f"[{mark}] {r.check_name}: {r.message}")


def _connect_vcf(settings: Settings) -> VCFProtectionAdapter:
    adapter = VCFProtectionAdapter(settings)
    try:
        adapter.authenticate()
    except ConfigError as exc:
        raise click.ClickException(str(exc))
    return adapter


@click.group()
def cli() -> None:
    """Migrate DR protection from Zerto/RecoverPoint for VMs to VCF Protection & Recovery."""


@cli.command()
@click.option("--source", required=True, type=click.Choice(sorted(SOURCE_ADAPTERS)))
@click.option("--cluster-id", default="default", show_default=True)
def export(source: str, cluster_id: str) -> None:
    """Discover source protection topology and write the manifest."""
    settings = Settings()
    adapter = SOURCE_ADAPTERS[source](settings, cluster_id)
    try:
        adapter.authenticate()
        inventory = adapter.discover_inventory()
        manifest = adapter.export_protection_manifest(inventory)
    except ConfigError as exc:
        raise click.ClickException(str(exc))
    _write_manifest(settings.manifest_file, manifest)
    click.echo(
        f"Exported {len(manifest.protection_groups)} protection group(s), "
        f"{len(manifest.virtual_machines)} VM(s) to {settings.manifest_file}"
    )


@cli.command()
@click.option("--manifest", "manifest_path", default=None, help="Defaults to $MANIFEST_FILE.")
def validate(manifest_path: str | None) -> None:
    """Run pre-flight checks for a manifest against the target vCenter."""
    settings = Settings()
    manifest = _load_manifest(manifest_path or settings.manifest_file)
    adapter = _connect_vcf(settings)
    try:
        results = run_preflight_checks(manifest, adapter)
    finally:
        adapter.disconnect()
    _print_check_results(results)
    if any(not r.passed for r in results):
        sys.exit(1)


@cli.command()
@click.option("--manifest", "manifest_path", default=None, help="Defaults to $MANIFEST_FILE.")
@click.option("--dry-run", is_flag=True, help="Print planned actions without changing anything.")
@click.option("--pairing-id", default=None, help="VR Gateway pairing ID. Defaults to $VR_PAIRING_ID.")
def provision(manifest_path: str | None, dry_run: bool, pairing_id: str | None) -> None:
    """Copy seeds, clean descriptors, and configure VCF replication for each group.

    Skips groups already marked PROVISIONED in .cache/migration_state.json whose
    content hash is unchanged (delta execution). Run `validate` first to confirm
    seed disks exist and match the manifest's declared sizes.
    """
    settings = Settings()
    manifest = _load_manifest(manifest_path or settings.manifest_file)
    pairing_id = pairing_id or settings.vr_pairing_id
    if not dry_run and not pairing_id:
        raise click.ClickException("Missing VR Gateway pairing ID: pass --pairing-id or set VR_PAIRING_ID.")

    vm_names = {vm.vm_id: vm.name for vm in manifest.virtual_machines}
    adapter = None if dry_run else _connect_vcf(settings)
    failures = 0
    try:
        for group in manifest.protection_groups:
            content = group.model_dump()
            if cache.is_unchanged_and_provisioned(group.name, content):
                click.echo(f"[skip] {group.name}: already provisioned, unchanged.")
                continue

            group_disks = [d for d in manifest.disks if d.vm_id in group.vm_ids]
            if dry_run:
                click.echo(f"[dry-run] would provision {group.name} ({len(group_disks)} disk(s))")
                continue

            try:
                for disk in group_disks:
                    if disk.source_datastore and disk.source_raw_path:
                        # Physically relocate the replica (e.g. Zerto). Adapters whose
                        # replicas already sit at seed_file_path (e.g. RecoverPoint)
                        # leave these fields unset and skip this step.
                        adapter.copy_and_prepare_seed(
                            disk.source_datastore, vm_names.get(disk.vm_id, disk.vm_id), disk.source_raw_path
                        )
                adapter.provision_protection_group(group, group_disks, pairing_id=pairing_id)
            except Exception as exc:  # noqa: BLE001 - one bad group shouldn't abort the batch
                cache.set_group_state(group.name, status="FAILED", content=content)
                click.echo(f"[fail] {group.name}: {exc}")
                failures += 1
                continue

            cache.set_group_state(group.name, status="PROVISIONED", content=content)
            click.echo(f"[done] {group.name}: provisioned.")
    finally:
        if adapter is not None:
            adapter.disconnect()
    if failures:
        sys.exit(1)


@cli.command()
@click.option("--manifest", "manifest_path", required=True)
def rollback(manifest_path: str) -> None:
    """Revert target replication config for groups left FAILED by a `provision` run."""
    settings = Settings()
    manifest = _load_manifest(manifest_path)
    adapter = _connect_vcf(settings)
    reverted = 0
    try:
        for group in manifest.protection_groups:
            state = cache.get_group_state(group.name)
            if not state or state.get("status") != "FAILED":
                continue
            adapter.cleanup_source(group.name, keep_target_disks=True)
            cache.set_group_state(group.name, status="ROLLED_BACK", content=group.model_dump())
            click.echo(f"[rolled-back] {group.name}")
            reverted += 1
    finally:
        adapter.disconnect()
    click.echo(f"Reverted {reverted} group(s).")


@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8000, show_default=True)
def ui(host: str, port: int) -> None:
    """Launch the FastAPI web UI (mapping matrix, pre-flight dashboard, migration console)."""
    import uvicorn

    uvicorn.run("src.ui.app:create_app", host=host, port=port, factory=True)


if __name__ == "__main__":
    cli()
