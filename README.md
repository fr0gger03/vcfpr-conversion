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
| **Python Libraries** | `requests`, `pyvmomi`, `rich` |
| **Zerto Permissions** | ZVMA / ZVM Keycloak Client ID & Client Secret |
| **vCenter Permissions** | Account with `Datastore.FileManagement` privileges |

### Installation

Install dependencies using `uv` or `pip`:

```bash
# Using uv (recommended)
uv add requests pyvmomi rich

# Or standard pip
pip install requests pyvmomi rich

```

---

## Repository Structure

* `01_zerto_discover_and_export.py` — Connects to Zerto ZVMA REST API, maps protected VMs to target ESXi hosts/VRAs, displays a formatted terminal table, and generates `zerto_seeds_manifest.json`.
* `02_vcf_seed_copy.py` — Imports the manifest, connects to vCenter via `pyVmomi`, creates standard seed folders, and executes server-side VMDK copies.

---

## Step-by-Step Usage

### Prerequisite: Setup Zerto Keycloak client token
See [keycloak.md](keycloak.md) for instructions.

### Step 1: Discover Zerto Replicas & Export Manifest

Run Script 1 while Zerto protection is still active.

1. Open `01_zerto_discover_and_export.py` and update the Zerto ZVMA parameters:
```python
ZVM_IP = "10.42.7.150"
ZVM_CLIENT_ID = "zerto-python-script"
ZVM_CLIENT_SECRET = "YourClientSecretHere"

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

1. Open `02_vcf_seed_copy.py` and update your vCenter credentials:
```python
VCENTER_IP = "10.42.7.200"
VCENTER_USER = "administrator@vsphere.local"
VCENTER_PASSWORD = "YourVcenterPasswordHere"
DATACENTER_NAME = "Datacenter-DR"

```


2. Execute the copy script:
```bash
uv run 02_vcf_seed_copy.py

```

3. Confirm the prompt for each disk. The script creates the target `[Datastore] VM_Name` directory on vSAN/VMFS and initiates a `VirtualDiskManager` copy task.

![alt text](images/move-vmdk-04.png)

---

## Important Technical Notes

* **Server-Side Processing:** All VMDK copy operations take place 100% server-side within the vSphere ESXi storage stack. Large file transfers do **not** route over your local workstation or VPN connection.
* **vSAN Compatibility:** Script 2 automatically handles datastore bracket formatting (`[DatastoreName]`) and utilizes `VirtualDiskManager.CopyVirtualDisk_Task` to safely duplicate virtual disk descriptor files and vSAN storage objects.