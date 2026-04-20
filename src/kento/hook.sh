#!/bin/sh
set -eu

NAME="@@NAME@@"
CONTAINER_DIR="@@CONTAINER_DIR@@"
STATE_DIR="@@STATE_DIR@@"
LAYERS="@@LAYERS@@"
HOOK_TYPE="${LXC_HOOK_TYPE:-$3}"

case "$HOOK_TYPE" in
    pre-start|pre-mount)
        # Validate layer paths still exist (image may have changed)
        IFS=:
        for dir in $LAYERS; do
            if [ ! -d "$dir" ]; then
                echo "Error: layer path missing: $dir" >&2
                echo "Image may have changed. Run: kento scrub $NAME" >&2
                exit 1
            fi
        done
        unset IFS

        # pre-mount (PVE): mount at lxc.rootfs.path source so LXC picks it up
        # pre-start (plain LXC): mount at $CONTAINER_DIR/rootfs directly
        # Both use $LXC_ROOTFS_PATH when available, else $CONTAINER_DIR/rootfs
        ROOTFS="${LXC_ROOTFS_PATH:-$CONTAINER_DIR/rootfs}"

        mkdir -p "$STATE_DIR/upper" "$STATE_DIR/work" "$ROOTFS"
        export LIBMOUNT_FORCE_MOUNT2=always
        mount -t overlay overlay \
            -o "lowerdir=$LAYERS,upperdir=$STATE_DIR/upper,workdir=$STATE_DIR/work" \
            "$ROOTFS"

        # Guest config injection — shared with VM / PVE-VM modes.
        sh "$CONTAINER_DIR/kento-inject.sh" "$ROOTFS" "$CONTAINER_DIR"
        ;;
    post-stop)
        mountpoint -q "$CONTAINER_DIR/rootfs" 2>/dev/null && umount "$CONTAINER_DIR/rootfs" || true
        ;;
esac
