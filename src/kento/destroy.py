"""Destroy a kento-managed LXC container."""

import shutil
import subprocess
import sys
from pathlib import Path

from kento import LXC_BASE, require_root


def destroy(name: str) -> None:
    require_root()

    lxc_dir = LXC_BASE / name

    if not lxc_dir.is_dir():
        print(f"Error: container not found: {name}", file=sys.stderr)
        sys.exit(1)

    if not (lxc_dir / "kento-image").is_file():
        print(f"Error: {name} is not a kento-managed container",
              file=sys.stderr)
        sys.exit(1)

    # Detect mode (default lxc for containers created before mode tracking)
    mode_file = lxc_dir / "kento-mode"
    mode = mode_file.read_text().strip() if mode_file.is_file() else "lxc"

    # Read state dir before we delete anything
    state_file = lxc_dir / "kento-state"
    state_dir = Path(state_file.read_text().strip()) if state_file.is_file() else lxc_dir

    # Stop if running
    if mode == "pve":
        result = subprocess.run(
            ["pct", "status", name],
            capture_output=True, text=True,
        )
        running = result.returncode == 0 and "running" in result.stdout
    else:
        result = subprocess.run(
            ["lxc-info", "-n", name, "-sH"],
            capture_output=True, text=True,
        )
        running = result.returncode == 0 and "RUNNING" in result.stdout

    if running:
        print("Stopping container...")
        if mode == "pve":
            subprocess.run(["pct", "stop", name], check=True)
        else:
            subprocess.run(["lxc-stop", "-n", name], check=True)

    # Unmount rootfs if mounted
    rootfs = lxc_dir / "rootfs"
    if subprocess.run(["mountpoint", "-q", str(rootfs)],
                      capture_output=True).returncode == 0:
        subprocess.run(["umount", str(rootfs)])

    # Release OCI image mount
    from kento.layers import _podman_cmd
    image = (lxc_dir / "kento-image").read_text().strip()
    subprocess.run(
        [*_podman_cmd(), "image", "unmount", image],
        capture_output=True,
    )

    # Remove state dir if separate from lxc_dir
    if state_dir != lxc_dir and state_dir.is_dir():
        shutil.rmtree(state_dir)

    shutil.rmtree(lxc_dir)

    # Clean up PVE config if applicable
    if mode == "pve":
        from kento.pve import delete_pve_config
        delete_pve_config(int(name))

    print(f"Container destroyed: {name}")
