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

        # --- Guest config injection ---
        # Read config from LXC/PVE config (authoritative, handles pct set)
        # and kento metadata files (fallback for values LXC config can't carry).

        STATIC_IP=""
        STATIC_GW=""
        STATIC_DNS=""
        STATIC_SEARCH=""
        CFG_HOSTNAME=""
        CFG_TZ=""

        # Read kento metadata (fallback values)
        if [ -f "$CONTAINER_DIR/kento-net" ]; then
            STATIC_DNS=$(sed -n 's/^dns=//p' "$CONTAINER_DIR/kento-net")
            STATIC_SEARCH=$(sed -n 's/^searchdomain=//p' "$CONTAINER_DIR/kento-net")
        fi
        if [ -f "$CONTAINER_DIR/kento-tz" ]; then
            CFG_TZ=$(cat "$CONTAINER_DIR/kento-tz")
        fi

        # Read LXC/PVE config (authoritative — overrides kento metadata)
        MODE=$(cat "$CONTAINER_DIR/kento-mode" 2>/dev/null || echo "lxc")
        if [ "$MODE" = "pve" ]; then
            VMID=$(basename "$CONTAINER_DIR")
            PVE_CONF="/etc/pve/lxc/${{VMID}}.conf"
            if [ -f "$PVE_CONF" ]; then
                # Network (from net0 line)
                NET_LINE=$(grep '^net0:' "$PVE_CONF" || true)
                if [ -n "$NET_LINE" ]; then
                    CFG_IP=$(echo "$NET_LINE" | tr ',' '\\n' | sed -n 's/^ip=//p')
                    CFG_GW=$(echo "$NET_LINE" | tr ',' '\\n' | sed -n 's/^gw=//p')
                    if [ -n "$CFG_IP" ] && [ "$CFG_IP" != "dhcp" ]; then
                        STATIC_IP="$CFG_IP"
                        STATIC_GW="${{CFG_GW:-}}"
                    fi
                fi
                # Top-level PVE directives
                CFG_HOSTNAME=$(sed -n 's/^hostname: *//p' "$PVE_CONF")
                CFG_NS=$(sed -n 's/^nameserver: *//p' "$PVE_CONF")
                [ -n "$CFG_NS" ] && STATIC_DNS="$CFG_NS"
                CFG_SD=$(sed -n 's/^searchdomain: *//p' "$PVE_CONF")
                [ -n "$CFG_SD" ] && STATIC_SEARCH="$CFG_SD"
                CFG_PVE_TZ=$(sed -n 's/^timezone: *//p' "$PVE_CONF")
                [ -n "$CFG_PVE_TZ" ] && CFG_TZ="$CFG_PVE_TZ"
            fi
        else
            CONFIG_FILE="$CONTAINER_DIR/config"
            if [ -f "$CONFIG_FILE" ]; then
                CFG_IP=$(sed -n 's/^lxc\\.net\\.0\\.ipv4\\.address *= *//p' "$CONFIG_FILE")
                CFG_GW=$(sed -n 's/^lxc\\.net\\.0\\.ipv4\\.gateway *= *//p' "$CONFIG_FILE")
                CFG_HOSTNAME=$(sed -n 's/^lxc\\.uts\\.name *= *//p' "$CONFIG_FILE")
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

        # Inject hostname
        if [ -n "${{CFG_HOSTNAME:-}}" ]; then
            echo "$CFG_HOSTNAME" > "$ROOTFS/etc/hostname"
        fi

        # Inject network config
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
                [ -n "${{STATIC_SEARCH:-}}" ] && echo "Domains=$STATIC_SEARCH"
            }} > "$NET_DIR/10-static.network"
        elif [ -n "${{STATIC_DNS:-}}" ] || [ -n "${{STATIC_SEARCH:-}}" ]; then
            # No static IP but DNS/search set — use resolved drop-in
            RESOLVED_DIR="$ROOTFS/etc/systemd/resolved.conf.d"
            mkdir -p "$RESOLVED_DIR"
            {{
                echo "[Resolve]"
                [ -n "${{STATIC_DNS:-}}" ] && echo "DNS=$STATIC_DNS"
                [ -n "${{STATIC_SEARCH:-}}" ] && echo "Domains=$STATIC_SEARCH"
            }} > "$RESOLVED_DIR/90-kento.conf"
        fi

        # Inject timezone
        if [ -n "${{CFG_TZ:-}}" ]; then
            ln -sf "/usr/share/zoneinfo/$CFG_TZ" "$ROOTFS/etc/localtime"
            echo "$CFG_TZ" > "$ROOTFS/etc/timezone"
        fi

        # Inject environment variables
        CFG_ENV=""
        if [ "$MODE" = "pve" ] && [ -f "${{PVE_CONF:-}}" ]; then
            CFG_ENV=$(sed -n 's/^lxc\\.environment: *//p' "$PVE_CONF")
        elif [ "$MODE" != "pve" ] && [ -f "${{CONFIG_FILE:-}}" ]; then
            CFG_ENV=$(sed -n 's/^lxc\\.environment *= *//p' "$CONFIG_FILE")
        fi
        # Append kento-env entries (won't duplicate — kento-env has entries
        # not in lxc config, e.g. for VM mode or plain LXC without lxc.environment)
        if [ -f "$CONTAINER_DIR/kento-env" ]; then
            KENTO_ENV=$(cat "$CONTAINER_DIR/kento-env")
            if [ -n "$CFG_ENV" ]; then
                CFG_ENV="$CFG_ENV
$KENTO_ENV"
            else
                CFG_ENV="$KENTO_ENV"
            fi
        fi
        if [ -n "${{CFG_ENV:-}}" ]; then
            echo "$CFG_ENV" > "$ROOTFS/etc/environment"
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
