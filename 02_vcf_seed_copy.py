#!/usr/bin/env python3

import json
import ssl
from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim
from rich.console import Console
from rich.prompt import Confirm

# --- vCenter Configuration ---
VCENTER_IP = "10.42.7.200"
VCENTER_USER = "administrator@vsphere.local"
VCENTER_PASSWORD = "h$cilV2u64r7C@^9zTab"
DATACENTER_NAME = "consolidated-sandbox-dc01"  # vCenter Datacenter Object Name
MANIFEST_FILE = "zerto_seeds_manifest.json"

console = Console()


def wait_for_task(task):
    """Wait for a vSphere asynchronous task to complete."""
    while True:
        if task.info.state == "success":
            return True
        if task.info.state == "error":
            console.print(f"[bold red]Task Error:[/bold red] {task.info.error.msg}")
            return False


def main():
    console.rule("[bold cyan]vSphere Datastore Seed Copier")

    # 1. Load JSON Manifest
    try:
        with open(MANIFEST_FILE, "r") as f:
            manifest = json.load(f)
    except Exception as e:
        console.print(f"[bold red]Failed to load {MANIFEST_FILE}:[/bold red] {e}")
        return

    if not manifest:
        console.print("[yellow]Manifest is empty. Nothing to copy.[/yellow]")
        return

    console.print(f"Loaded [bold white]{len(manifest)}[/bold white] volume(s) from [bold white]{MANIFEST_FILE}[/bold white].")

    # 2. Connect to vCenter
    console.print(f"[bold green]Connecting to vCenter {VCENTER_IP}...[/bold green]")
    try:
        ssl_context = ssl._create_unverified_context()
        si = SmartConnect(
            host=VCENTER_IP,
            user=VCENTER_USER,
            pwd=VCENTER_PASSWORD,
            sslContext=ssl_context,
        )
    except Exception as e:
        console.print(f"[bold red]Failed to connect to vCenter:[/bold red] {e}")
        exit(1)

    try:
        content = si.RetrieveContent()
        file_mgr = content.fileManager
        vdisk_mgr = content.virtualDiskManager

        # Locate Datacenter Object
        datacenter = None
        for child in content.rootFolder.childEntity:
            if isinstance(child, vim.Datacenter) and child.name == DATACENTER_NAME:
                datacenter = child
                break

        if not datacenter:
            console.print(f"[bold red]Datacenter '{DATACENTER_NAME}' not found in vCenter![/bold red]")
            exit(1)

        # 3. Process Each Seed Copy
        for idx, item in enumerate(manifest, 1):
            ds_name = item["datastore"]
            vm_name = item["vm_name"]
            
            # Format explicitly as per your snippet: [datastore_name] folder/file
            src_raw_path = item["source_raw_path"]
            if not src_raw_path.startswith("["):
                src_raw_path = f"[{ds_name}] {src_raw_path}"

            # Destination Folder and File Paths
            dest_folder_path = f"[{ds_name}] {vm_name}"
            vmdk_filename = src_raw_path.rsplit("/", 1)[-1]
            dest_vmdk_path = f"[{ds_name}] {vm_name}/{vmdk_filename}"

            console.print(f"\n[bold underline]Volume {idx} of {len(manifest)}:[/bold underline]")
            console.print(f"  [cyan]VM Name:[/cyan]           {vm_name}")
            console.print(f"  [cyan]Source Path:[/cyan]       {src_raw_path}")
            console.print(f"  [cyan]Target Folder Path:[/cyan] {dest_folder_path}")
            console.print(f"  [cyan]Destination Seed:[/cyan]  {dest_vmdk_path}")

            if Confirm.ask(f"Copy seed VMDK for [bold green]{vm_name}[/bold green]?"):
                
                # --- STEP A: Create Folder using Key/Value Keyword Arguments ---
                console.print(f"  [dim]Creating folder: {dest_folder_path}[/dim]")
                try:
                    file_mgr.MakeDirectory(
                        name=dest_folder_path,
                        datacenter=datacenter,
                        createParentDirectories=True,
                    )
                    console.print("  [green]✔ Folder created successfully.[/green]")
                except Exception as ex:
                    # Ignore if directory already exists
                    if "already exists" in str(ex).lower():
                        console.print("  [dim yellow]Folder already exists.[/dim yellow]")
                    else:
                        console.print(f"  [yellow]MakeDirectory notice:[/yellow] {ex}")

                # --- STEP B: Copy Virtual Disk ---
                with console.status(f"[bold magenta]Copying Virtual Disk for {vm_name}..."):
                    copy_task = vdisk_mgr.CopyVirtualDisk_Task(
                        sourceName=src_raw_path,
                        sourceDatacenter=datacenter,
                        destName=dest_vmdk_path,
                        destDatacenter=datacenter,
                        force=False,
                    )
                    success = wait_for_task(copy_task)

                if success:
                    console.print(f"[bold green]✔ Successfully copied seed VMDK for {vm_name}![/bold green]")
            else:
                console.print(f"[yellow]Skipped copy for {vm_name}.[/yellow]")

    finally:
        Disconnect(si)
        console.print("\n[bold blue]Disconnected from vCenter. Execution complete.[/bold blue]\n")


if __name__ == "__main__":
    main()