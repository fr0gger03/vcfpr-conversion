# Zerto to VCF Protection & Recovery Seed Migration Toolset

This repository provides a two-phase automation workflow for converting active Zerto replica VMDKs into clean, vSphere-native seed disks for **VMware Cloud Foundation (VCF) Protection & Recovery** or **vSphere Replication**.

By copying Zerto target disks into standard datastore folders (`[Datastore] VM_Name/VMDK`), you can eliminate full-resynchronization over the network and drastically speed up disaster recovery cutovers.

---

## Architecture & Workflow Overview

```
┌────────────────────────────────┐
│ 1. Active Zerto Replication    │ ──► Run 01_zerto_discover_and_export.py
└────────────────────────────────┘     Exports zerto_seeds_manifest.json
               │
               ▼
┌────────────────────────────────┐
│ 2. Unprotect / Delete VPG      │ ──► Manual Step in Zerto UI
└────────────────────────────────┘     Option: "Keep target disks" (Releases locks)
               │
               ▼
┌────────────────────────────────┐
│ 3. Execute vSphere Seed Copy   │ ──► Run 02_vcf_seed_copy.py
└────────────────────────────────┘     Server-side VMDK copy on vSAN/VMFS

```

---

## Prerequisites

| Requirement | Details |
| --- | --- |
| **Python Engine** | Python 3.10+ (Managed via `uv` or standard `pip`) |
| **Python Libraries** | `requests`, `pyvmomi`, `rich`, `python-dotenv` |
| **Zerto Permissions** | ZVMA / ZVM Keycloak Client ID & Client Secret |
| **vCenter Permissions** | Account with `Datastore.FileManagement` privileges |

### Installation

Install dependencies using `uv` or `pip`:

```bash
# Using uv (recommended)
uv sync

# Or standard pip
pip install requests pyvmomi rich python-dotenv

```

---

## Configuration

All connection details and credentials are read from environment variables loaded
from a local `.env` file. **No credentials are stored in the scripts.**

```bash
cp .env.example .env
# then edit .env with your own values

```

`.env` is listed in `.gitignore` and must never be committed. `.env.example` is the
committed template containing placeholders only.

| Variable | Used by | Description |
| --- | --- | --- |
| `ZVM_IP` | Script 1 | Zerto ZVMA IP or FQDN (no scheme) |
| `ZVM_CLIENT_ID` | Script 1 | Keycloak OAuth client ID (see [keycloak.md](keycloak.md)) |
| `ZVM_CLIENT_SECRET` | Script 1 | Keycloak OAuth client secret |
| `VCENTER_IP` | Script 2 | vCenter Server IP or FQDN (no scheme) |
| `VCENTER_USER` | Script 2 | vCenter account with `Datastore.FileManagement` |
| `VCENTER_PASSWORD` | Script 2 | vCenter account password |
| `VCENTER_DATACENTER` | Script 2 | vCenter Datacenter object name |
| `MANIFEST_FILE` | Both | Optional. Manifest filename (default `zerto_seeds_manifest.json`) |
| `VERIFY_SSL` | Both | Optional. `true` to enforce TLS verification (default `false` for self-signed certs) |

Environment variables exported in your shell (or injected by a secret manager
such as `op run --env-file=.env -- uv run ...`) take precedence over the `.env` file,
so credentials never have to touch disk if you prefer.

Both scripts fail fast with a clear message listing every missing variable.

---

## Repository Structure

* `01_zerto_discover_and_export.py` — Connects to Zerto ZVMA REST API, maps protected VMs to target ESXi hosts/VRAs, displays a formatted terminal table, and generates `zerto_seeds_manifest.json`.
* `02_vcf_seed_copy.py` — Imports the manifest, connects to vCenter via `pyVmomi`, creates standard seed folders, and executes server-side VMDK copies.
* `config.py` — Shared `.env` loader and required-variable validation.
* `.env.example` — Template for the git-ignored `.env` file.

---

## Step-by-Step Usage

### Prerequisite: Setup Zerto Keycloak client token
See [keycloak.md](keycloak.md) for instructions.

### Step 1: Discover Zerto Replicas & Export Manifest

Run Script 1 while Zerto protection is still active.

1. Set the Zerto ZVMA parameters in your `.env` file:
```bash
ZVM_IP=your.zvm.ip.or.fqdn
ZVM_CLIENT_ID=zerto-python-script
ZVM_CLIENT_SECRET=YourClientSecretHere

```


2. Run the discovery script:
```bash
uv run 01_zerto_discover_and_export.py

```
![alt text](images/move-vmdk-01.png)

![alt text](images/move-vmdk-02.png)

3. Inspect the terminal output and verify that `zerto_seeds_manifest.json` has been created.

### Step 2: Release File Locks in Zerto

Before moving or copying replica files, ESXi file locks held by Zerto VRAs must be released:

1. Open the **Zerto Management Interface**.
2. Select the VPG(s) to migrate and click **Delete VPG** (or remove specific VMs).
3. **CRITICAL:** Select **"Keep target disks"** when prompted.
4. Confirm deletion. Zerto will detach the VMDKs from the target VRA.

![alt text](images/zerto-delete-vpg.png)

### Step 3: Copy VMDKs to VCF Seed Directories

Examine your vSphere datastore to confirm current location of replica disks


Run Script 2 after Zerto has released the file locks.

1. Set your vCenter credentials in the same `.env` file:
```bash
VCENTER_IP=your.vcenter.ip.or.fqdn
VCENTER_USER=administrator@vsphere.local
VCENTER_PASSWORD=YourVcenterPasswordHere
VCENTER_DATACENTER=Datacenter-DR

```


2. Execute the copy script:
```bash
uv run 02_vcf_seed_copy.py

```

3. Confirm the prompt for each disk. The script creates the target `[Datastore] VM_Name` directory on vSAN/VMFS and initiates a `VirtualDiskManager` copy task.

![alt text](images/move-vmdk-04.png)

---

## Important Technical Notes

* **Credential Handling:** Credentials live only in the git-ignored `.env` file (or your shell/secret manager). If you ever committed real credentials to this repository, rotate the Zerto Keycloak client secret and the vCenter account password — removing them from the working tree does not remove them from git history.
* **Server-Side Processing:** All VMDK copy operations take place 100% server-side within the vSphere ESXi storage stack. Large file transfers do **not** route over your local workstation or VPN connection.
* **vSAN Compatibility:** Script 2 automatically handles datastore bracket formatting (`[DatastoreName]`) and utilizes `VirtualDiskManager.CopyVirtualDisk_Task` to safely duplicate virtual disk descriptor files and vSAN storage objects.