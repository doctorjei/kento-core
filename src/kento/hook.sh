#!/bin/sh
set -eu

NAME="@@NAME@@"
CONTAINER_DIR="@@CONTAINER_DIR@@"
STATE_DIR="@@STATE_DIR@@"
LAYERS="@@LAYERS@@"
HOOK_TYPE="${LXC_HOOK_TYPE:-$3}"

# ---------------------------------------------------------------------------
# Port forwarding setup — idempotent.
#
# Called from `start-host` for both plain LXC and pve-lxc. For pve-lxc,
# start-host fires via PVE's snippets hookscript at post-start phase (PVE's
# config parser strips `lxc.hook.start-host:`, so we route it through a
# snippets wrapper that execs this script with $3="start-host").
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

    # F19: validate PORT_SPEC before feeding into nft. kento's CLI
    # validates --port at create time, but kento-port is a plain file on
    # disk; a tampered/corrupted value here would otherwise flow straight
    # into `nft add rule ... dport "$HOST_PORT"` as shell-expanded text.
    # Require strictly HOST:GUEST where both are integers in [1, 65535].
    _kento_port_valid=1
    case "$PORT_SPEC" in
        *[!0-9:]*)      _kento_port_valid=0 ;;
        *:*:*)          _kento_port_valid=0 ;;
        :*|*:)          _kento_port_valid=0 ;;
        *:*)            : ;;
        *)              _kento_port_valid=0 ;;
    esac
    if [ "$_kento_port_valid" -eq 0 ]; then
        echo "kento-hook: invalid kento-port $PORT_SPEC (expected HOST:GUEST integers) -- skipping port forwarding" >&2
        return 0
    fi
    HOST_PORT="${PORT_SPEC%%:*}"
    GUEST_PORT="${PORT_SPEC##*:}"
    for _kp in "$HOST_PORT" "$GUEST_PORT"; do
        if ! [ "$_kp" -ge 1 ] 2>/dev/null || ! [ "$_kp" -le 65535 ] 2>/dev/null; then
            echo "kento-hook: port $_kp out of range 1..65535 in kento-port -- skipping port forwarding" >&2
            return 0
        fi
    done

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
                echo "kento-hook: error: layer path missing: $dir" >&2
                echo "kento-hook: image may have changed. Run: kento scrub $NAME" >&2
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
        # Container identifier: plain LXC with hook.version=1 passes args via
        # env vars only ($LXC_NAME); pve-lxc arrives via the snippets wrapper
        # which passes VMID as $1. Handle both safely under `set -u`.
        CONTAINER_ID="${LXC_NAME:-${1:-}}"

        # pve-lxc only: propagate memory/cores limits into the inner `ns` cgroup
        # so the guest sees its own limit at /sys/fs/cgroup/memory.max instead
        # of "max". PVE nests the container cgroup via
        # `lxc.cgroup.dir.container.inner = ns`, so `lxc.cgroup2.*` keys land
        # on the outer (accounting) cgroup at /sys/fs/cgroup/lxc/<vmid>/ while
        # processes run in /sys/fs/cgroup/lxc/<vmid>/ns/. Plain LXC has no
        # inner nesting; /sys/fs/cgroup/lxc/<name>/ns/ won't exist, so the
        # is-dir check below silently skips.
        NS_CGROUP="${KENTO_TEST_NS_CGROUP:-/sys/fs/cgroup/lxc/$CONTAINER_ID/ns}"
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

        # Port forwarding via nftables DNAT. For pve-lxc, this branch is
        # reached via the snippets hookscript wrapper (post-start phase).
        setup_port_forwarding "$CONTAINER_ID"
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
