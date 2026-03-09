"""Generate per-container LXC hook scripts."""

from pathlib import Path


def generate_hook(container_dir: Path, layers: str, name: str,
                  state_dir: Path | None = None) -> str:
    """Return a hook script with baked-in paths for a container.

    state_dir is where upper/work live. Defaults to container_dir if not given.
    """
    sd = state_dir or container_dir
    return f"""#!/bin/sh
set -eu

NAME="{name}"
CONTAINER_DIR="{container_dir}"
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
        # pre-start (plain LXC): mount at $CONTAINER_DIR/rootfs directly
        # Both use $LXC_ROOTFS_PATH when available, else $CONTAINER_DIR/rootfs
        ROOTFS="${{LXC_ROOTFS_PATH:-$CONTAINER_DIR/rootfs}}"

        mkdir -p "$STATE_DIR/upper" "$STATE_DIR/work" "$ROOTFS"
        export LIBMOUNT_FORCE_MOUNT2=always
        mount -t overlay overlay \\
            -o "lowerdir=$LAYERS,upperdir=$STATE_DIR/upper,workdir=$STATE_DIR/work" \\
            "$ROOTFS"

        # Inject static IP into systemd-networkd config if configured.
        # Sources: LXC/PVE config (authoritative — handles pct set),
        #          kento-net (fallback + DNS which LXC config doesn't carry).
        STATIC_IP=""
        STATIC_GW=""
        STATIC_DNS=""

        # Read DNS from kento-net (LXC config doesn't carry it)
        if [ -f "$CONTAINER_DIR/kento-net" ]; then
            STATIC_DNS=$(sed -n 's/^dns=//p' "$CONTAINER_DIR/kento-net")
        fi

        # Read IP/gateway from LXC/PVE config (authoritative source)
        MODE=$(cat "$CONTAINER_DIR/kento-mode" 2>/dev/null || echo "lxc")
        if [ "$MODE" = "pve" ]; then
            VMID=$(basename "$CONTAINER_DIR")
            PVE_CONF="/etc/pve/lxc/${{VMID}}.conf"
            if [ -f "$PVE_CONF" ]; then
                NET_LINE=$(grep '^net0:' "$PVE_CONF" || true)
                if [ -n "$NET_LINE" ]; then
                    CFG_IP=$(echo "$NET_LINE" | tr ',' '\\n' | sed -n 's/^ip=//p')
                    CFG_GW=$(echo "$NET_LINE" | tr ',' '\\n' | sed -n 's/^gw=//p')
                    if [ -n "$CFG_IP" ] && [ "$CFG_IP" != "dhcp" ]; then
                        STATIC_IP="$CFG_IP"
                        STATIC_GW="${{CFG_GW:-}}"
                    fi
                fi
            fi
        else
            CONFIG_FILE="$CONTAINER_DIR/config"
            if [ -f "$CONFIG_FILE" ]; then
                CFG_IP=$(sed -n 's/^lxc\\.net\\.0\\.ipv4\\.address *= *//p' "$CONFIG_FILE")
                CFG_GW=$(sed -n 's/^lxc\\.net\\.0\\.ipv4\\.gateway *= *//p' "$CONFIG_FILE")
                if [ -n "$CFG_IP" ]; then
                    STATIC_IP="$CFG_IP"
                    STATIC_GW="${{CFG_GW:-}}"
                fi
            fi
        fi

        # Fall back to kento-net if LXC config has no IP
        if [ -z "$STATIC_IP" ] && [ -f "$CONTAINER_DIR/kento-net" ]; then
            STATIC_IP=$(sed -n 's/^ip=//p' "$CONTAINER_DIR/kento-net")
            STATIC_GW=$(sed -n 's/^gateway=//p' "$CONTAINER_DIR/kento-net")
        fi

        # Write systemd-networkd unit if static IP is configured
        if [ -n "$STATIC_IP" ]; then
            NET_DIR="$ROOTFS/etc/systemd/network"
            mkdir -p "$NET_DIR"
            {{
                echo "[Match]"
                echo "Name=eth0"
                echo ""
                echo "[Network]"
                echo "Address=$STATIC_IP"
                [ -n "${{STATIC_GW:-}}" ] && echo "Gateway=$STATIC_GW"
                [ -n "${{STATIC_DNS:-}}" ] && echo "DNS=$STATIC_DNS"
            }} > "$NET_DIR/90-static.network"
        fi
        ;;
    post-stop)
        mountpoint -q "$CONTAINER_DIR/rootfs" 2>/dev/null && umount "$CONTAINER_DIR/rootfs" || true
        ;;
esac
"""


def write_hook(container_dir: Path, layers: str, name: str,
               state_dir: Path | None = None) -> Path:
    """Generate and write the hook script into the container directory."""
    hook_path = container_dir / "kento-hook"
    hook_path.write_text(generate_hook(container_dir, layers, name, state_dir))
    hook_path.chmod(0o755)
    return hook_path
