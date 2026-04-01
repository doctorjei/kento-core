"""Start a kento-managed container."""

import subprocess
from pathlib import Path

from kento import read_mode, require_root, resolve_container


def start(name: str, *, container_dir: Path | None = None, mode: str | None = None) -> None:
    require_root()

    if container_dir is None:
        container_dir = resolve_container(name)
    container_id = container_dir.name

    if mode is None:
        mode = read_mode(container_dir)

    if mode == "vm":
        from kento.vm import start_vm
        start_vm(container_dir, name)
        return
    elif mode == "pve-vm":
        vmid = (container_dir / "kento-vmid").read_text().strip()
        subprocess.run(["qm", "start", vmid], check=True)
    elif mode == "pve":
        subprocess.run(["pct", "start", container_id], check=True)
    else:
        subprocess.run(["lxc-start", "-n", container_id], check=True)

    print(f"Started: {name}")
