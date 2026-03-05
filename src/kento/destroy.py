"""Destroy a kento-managed LXC container."""

import shutil
import subprocess
import sys

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
    image = (lxc_dir / "kento-image").read_text().strip()
    subprocess.run(
        ["podman", "image", "unmount", image],
        capture_output=True,
    )

    shutil.rmtree(lxc_dir)
    print(f"Container destroyed: {name}")
