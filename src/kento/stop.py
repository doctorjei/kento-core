"""Shut down a kento-managed instance."""

import subprocess
from pathlib import Path

from kento import read_mode, require_root, resolve_container


def shutdown(name: str, *, force: bool = False, container_dir: Path | None = None, mode: str | None = None) -> None:
    require_root()

    if container_dir is None:
        container_dir = resolve_container(name)
    container_id = container_dir.name

    if mode is None:
        mode = read_mode(container_dir)

    if mode == "vm":
        from kento.vm import stop_vm
        stop_vm(container_dir, force=force)
    elif mode == "pve-vm":
        vmid = (container_dir / "kento-vmid").read_text().strip()
        if force:
            subprocess.run(["qm", "stop", vmid], check=True)
        else:
            subprocess.run(["qm", "shutdown", vmid], check=True)
    elif mode == "pve":
        if force:
            subprocess.run(["pct", "stop", container_id], check=True)
        else:
            subprocess.run(["pct", "shutdown", container_id], check=True)
    else:
        cmd = ["lxc-stop", "-n", container_id]
        if force:
            cmd.append("-k")
        subprocess.run(cmd, check=True)

    action = "Stopped" if force else "Shut down"
    print(f"{action}: {name}")


# Alias for backward compatibility
stop = shutdown
