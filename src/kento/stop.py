"""Shut down a kento-managed container."""

import subprocess

from kento import require_root, resolve_container


def shutdown(name: str, *, force: bool = False) -> None:
    require_root()

    container_dir = resolve_container(name)
    container_id = container_dir.name

    mode_file = container_dir / "kento-mode"
    mode = mode_file.read_text().strip() if mode_file.is_file() else "lxc"

    if mode == "vm":
        from kento.vm import stop_vm
        stop_vm(container_dir)
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
