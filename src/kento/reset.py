"""Reset a kento-managed container to clean OCI state."""

import shutil
import subprocess
import sys
from pathlib import Path

from kento import require_root, resolve_container, is_running
from kento.hook import write_hook
from kento.layers import resolve_layers


def reset(name: str) -> None:
    require_root()

    container_dir = resolve_container(name)

    # Detect mode (default lxc for containers created before mode tracking)
    mode_file = container_dir / "kento-mode"
    mode = mode_file.read_text().strip() if mode_file.is_file() else "lxc"

    # Refuse if running
    if is_running(container_dir, mode):
        print(f"Error: container is running. Stop it first: kento container stop {name}",
              file=sys.stderr)
        sys.exit(1)

    # Read state dir
    state_file = container_dir / "kento-state"
    state_dir = Path(state_file.read_text().strip()) if state_file.is_file() else container_dir

    # Unmount rootfs if mounted
    rootfs = container_dir / "rootfs"
    if subprocess.run(["mountpoint", "-q", str(rootfs)],
                      capture_output=True).returncode == 0:
        subprocess.run(["umount", str(rootfs)])

    # Clear writable layer
    upper = state_dir / "upper"
    work = state_dir / "work"
    if upper.exists():
        shutil.rmtree(upper)
    if work.exists():
        shutil.rmtree(work)
    upper.mkdir(parents=True)
    work.mkdir(parents=True)

    # Re-inject static IP config if present
    net_file = container_dir / "kento-net"
    if net_file.is_file():
        from kento.create import _inject_network_config
        net_cfg = {}
        for line in net_file.read_text().strip().splitlines():
            k, v = line.split("=", 1)
            net_cfg[k] = v
        _inject_network_config(state_dir, net_cfg["ip"],
                               net_cfg.get("gateway"), net_cfg.get("dns"))

    # Re-resolve layers from image
    image = (container_dir / "kento-image").read_text().strip()
    layers = resolve_layers(image, mode=mode)
    (container_dir / "kento-layers").write_text(layers + "\n")

    # Regenerate hook (LXC/PVE only — VM mode has no hook)
    if mode != "vm":
        write_hook(container_dir, layers, name, state_dir)

    print(f"Reset: {name}")
    print("  Writable layer cleared, layers re-resolved from image.")
