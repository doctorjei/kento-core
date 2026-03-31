"""Start a kento-managed container."""

import subprocess

from kento import require_root, resolve_container


def start(name: str) -> None:
    require_root()

    container_dir = resolve_container(name)
    container_id = container_dir.name

    mode_file = container_dir / "kento-mode"
    mode = mode_file.read_text().strip() if mode_file.is_file() else "lxc"

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
