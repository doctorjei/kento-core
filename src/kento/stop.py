"""Stop a kento-managed container."""

import subprocess

from kento import require_root, resolve_container


def stop(name: str) -> None:
    require_root()

    lxc_dir = resolve_container(name)
    container_id = lxc_dir.name

    mode_file = lxc_dir / "kento-mode"
    mode = mode_file.read_text().strip() if mode_file.is_file() else "lxc"

    if mode == "pve":
        subprocess.run(["pct", "stop", container_id], check=True)
    else:
        subprocess.run(["lxc-stop", "-n", container_id], check=True)

    print(f"Stopped: {name}")
