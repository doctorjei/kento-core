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

        # Discover container IP: static (kento-net) fast path, or DHCP
        # discovery via lxc-info in a detached background worker.
        #
        # DEADLOCK NOTE: the start-host hook runs in the LXC monitor process.
        # `lxc-info -iH` opens a control-socket RPC to that same monitor —
        # which is blocked waiting for this hook to return. So calling
        # lxc-info synchronously here hangs forever and kento start never
        # exits. Always detach the DHCP path with setsid + nohup so the
        # worker survives after the hook returns and can talk to the
        # (by then free) monitor.
        CONTAINER_IP=""
        NET_FILE="$CONTAINER_DIR/kento-net"
        if [ -f "$NET_FILE" ]; then
            CONTAINER_IP=$(grep '^ip=' "$NET_FILE" | head -1 | sed 's/^ip=//' | cut -d/ -f1)
        fi

        # Enable route_localnet so the kernel will route 127.0.0.0/8 traffic
        # out through the bridge interface toward the container. Without this
        # the kernel drops packets with saddr 127.0.0.1 on non-loopback
        # interfaces, so `ssh -p <host_port> localhost` times out even when
        # the DNAT and masquerade rules are correct. Debian/PVE defaults to 0.
        echo 1 > /proc/sys/net/ipv4/conf/all/route_localnet 2>/dev/null || true

        # Ensure kento nftables table and base chains exist (idempotent)
        nft add table ip kento 2>/dev/null || true
        nft 'add chain ip kento prerouting { type nat hook prerouting priority dstnat; policy accept; }' 2>/dev/null || true
        nft 'add chain ip kento output { type nat hook output priority dstnat; policy accept; }' 2>/dev/null || true
        nft 'add chain ip kento postrouting { type nat hook postrouting priority srcnat; policy accept; }' 2>/dev/null || true

        apply_portfwd_rules() {
            # $1 = CONTAINER_IP
            nft add rule ip kento prerouting tcp dport "$HOST_PORT" dnat to "${1}:${GUEST_PORT}" comment "\"kento:${NAME}\""
            nft add rule ip kento output tcp dport "$HOST_PORT" dnat to "${1}:${GUEST_PORT}" comment "\"kento:${NAME}\""
            nft add rule ip kento postrouting ip saddr 127.0.0.0/8 ip daddr "${1}" tcp dport "$GUEST_PORT" masquerade comment "\"kento:${NAME}\""
            echo "${HOST_PORT}:${GUEST_PORT}:${1}" > "$CONTAINER_DIR/kento-portfwd-active"
        }

        if [ -n "$CONTAINER_IP" ]; then
            apply_portfwd_rules "$CONTAINER_IP"
        else
            # DHCP path — detach a worker that polls lxc-info and installs
            # the rules once the guest has an address.
            CONTAINER_ID="${LXC_NAME:-$1}"
            WORKER="$CONTAINER_DIR/kento-portfwd-worker.sh"
            cat > "$WORKER" <<WORKER_EOF
#!/bin/sh
CID="$CONTAINER_ID"
NAME="$NAME"
HOST_PORT="$HOST_PORT"
GUEST_PORT="$GUEST_PORT"
CONTAINER_DIR="$CONTAINER_DIR"
IP=""
TRIES=0
# Retry for up to ~30s; DHCP can take a while on slow guests.
# IPv4-only: our nftables rules live in the ip family, so we must ignore
# IPv6 addresses (which lxc-info prints interleaved with IPv4 ones).
while [ "\$TRIES" -lt 30 ]; do
    IP=\$(lxc-info -n "\$CID" -iH 2>/dev/null \\
        | grep -E '^[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+\$' | head -1)
    [ -n "\$IP" ] && break
    sleep 1
    TRIES=\$((TRIES + 1))
done
if [ -z "\$IP" ]; then
    echo "kento: could not determine IPv4 address for \$NAME; port forwarding not active" \\
        > "\$CONTAINER_DIR/kento-portfwd-error" 2>&1
    exit 0
fi
nft add rule ip kento prerouting tcp dport "\$HOST_PORT" \\
    dnat to "\$IP:\$GUEST_PORT" comment "\"kento:\$NAME\""
nft add rule ip kento output tcp dport "\$HOST_PORT" \\
    dnat to "\$IP:\$GUEST_PORT" comment "\"kento:\$NAME\""
nft add rule ip kento postrouting ip saddr 127.0.0.0/8 \\
    ip daddr "\$IP" tcp dport "\$GUEST_PORT" \\
    masquerade comment "\"kento:\$NAME\""
echo "\$HOST_PORT:\$GUEST_PORT:\$IP" > "\$CONTAINER_DIR/kento-portfwd-active"
WORKER_EOF
            chmod +x "$WORKER"
            # setsid + redirection detaches fully from the monitor process
            # group, so lxc-start can reap its children and return.
            setsid sh "$WORKER" </dev/null >/dev/null 2>&1 &
        fi
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
