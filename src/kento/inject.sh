#!/bin/sh
# kento guest config injection — standalone POSIX shell script.
#
# Usage: inject.sh <ROOTFS> <CONTAINER_DIR>
#   $1 = ROOTFS path (where to inject guest-side files)
#   $2 = CONTAINER_DIR (where to read kento metadata and mode from)
#
# Reads config from LXC/PVE config (authoritative, handles `pct set`) and
# kento metadata files (fallback for values LXC config can't carry).
set -eu

ROOTFS="$1"
CONTAINER_DIR="$2"

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
    PVE_CONF="/etc/pve/lxc/${VMID}.conf"
    if [ -f "$PVE_CONF" ]; then
        # Network (from net0 line)
        NET_LINE=$(grep '^net0:' "$PVE_CONF" || true)
        if [ -n "$NET_LINE" ]; then
            CFG_IP=$(echo "$NET_LINE" | tr ',' '\n' | sed -n 's/^ip=//p')
            CFG_GW=$(echo "$NET_LINE" | tr ',' '\n' | sed -n 's/^gw=//p')
            if [ -n "$CFG_IP" ] && [ "$CFG_IP" != "dhcp" ]; then
                STATIC_IP="$CFG_IP"
                STATIC_GW="${CFG_GW:-}"
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

        # Create guest-side mount point directories for mp[n] entries
        grep '^mp[0-9]*:' "$PVE_CONF" | while IFS= read -r mp_line; do
            MP_PATH=$(echo "$mp_line" | tr ',' '\n' | sed -n 's/^mp=//p')
            if [ -n "$MP_PATH" ]; then
                mkdir -p "$ROOTFS$MP_PATH"
            fi
        done
    fi
else
    CONFIG_FILE="$CONTAINER_DIR/config"
    if [ -f "$CONFIG_FILE" ]; then
        CFG_IP=$(sed -n 's/^lxc\.net\.0\.ipv4\.address *= *//p' "$CONFIG_FILE")
        CFG_GW=$(sed -n 's/^lxc\.net\.0\.ipv4\.gateway *= *//p' "$CONFIG_FILE")
        CFG_HOSTNAME=$(sed -n 's/^lxc\.uts\.name *= *//p' "$CONFIG_FILE")
        if [ -n "$CFG_IP" ]; then
            STATIC_IP="$CFG_IP"
            STATIC_GW="${CFG_GW:-}"
        fi
    fi
fi

# Fall back to kento-net if LXC config has no IP
if [ -z "$STATIC_IP" ] && [ -f "$CONTAINER_DIR/kento-net" ]; then
    STATIC_IP=$(sed -n 's/^ip=//p' "$CONTAINER_DIR/kento-net")
    STATIC_GW=$(sed -n 's/^gateway=//p' "$CONTAINER_DIR/kento-net")
fi

# Inject hostname
if [ -n "${CFG_HOSTNAME:-}" ]; then
    echo "$CFG_HOSTNAME" > "$ROOTFS/etc/hostname"
fi

# Inject network config
if [ -n "$STATIC_IP" ]; then
    NET_DIR="$ROOTFS/etc/systemd/network"
    mkdir -p "$NET_DIR"
    # VM modes use predictable naming (e.g. enp0s2), match by type.
    # LXC/PVE modes always have eth0 (configured by LXC veth).
    if [ "$MODE" = "vm" ] || [ "$MODE" = "pve-vm" ]; then
        MATCH_LINE="Type=ether"
    else
        MATCH_LINE="Name=eth0"
    fi
    {
        echo "[Match]"
        echo "$MATCH_LINE"
        echo ""
        echo "[Network]"
        echo "Address=$STATIC_IP"
        [ -n "${STATIC_GW:-}" ] && echo "Gateway=$STATIC_GW"
        [ -n "${STATIC_DNS:-}" ] && echo "DNS=$STATIC_DNS"
        [ -n "${STATIC_SEARCH:-}" ] && echo "Domains=$STATIC_SEARCH"
    } > "$NET_DIR/10-static.network"
elif [ -n "${STATIC_DNS:-}" ] || [ -n "${STATIC_SEARCH:-}" ]; then
    # No static IP but DNS/search set — use resolved drop-in
    RESOLVED_DIR="$ROOTFS/etc/systemd/resolved.conf.d"
    mkdir -p "$RESOLVED_DIR"
    {
        echo "[Resolve]"
        [ -n "${STATIC_DNS:-}" ] && echo "DNS=$STATIC_DNS"
        [ -n "${STATIC_SEARCH:-}" ] && echo "Domains=$STATIC_SEARCH"
    } > "$RESOLVED_DIR/90-kento.conf"
fi

# Inject timezone
if [ -n "${CFG_TZ:-}" ]; then
    ln -sf "/usr/share/zoneinfo/$CFG_TZ" "$ROOTFS/etc/localtime"
    echo "$CFG_TZ" > "$ROOTFS/etc/timezone"
fi

# Inject environment variables
CFG_ENV=""
if [ "$MODE" = "pve" ] && [ -f "${PVE_CONF:-}" ]; then
    CFG_ENV=$(sed -n 's/^lxc\.environment: *//p' "$PVE_CONF")
elif [ "$MODE" != "pve" ] && [ -f "${CONFIG_FILE:-}" ]; then
    CFG_ENV=$(sed -n 's/^lxc\.environment *= *//p' "$CONFIG_FILE")
fi
# Append kento-env entries (may overlap with lxc.environment keys),
# then deduplicate by key — config takes priority over kento-env.
if [ -f "$CONTAINER_DIR/kento-env" ]; then
    KENTO_ENV=$(cat "$CONTAINER_DIR/kento-env")
    if [ -n "$CFG_ENV" ]; then
        CFG_ENV="$CFG_ENV
$KENTO_ENV"
    else
        CFG_ENV="$KENTO_ENV"
    fi
fi
# Auto-inject TZ from timezone config (lowest priority — user --env TZ wins
# because the awk dedup below keeps the first occurrence of each key).
if [ -n "${CFG_TZ:-}" ]; then
    if [ -n "$CFG_ENV" ]; then
        CFG_ENV="$CFG_ENV
TZ=$CFG_TZ"
    else
        CFG_ENV="TZ=$CFG_TZ"
    fi
fi
if [ -n "${CFG_ENV:-}" ]; then
    echo "$CFG_ENV" | awk -F= '!seen[$1]++' > "$ROOTFS/etc/environment"
fi

# --- SSH authorized_keys injection ---
if [ -f "$CONTAINER_DIR/kento-authorized-keys" ]; then
    mkdir -p "$ROOTFS/root/.ssh"
    chmod 700 "$ROOTFS/root/.ssh"
    cp "$CONTAINER_DIR/kento-authorized-keys" "$ROOTFS/root/.ssh/authorized_keys"
    chmod 600 "$ROOTFS/root/.ssh/authorized_keys"
fi
