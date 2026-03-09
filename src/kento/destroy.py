"""Remove a kento-managed container."""

import shutil
import subprocess
import sys
from pathlib import Path

from kento import require_root, resolve_container, is_running


def destroy(name: str, force: bool = False) -> None:
    require_root()

    container_dir = resolve_container(name)
    container_id = container_dir.name

    # Detect mode (default lxc for containers created before mode tracking)
    mode_file = container_dir / "kento-mode"
    mode = mode_file.read_text().strip() if mode_file.is_file() else "lxc"

    # Read state dir before we delete anything
    state_file = container_dir / "kento-state"
    state_dir = Path(state_file.read_text().strip()) if state_file.is_file() else container_dir

    # Check if running
    running = is_running(container_dir, mode)

    if running and not force:
        print(f"Error: container {name} is running. "
              f"Use 'kento container rm -f {name}' to force removal.",
              file=sys.stderr)
        sys.exit(1)

    if running:
        print("Stopping container...")
        if mode == "vm":
            from kento.vm import stop_vm
            stop_vm(container_dir)
        elif mode == "pve":
            subprocess.run(["pct", "stop", container_id], check=True)
        else:
            subprocess.run(["lxc-stop", "-n", container_id], check=True)

    # Unmount rootfs if still mounted
    rootfs = container_dir / "rootfs"
    if subprocess.run(["mountpoint", "-q", str(rootfs)],
                      capture_output=True).returncode == 0:
        subprocess.run(["umount", str(rootfs)])

    # Release OCI image mount (LXC/PVE only)
    if mode != "vm":
        from kento.layers import _podman_cmd
        image = (container_dir / "kento-image").read_text().strip()
        subprocess.run(
            [*_podman_cmd(), "image", "unmount", image],
            capture_output=True,
        )

    # Remove state dir if separate from container_dir
    if state_dir != container_dir and state_dir.is_dir():
        shutil.rmtree(state_dir)

    shutil.rmtree(container_dir)

    # Clean up PVE config if applicable
    if mode == "pve":
        from kento.pve import delete_pve_config
        delete_pve_config(int(container_id))

    print(f"Removed: {name}")
