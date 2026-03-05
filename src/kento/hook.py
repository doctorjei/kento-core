"""Generate per-container LXC hook scripts."""

from pathlib import Path


def generate_hook(lxc_dir: Path, layers: str, name: str) -> str:
    """Return a hook script with baked-in paths for a container."""
    return f"""#!/bin/sh
set -eu

NAME="{name}"
LXC_DIR="{lxc_dir}"
LAYERS="{layers}"
HOOK_TYPE="${{LXC_HOOK_TYPE:-$3}}"

case "$HOOK_TYPE" in
    pre-start)
        # Validate layer paths still exist (image may have changed)
        IFS=:
        for dir in $LAYERS; do
            if [ ! -d "$dir" ]; then
                echo "Error: layer path missing: $dir" >&2
                echo "Image may have changed. Run: kento reset $NAME" >&2
                exit 1
            fi
        done
        unset IFS

        mkdir -p "$LXC_DIR/upper" "$LXC_DIR/work" "$LXC_DIR/rootfs"
        export LIBMOUNT_FORCE_MOUNT2=always
        mount -t overlay overlay \\
            -o "lowerdir=$LAYERS,upperdir=$LXC_DIR/upper,workdir=$LXC_DIR/work" \\
            "$LXC_DIR/rootfs"
        ;;
    post-stop)
        mountpoint -q "$LXC_DIR/rootfs" 2>/dev/null && umount "$LXC_DIR/rootfs" || true
        ;;
esac
"""


def write_hook(lxc_dir: Path, layers: str, name: str) -> Path:
    """Generate and write the hook script into the container directory."""
    hook_path = lxc_dir / "kento-hook"
    hook_path.write_text(generate_hook(lxc_dir, layers, name))
    hook_path.chmod(0o755)
    return hook_path
