"""Reset a kento-managed LXC container to clean OCI state."""

import shutil
import subprocess
import sys

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

    # Refuse if running
    result = subprocess.run(
        ["lxc-info", "-n", name, "-sH"],
        capture_output=True, text=True,
    )
    if result.returncode == 0 and "RUNNING" in result.stdout:
        print(f"Error: container is running. Stop it first: lxc-stop -n {name}",
              file=sys.stderr)
        sys.exit(1)

    # Unmount rootfs if mounted
    rootfs = lxc_dir / "rootfs"
    if subprocess.run(["mountpoint", "-q", str(rootfs)],
                      capture_output=True).returncode == 0:
        subprocess.run(["umount", str(rootfs)])

    # Clear writable layer
    upper = lxc_dir / "upper"
    work = lxc_dir / "work"
    if upper.exists():
        shutil.rmtree(upper)
    if work.exists():
        shutil.rmtree(work)
    upper.mkdir()
    work.mkdir()

    # Re-resolve layers from image and regenerate hook
    image = (lxc_dir / "kento-image").read_text().strip()
    layers = resolve_layers(image)
    (lxc_dir / "kento-layers").write_text(layers + "\n")
    write_hook(lxc_dir, layers, name)

    print(f"Container reset: {name}")
    print("  Writable layer cleared, layers re-resolved from image.")
