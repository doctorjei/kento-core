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

    # Read state dir before we delete anything
    state_file = lxc_dir / "kento-state"
    state_dir = Path(state_file.read_text().strip()) if state_file.is_file() else lxc_dir

    # Stop if running
    result = subprocess.run(
        ["lxc-info", "-n", name, "-sH"],
        capture_output=True, text=True,
    )
    if result.returncode == 0 and "RUNNING" in result.stdout:
        print("Stopping container...")
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
    print(f"Container destroyed: {name}")
