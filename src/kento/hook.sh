#!/bin/sh
set -eu

NAME="@@NAME@@"
CONTAINER_DIR="@@CONTAINER_DIR@@"
STATE_DIR="@@STATE_DIR@@"
LAYERS="@@LAYERS@@"
HOOK_TYPE="${LXC_HOOK_TYPE:-$3}"

# ---------------------------------------------------------------------------
# Port forwarding setup — idempotent, safe to call from multiple hook points.
#
# Called from `start-host` for plain LXC (normal path) AND from `pre-mount`
# for PVE-LXC (because PVE's config parser silently drops the
# `lxc.hook.start-host` directive — it is not in PVE's allow-list —
# so start-host is never invoked for PVE containers).
#
# Guard: once $CONTAINER_DIR/kento-portfwd-active exists the hook has already
# installed rules for this boot, so subsequent invocations are no-ops.
# ---------------------------------------------------------------------------
setup_port_forwarding() {
    CONTAINER_ID_ARG="$1"  # container name (plain LXC) or VMID (PVE)

    PORT_FILE="$CONTAINER_DIR/kento-port"
    [ -f "$PORT_FILE" ] || return 0

    # Already configured for this boot (another hook point beat us to it)?
    [ -f "$CONTAINER_DIR/kento-portfwd-active" ] && return 0

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

    if [ -n "$CONTAINER_IP" ]; then
        nft add rule ip kento prerouting tcp dport "$HOST_PORT" dnat to "${CONTAINER_IP}:${GUEST_PORT}" comment "\"kento:${NAME}\""
        nft add rule ip kento output tcp dport "$HOST_PORT" dnat to "${CONTAINER_IP}:${GUEST_PORT}" comment "\"kento:${NAME}\""
        nft add rule ip kento postrouting ip saddr 127.0.0.0/8 ip daddr "${CONTAINER_IP}" tcp dport "$GUEST_PORT" masquerade comment "\"kento:${NAME}\""
        echo "${HOST_PORT}:${GUEST_PORT}:${CONTAINER_IP}" > "$CONTAINER_DIR/kento-portfwd-active"
    else
        # DHCP path — detach a worker that polls lxc-info and installs
        # the rules once the guest has an address.
        CONTAINER_ID="${LXC_NAME:-$CONTAINER_ID_ARG}"
        WORKER="$CONTAINER_DIR/kento-portfwd-worker.sh"
        cat > "$WORKER" <<WORKER_EOF
#!/bin/sh
CID="$CONTAINER_ID"
NAME="$NAME"
HOST_PORT="$HOST_PORT"
GUEST_PORT="$GUEST_PORT"
CONTAINER_DIR="$CONTAINER_DIR"
# Bail out if rules already installed (e.g. another hook raced ahead).
[ -f "\$CONTAINER_DIR/kento-portfwd-active" ] && exit 0
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
# Re-check in case start-host raced ahead while we polled.
[ -f "\$CONTAINER_DIR/kento-portfwd-active" ] && exit 0
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
}

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

        # PVE-LXC: install port-forwarding rules here because PVE's
        # config parser drops `lxc.hook.start-host` (not in its allow-list),
        # so the start-host branch below never fires for pve-lxc. For
        # plain LXC this is a no-op when start-host later runs, thanks to
        # the kento-portfwd-active guard in setup_port_forwarding.
        setup_port_forwarding "$1"
        ;;
    port-forward-only)
        # Internal re-entry point: the pre-mount branch invokes the hook
        # script again with this pseudo hook-type inside pid-1's network
        # namespace (via nsenter) to install nftables rules on the host.
        # See the NETNS comment in the pre-start/pre-mount branch.
        setup_port_forwarding "$1"
        ;;
    start-host)
        # PVE-LXC: propagate memory/cores limits into the inner `ns` cgroup so
        # the guest sees its own limit at /sys/fs/cgroup/memory.max instead of
        # "max". PVE nests the container cgroup via `lxc.cgroup.dir.container.inner = ns`,
        # which means `lxc.cgroup2.*` keys land on the outer (accounting) cgroup
        # at /sys/fs/cgroup/lxc/<vmid>/ while processes run in
        # /sys/fs/cgroup/lxc/<vmid>/ns/ — cgroup v2 enforces the outer ceiling,
        # but the inner file literally has "max" written on it. Apps like JVMs
        # that read memory.max to size themselves get misled. Skip cleanly if
        # PVE changes the nesting name (no `ns/`) or the write fails.
        NS_CGROUP="/sys/fs/cgroup/lxc/$1/ns"
        if [ -d "$NS_CGROUP" ]; then
            if [ -f "$CONTAINER_DIR/kento-memory" ]; then
                MEM_MB=$(cat "$CONTAINER_DIR/kento-memory" | tr -d '[:space:]')
                if [ -n "$MEM_MB" ]; then
                    MEM_BYTES=$((MEM_MB * 1024 * 1024))
                    echo "$MEM_BYTES" > "$NS_CGROUP/memory.max" 2>/dev/null \
                        || echo "kento: warning: could not set memory.max on $NS_CGROUP" >&2
                fi
            fi
            if [ -f "$CONTAINER_DIR/kento-cores" ]; then
                CORES=$(cat "$CONTAINER_DIR/kento-cores" | tr -d '[:space:]')
                if [ -n "$CORES" ]; then
                    QUOTA=$((CORES * 100000))
                    echo "$QUOTA 100000" > "$NS_CGROUP/cpu.max" 2>/dev/null \
                        || echo "kento: warning: could not set cpu.max on $NS_CGROUP" >&2
                fi
            fi
        fi

        # Port forwarding via nftables DNAT (LXC only; PVE-LXC handles it
        # from pre-mount because PVE strips lxc.hook.start-host).
        setup_port_forwarding "$1"
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
