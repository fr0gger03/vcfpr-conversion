# vcf-migrator

A modular ETL tool for migrating disaster-recovery protection from legacy engines
— **Zerto** and **Dell RecoverPoint for VMs** — to **VMware Cloud Foundation (VCF)
Protection & Recovery** (vSphere Replication).

The tool extracts protection topology (protection groups, VMs, disks, RPOs, boot
order, network mappings, IP customization) from the source engine into a validated
manifest, then reconstructs that protection on the VCF side. Critically, it reuses
the **existing replica VMDKs already sitting on the target datastore as seed data**,
so vSphere Replication can be configured with `use_seeds=true` and skip the full
baseline sync entirely — no multi-terabyte re-replication over the WAN.

## Architecture

```
  source adapters              engine                     target adapter
  (BaseDREngine)
┌────────────────────┐     ┌──────────────────┐     ┌────────────────────────┐
│ ZertoAdapter       │     │ transformer.py   │     │ VCFProtectionAdapter   │
│ RecoverPointAdapter│ ──► │  raw → Manifest  │ ──► │  seed copy (pyVmomi)   │
│                    │     │ validator.py     │     │  descriptor cleanup    │
│ discover / quiesce │     │  pre-flight      │     │  Configure Replication │
│ cleanup_source     │     │ cache.py (delta) │     │   (use_seeds=true)     │
└────────────────────┘     └──────────────────┘     └────────────────────────┘
                               ▲
                    ┌──────────┴───────────┐
                    │ src/cli.py   src/ui/ │
                    └──────────────────────┘
```

### Adapter contract
All engines implement `BaseDREngine` (`src/adapters/base.py`):
`authenticate()`, `discover_inventory()`, `export_protection_manifest()`,
`quiesce_replication()`, `cleanup_source()`.

* `ZertoAdapter` (`src/adapters/zerto.py`) — ZVMA Keycloak OAuth auth, batched
  `/v1/vras|vms|volumes` discovery, `POST /v1/vpgs/{id}/pause` to quiesce, and
  VPG deletion with target disks preserved (`keep_target_disks=True`).
* `RecoverPointAdapter` (`src/adapters/recoverpoint.py`) — RP4VM 5.3+ REST API:
  session auth, consistency-group/VM discovery, pause-transfer and
  remove-VM-from-CG for source cleanup.
* `VCFProtectionAdapter` (`src/adapters/vcf_target.py`) — pyVmomi server-side
  `VirtualDiskManager.CopyVirtualDisk_Task` seed copy into the
  `[Datastore] VM_Name/VMDK` convention, VMDK descriptor cleanup (strips stale
  `ddb.iofilters` / `ddb.sidecars`), and vSphere Replication REST API Gateway
  calls (`/api/rest/vr/{version}/...`) to `Configure Replication` with
  `use_seeds` + per-disk `destination_path`. Cross-site calls additionally
  require `POST .../pairings/{pairing_id}/remote-session`; the same
  `x-dr-session` header is reused throughout.

### Manifest
`src/models/manifest.py` defines the engine-neutral, versioned pydantic schema
that decouples source discovery from target provisioning: `ManifestMetadata`
(source engine, cluster ID, extraction timestamp), `ProtectionGroup` (RPO, boot
priority, startup delay, member VM IDs), `VirtualMachine`, `Disk` (capacity,
controller index, `seed_file_path`), `NetworkMapping`, and `IPCustomization`.
Validation happens at parse time, so a malformed manifest fails before any
vSphere object is touched.

`Disk` also carries optional `source_datastore`/`source_raw_path`: adapters
whose replicas must be physically relocated before they can serve as seeds
(Zerto) populate these; adapters whose replicas already sit at their final
destination (RecoverPoint) leave them unset. `provision` uses their presence
to decide whether a given disk needs an actual seed copy first.

### Engine
* `src/engine/transformer.py` — normalizes raw adapter payloads into a validated
  `Manifest` and resolves each disk's `seed_file_path`.
* `src/engine/validator.py` — pre-flight checks consumed identically by the CLI
  and the UI: seed disk path existence, datastore capacity, RDM conflicts,
  missing network mappings, and a **seed geometry check** comparing each disk's
  declared `capacity_bytes` to the copied seed VMDK's actual size (a mismatch
  makes `Configure Replication` with `use_seeds=true` fail unrecoverably).

### Caching & delta execution
`src/cache.py` maintains two JSON files:
* `.cache/inventory.json` — raw discovery payloads keyed by `engine:cluster_id`
  with a TTL, so repeated runs don't re-hit source APIs.
* `.cache/migration_state.json` — per-protection-group SHA-256 content hash,
  status, and timestamp. `provision` skips groups already marked `PROVISIONED`
  whose content hash is unchanged, making runs resumable; `rollback` uses the
  same file to find groups marked `FAILED` and revert them.

### Web UI
`src/ui/` is a FastAPI app (launched via `vcf-migrator ui`) with three surfaces,
each accepting an optional `?manifest_path=` query parameter (defaults to
`$MANIFEST_FILE`):

* **Mapping Matrix** — `GET /mappings` (HTML), `GET /api/mappings` (JSON),
  `POST /api/mappings/{source_network}` (JSON update), `GET /mappings/update`
  (browser-form edit). Edits persist back to the manifest file.
* **Pre-Flight Dashboard** — `GET /preflight` (HTML), `GET /api/preflight`
  (JSON), both backed by `engine/validator.run_preflight_checks()`.
* **Migration Console** — `GET /console` (HTML + live view),
  `GET /console/stream` (Server-Sent Events), `POST /console/publish`,
  `POST /console/simulate` (canned demo progress messages).

The dashboard currently uses a permissive stub vCenter session
(`src/ui/vcenter_session.py`), so only the network-mapping check is
meaningful there today; `validate`/`provision` in the CLI use the real
pyVmomi-backed session and catch everything.

## Requirements

* Python 3.11+ and [`uv`](https://docs.astral.sh/uv/)
* Zerto: ZVMA/ZVM Keycloak client ID + secret (see [keycloak.md](keycloak.md))
* RecoverPoint: RP4VM plugin-server account
* vCenter: account with `Datastore.FileManagement`; access to the vSphere
  Replication REST API Gateway
* Docker (optional) — only for the `testcontainers`-based adapter tests

## Installation

```bash
uv sync --extra dev
```

The `dev` extra adds `pytest`, `pytest-asyncio`, `httpx`, and `testcontainers`.
Omit `--extra dev` for a runtime-only install.

## Configuration

All credentials and endpoints come from environment variables, loaded from a
git-ignored `.env` file via `src/config.py` (`pydantic-settings`). Shell-exported
variables — or a secret manager, e.g. `op run --env-file=.env -- uv run ...` —
take precedence over the file, so credentials never need to touch disk.

```bash
cp .env.example .env
# edit .env
```

| Variable | Description |
| --- | --- |
| `ZVM_IP` | Zerto ZVMA IP or FQDN (no scheme) |
| `ZVM_CLIENT_ID` | Keycloak OAuth client ID (see [keycloak.md](keycloak.md)) |
| `ZVM_CLIENT_SECRET` | Keycloak OAuth client secret |
| `RP4VM_IP` | RecoverPoint for VMs plugin server IP or FQDN |
| `RP4VM_USER` / `RP4VM_PASSWORD` | RP4VM plugin server credentials |
| `VCENTER_IP` | vCenter Server IP or FQDN (no scheme) |
| `VCENTER_USER` / `VCENTER_PASSWORD` | vCenter account with `Datastore.FileManagement` |
| `VCENTER_DATACENTER` | vCenter Datacenter object holding the target datastores |
| `VR_GATEWAY_IP` | vSphere Replication REST API Gateway; defaults to `VCENTER_IP` |
| `VR_PAIRING_ID` | VR Gateway site-pairing ID used by `provision` (or pass `--pairing-id`) |
| `MANIFEST_FILE` | Manifest path written by `export`, read by `validate`/`provision` (default `manifest.json`) |
| `VERIFY_SSL` | `true` to enforce TLS verification (default `false` for self-signed certs) |

Each subcommand validates only the variables it needs and fails fast listing
every missing one.

## Usage

```bash
# 1. Discover source protection topology and write the manifest.
#    Run while source replication is still active.
uv run vcf-migrator export --source zerto
uv run vcf-migrator export --source recoverpoint

# 2. Pre-flight the manifest against the target (no changes made).
uv run vcf-migrator validate

# 3. Copy seeds, clean descriptors, and configure VCF replication.
uv run vcf-migrator provision --dry-run   # print planned actions only
uv run vcf-migrator provision

# 4. Revert target-side replication configs for partially provisioned groups.
uv run vcf-migrator rollback --manifest manifest.json

# Web UI (mapping matrix, pre-flight dashboard, migration console)
uv run vcf-migrator ui
```

### CLI reference

| Command | Key options | Notes |
| --- | --- | --- |
| `export` | `--source {zerto,recoverpoint}` (required), `--cluster-id` (default `default`) | Writes `MANIFEST_FILE` |
| `validate` | `--manifest <path>` (defaults to `$MANIFEST_FILE`) | Exits non-zero if any check fails |
| `provision` | `--manifest <path>`, `--dry-run`, `--pairing-id` (defaults to `$VR_PAIRING_ID`) | Copies seeds and configures replication per group; skips groups already `PROVISIONED` and unchanged; continues past per-group failures (marking them `FAILED`) and exits non-zero if any occurred |
| `rollback` | `--manifest <path>` (required) | Reverts target replication config for every group cached as `FAILED` |
| `ui` | `--host` (default `127.0.0.1`), `--port` (default `8000`) | Launches the FastAPI web UI |

### Workflow notes

**Export.** `export` reads the source engine's inventory and writes
`MANIFEST_FILE`. Review the reported protection groups, VMs, and disk paths
before continuing.

![Zerto discovery output](images/move-vmdk-01.png)

![Zerto discovery output, continued](images/move-vmdk-02.png)

**Release source file locks.** Replica VMDKs are locked by the source engine's
replication appliances (Zerto VRAs, vRPAs) and cannot be copied until released.
`cleanup_source()` performs this via the source API and **defaults to preserving
the target disks**. The equivalent manual step in the Zerto UI is *Delete VPG*
with **"Keep the recovery disks at the peer site"** selected — the seed disks must
survive, or there is nothing to seed from.

![Zerto delete VPG, keeping target disks](images/zerto-delete-vpg.png)

**Provision.** Seed copies run entirely server-side inside the ESXi storage
stack (`VirtualDiskManager.CopyVirtualDisk_Task`), so no bulk data crosses your
workstation or VPN. Copied descriptors then have stale `ddb.iofilters` /
`ddb.sidecars` lines commented out — if the source replica had an I/O filter
attached (e.g. `vmwarelwd`), those lines reference sidecar files absent on the
target and cause `Cannot open the disk '...' or one of the snapshot disks it
depends on.` on power-on. See
[Broadcom KB 334555](https://knowledge.broadcom.com/external/article/334555/unable-to-power-on-vm-when-iofilters-is.html).
Only the small text descriptor is rewritten, never the `-flat.vmdk` extent.

![Seed copy in progress](images/move-vmdk-04.png)

Finally, each protection group is registered with vSphere Replication using the
copied disks as seeds. `provision` is idempotent — already-provisioned,
unchanged groups are skipped via `.cache/migration_state.json` — and resilient:
a failure on one group is recorded as `FAILED` and doesn't stop the rest of the
batch. Run `rollback --manifest <path>` afterward to revert any `FAILED` groups'
target replication config.

## Testing

```bash
uv run pytest                     # full suite
uv run pytest -m "not docker"     # skip tests needing a Docker daemon
```

`tests/test_zerto_adapter.py` and `tests/test_rp4vm_adapter.py` each have one
`@pytest.mark.docker` test that uses `testcontainers` to spin up a throwaway
HTTP container and confirm `authenticate()` surfaces failures instead of
silently proceeding. Everything else — manifest mapping, `engine.transformer`,
`engine.validator`'s checks (including missing-network-mapping and
seed-geometry-mismatch cases), and the FastAPI UI (`tests/test_ui.py`) — is
pure unit/`TestClient` testing and needs no Docker.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
