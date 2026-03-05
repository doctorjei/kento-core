"""Generate per-container LXC hook scripts."""

from pathlib import Path


def generate_hook(lxc_dir: Path, layers: str, name: str,
                  state_dir: Path | None = None) -> str:
    """Return a hook script with baked-in paths for a container.

    state_dir is where upper/work live. Defaults to lxc_dir if not given.
    """
    sd = state_dir or lxc_dir
    return f"""#!/bin/sh
set -eu

NAME="{name}"
LXC_DIR="{lxc_dir}"
STATE_DIR="{sd}"
LAYERS="{layers}"
HOOK_TYPE="${{LXC_HOOK_TYPE:-$3}}"

case "$HOOK_TYPE" in
    pre-start|pre-mount)
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

        # pre-mount (PVE): mount at lxc.rootfs.path source so LXC picks it up
        # pre-start (plain LXC): mount at $LXC_DIR/rootfs directly
        # Both use $LXC_ROOTFS_PATH when available, else $LXC_DIR/rootfs
        ROOTFS="${{LXC_ROOTFS_PATH:-$LXC_DIR/rootfs}}"

        mkdir -p "$STATE_DIR/upper" "$STATE_DIR/work" "$ROOTFS"
        export LIBMOUNT_FORCE_MOUNT2=always
        mount -t overlay overlay \\
            -o "lowerdir=$LAYERS,upperdir=$STATE_DIR/upper,workdir=$STATE_DIR/work" \\
            "$ROOTFS"
        ;;
    post-stop)
        mountpoint -q "$LXC_DIR/rootfs" 2>/dev/null && umount "$LXC_DIR/rootfs" || true
        ;;
esac
"""


def write_hook(lxc_dir: Path, layers: str, name: str,
               state_dir: Path | None = None) -> Path:
    """Generate and write the hook script into the container directory."""
    hook_path = lxc_dir / "kento-hook"
    hook_path.write_text(generate_hook(lxc_dir, layers, name, state_dir))
    hook_path.chmod(0o755)
    return hook_path
