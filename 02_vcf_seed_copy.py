#!/usr/bin/env python3

import json
import ssl

# from pyVim.connect import Disconnect, SmartConnect
from pyvim.connect import Disconnect, SmartConnect
from pyVmomi import vim
from rich.console import Console
from rich.prompt import Confirm

import config

# --- vCenter Configuration (loaded from .env — see .env.example) ---
try:
    _env = config.require(
        "VCENTER_IP",
        "VCENTER_USER",
        "VCENTER_PASSWORD",
        "VCENTER_DATACENTER",
    )
except config.ConfigError as exc:
    config.fail(str(exc))

VCENTER_IP = _env["VCENTER_IP"]
VCENTER_USER = _env["VCENTER_USER"]
VCENTER_PASSWORD = _env["VCENTER_PASSWORD"]
DATACENTER_NAME = _env["VCENTER_DATACENTER"]  # vCenter Datacenter Object Name
MANIFEST_FILE = config.MANIFEST_FILE
VERIFY_SSL = config.VERIFY_SSL

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
        ssl_context = (
            ssl.create_default_context()
            if VERIFY_SSL
            else ssl._create_unverified_context()
        )
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

            # Shared with 03_vmdk_descriptor_cleanup.py so both scripts agree
            # on exactly where the seed disk ends up.
            src_raw_path, dest_folder_path, dest_vmdk_path, vmdk_filename = (
                config.compute_dest_paths(ds_name, vm_name, item["source_raw_path"])
            )

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