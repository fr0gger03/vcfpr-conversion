#!/usr/bin/env python3
import json
import requests
from rich.console import Console
from rich.table import Table

# --- ZVMA Configuration ---
ZVM_IP = "10.42.7.150"
ZVM_CLIENT_ID = "zerto-python-script"
ZVM_CLIENT_SECRET = "xxMCsl9izJbizLTvlqTEWKZTubPxOYoh"
MANIFEST_FILE = "zerto_seeds_manifest.json"

# Disable SSL warnings for self-signed certificates
requests.packages.urllib3.disable_warnings()
base_url = f"https://{ZVM_IP}"
console = Console()


def get_val(obj, key, default=None):
    """Case-insensitive dictionary getter for API variations."""
    if not isinstance(obj, dict):
        return default
    if key in obj:
        return obj[key]
    pascal = key[0].upper() + key[1:] if key else key
    if pascal in obj:
        return obj[pascal]
    camel = key[0].lower() + key[1:] if key else key
    if camel in obj:
        return obj[camel]
    return default


def main():
    console.rule("[bold cyan]Zerto Seed Discovery & Manifest Generator")

    # 1. Authenticate with Keycloak
    with console.status("[bold green]Authenticating with Zerto ZVMA..."):
        token_req = requests.post(
            f"{base_url}/auth/realms/zerto/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": ZVM_CLIENT_ID,
                "client_secret": ZVM_CLIENT_SECRET,
            },
            verify=False,
        )

    token_resp = token_req.json()
    if "access_token" not in token_resp:
        console.print(f"[bold red]Zerto Auth Failed:[/bold red] {token_resp}")
        exit(1)

    headers = {"Authorization": f"Bearer {token_resp['access_token']}"}

    # 2. Fetch Zerto Metadata
    with console.status("[bold cyan]Querying VRAs, VMs, and Volumes..."):
        vras_data = requests.get(
            f"{base_url}/v1/vras", headers=headers, verify=False
        ).json()
        vms_data = requests.get(
            f"{base_url}/v1/vms", headers=headers, verify=False
        ).json()
        volumes_data = requests.get(
            f"{base_url}/v1/volumes", headers=headers, verify=False
        ).json()

    # 3. Build Lookup Maps
    host_to_vra = {}
    for vra in vras_data:
        h_id = get_val(vra, "hostIdentifier")
        h_name = get_val(vra, "hostDisplayName")
        vra_name = get_val(vra, "vraName", "Unknown_VRA")
        if h_id:
            host_to_vra[h_id] = vra_name
        if h_name:
            host_to_vra[h_name] = vra_name

    vm_map = {}
    for vm in vms_data:
        vm_id = (
            get_val(vm, "vmIdentifier")
            or get_val(get_val(vm, "link"), "identifier")
            or get_val(vm, "identifier")
        )
        if vm_id:
            vm_map[vm_id] = {
                "vm_name": get_val(vm, "vmName", "Unknown_VM"),
                "vpg_name": get_val(vm, "vpgName", "Unknown_VPG"),
                "recovery_host_id": get_val(vm, "recoveryHostIdentifier"),
                "recovery_host_name": get_val(vm, "recoveryHostName"),
            }

    # 4. Process Recovery Volumes & Format Output
    manifest = []
    table = Table(
        title="Discovered Zerto Seed Replicas",
        show_lines=True,
        header_style="bold magenta",
    )
    table.add_column("VPG", style="cyan", no_wrap=True)
    table.add_column("VM Name", style="green")
    table.add_column("Target Datastore", style="yellow")
    table.add_column("Current Path (vCenter Display)", style="dim white")
    table.add_column("Destination Seed Path (vSphere Spec)", style="bold blue")

    for vol in volumes_data:
        if get_val(vol, "volumeType") == "Recovery":
            prot_vm = (
                get_val(vol, "protectedVm") or get_val(vol, "owningVm") or {}
            )
            vm_id = get_val(prot_vm, "identifier")
            vm_info = vm_map.get(vm_id, {})

            vm_name = vm_info.get("vm_name") or get_val(
                prot_vm, "name", "Unknown_VM"
            )
            vpg_name = vm_info.get("vpg_name") or get_val(
                get_val(vol, "vpg"), "name", "Unknown_VPG"
            )

            ds = get_val(vol, "datastore") or {}
            ds_name = get_val(ds, "name", "Unknown_DS")
            path = get_val(vol, "path") or {}
            raw_full = get_val(path, "full", "")
            raw_rel = get_val(path, "relative", "")
            vmdk_file = get_val(path, "fileName", "")

            # Ensure raw_full starts with [datastore]
            if raw_full and not raw_full.startswith("["):
                raw_full = f"[{ds_name}] {raw_full}"

            # Resolve VRA Appliance Name for Display
            rec_host_id = vm_info.get("recovery_host_id")
            rec_host_name = vm_info.get("recovery_host_name")
            vra_name = (
                host_to_vra.get(rec_host_id)
                or host_to_vra.get(rec_host_name)
                or "Z-VRA"
            )

            vsan_uuid = raw_rel.split("/")[0] if "/" in raw_rel else ""
            ui_display_path = (
                raw_full.replace(vsan_uuid, vra_name)
                if vsan_uuid
                else raw_full
            )

            # Strictly formatted vSphere seed path: [Datastore] VM_Name/VMDK_File
            seed_path = f"[{ds_name}] {vm_name}/{vmdk_file}"

            table.add_row(
                vpg_name, vm_name, ds_name, ui_display_path, seed_path
            )

            manifest.append(
                {
                    "vpg_name": vpg_name,
                    "vm_name": vm_name,
                    "datastore": ds_name,
                    "source_raw_path": raw_full,
                    "ui_display_path": ui_display_path,
                    "destination_seed_path": seed_path,
                }
            )

    console.print(table)

    # 5. Export Manifest JSON
    with open(MANIFEST_FILE, "w") as f:
        json.dump(manifest, f, indent=4)

    console.print(
        f"\n[bold green]✔ Manifest exported successfully to:[/bold green] [bold white]{MANIFEST_FILE}[/bold white]"
    )
    console.print(
        "[bold yellow]Next Step:[/bold yellow] Delete/unprotect VPG in Zerto (keep target disks), then run Script 2.\n"
    )


if __name__ == "__main__":
    main()