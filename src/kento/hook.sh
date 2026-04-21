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
    start-host)
        # Port forwarding via nftables DNAT (LXC/PVE modes only).
        PORT_FILE="$CONTAINER_DIR/kento-port"
        [ -f "$PORT_FILE" ] || exit 0

        PORT_SPEC=$(cat "$PORT_FILE" | tr -d '[:space:]')
        HOST_PORT="${PORT_SPEC%%:*}"
        GUEST_PORT="${PORT_SPEC##*:}"

        # Discover container IP: static (kento-net) or DHCP (lxc-info)
        CONTAINER_IP=""
        NET_FILE="$CONTAINER_DIR/kento-net"
        if [ -f "$NET_FILE" ]; then
            CONTAINER_IP=$(grep '^ip=' "$NET_FILE" | head -1 | sed 's/^ip=//' | cut -d/ -f1)
        fi

        if [ -z "$CONTAINER_IP" ]; then
            CONTAINER_ID="${LXC_NAME:-$1}"
            TRIES=0
            while [ "$TRIES" -lt 10 ]; do
                CONTAINER_IP=$(lxc-info -n "$CONTAINER_ID" -iH 2>/dev/null | head -1)
                [ -n "$CONTAINER_IP" ] && break
                sleep 1
                TRIES=$((TRIES + 1))
            done
        fi

        if [ -z "$CONTAINER_IP" ]; then
            echo "Warning: could not determine container IP for port forwarding" >&2
            echo "Port forwarding will not be active for $NAME" >&2
            exit 0
        fi

        # Check ip_forward
        if [ "$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null)" != "1" ]; then
            echo "Warning: net.ipv4.ip_forward is disabled; port forwarding may not work" >&2
        fi

        # Ensure kento nftables table and base chains exist (idempotent)
        nft add table ip kento 2>/dev/null || true
        nft 'add chain ip kento prerouting { type nat hook prerouting priority dstnat; policy accept; }' 2>/dev/null || true
        nft 'add chain ip kento output { type nat hook output priority dstnat; policy accept; }' 2>/dev/null || true
        nft 'add chain ip kento postrouting { type nat hook postrouting priority srcnat; policy accept; }' 2>/dev/null || true

        # Add DNAT rules tagged with container name for reliable cleanup
        nft add rule ip kento prerouting tcp dport "$HOST_PORT" dnat to "${CONTAINER_IP}:${GUEST_PORT}" comment "\"kento:${NAME}\""
        nft add rule ip kento output tcp dport "$HOST_PORT" dnat to "${CONTAINER_IP}:${GUEST_PORT}" comment "\"kento:${NAME}\""
        nft add rule ip kento postrouting ip saddr 127.0.0.0/8 ip daddr "$CONTAINER_IP" tcp dport "$GUEST_PORT" masquerade comment "\"kento:${NAME}\""

        # Record active state for info display and fallback cleanup
        echo "${HOST_PORT}:${GUEST_PORT}:${CONTAINER_IP}" > "$CONTAINER_DIR/kento-portfwd-active"
        ;;
    post-stop)
        # Tear down nftables port forwarding rules by comment tag
        if [ -f "$CONTAINER_DIR/kento-portfwd-active" ]; then
            for chain in prerouting output postrouting; do
                nft -a list chain ip kento "$chain" 2>/dev/null | grep "kento:${NAME}" | \
                    awk '{print $NF}' | while read -r handle; do
                        nft delete rule ip kento "$chain" handle "$handle" 2>/dev/null || true
                    done
            done
            rm -f "$CONTAINER_DIR/kento-portfwd-active"
        fi
        # Unmount overlayfs
        mountpoint -q "$CONTAINER_DIR/rootfs" 2>/dev/null && umount "$CONTAINER_DIR/rootfs" || true
        ;;
esac
