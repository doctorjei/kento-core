"""Reset a kento-managed LXC container to clean OCI state."""

import shutil
import subprocess
import sys
from pathlib import Path

from kento import LXC_BASE, require_root
from kento.hook import write_hook
from kento.layers import resolve_layers


def reset(name: str) -> None:
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

    # Refuse if running
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
        stop_hint = f"pct stop {name}" if mode == "pve" else f"lxc-stop -n {name}"
        print(f"Error: container is running. Stop it first: {stop_hint}",
              file=sys.stderr)
        sys.exit(1)

    # Read state dir
    state_file = lxc_dir / "kento-state"
    state_dir = Path(state_file.read_text().strip()) if state_file.is_file() else lxc_dir

    # Unmount rootfs if mounted
    rootfs = lxc_dir / "rootfs"
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

    # Re-resolve layers from image and regenerate hook
    image = (lxc_dir / "kento-image").read_text().strip()
    layers = resolve_layers(image)
    (lxc_dir / "kento-layers").write_text(layers + "\n")
    write_hook(lxc_dir, layers, name, state_dir)

    print(f"Container reset: {name}")
    print("  Writable layer cleared, layers re-resolved from image.")
