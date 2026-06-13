#!/bin/sh
set -eu

NAME="@@NAME@@"
CONTAINER_DIR="@@CONTAINER_DIR@@"
STATE_DIR="@@STATE_DIR@@"
LAYERS="@@LAYERS@@"
HOOK_TYPE="${LXC_HOOK_TYPE:-$3}"

# ---------------------------------------------------------------------------
# NAT backend resolution.
#
# Port forwarding installs DNAT/masquerade rules; the host may ship either
# nftables (`nft`) or legacy iptables (`iptables`). Prefer nft when present
# (kento's historical backend, isolated `ip kento` table), else fall back to
# iptables. If NEITHER is available we cannot install rules — warn and skip
# WITHOUT aborting the start-host hook (which runs under `set -eu`; an
# unguarded missing-binary call would exit 127 and fail instance start).
#
# Echoes `nft`, `iptables`, or `` (empty = none). Probes are guarded so a
# missing binary never trips `set -e`.
# ---------------------------------------------------------------------------
kento_nat_backend() {
    if command -v nft >/dev/null 2>&1; then
        echo nft
    elif command -v iptables >/dev/null 2>&1; then
        echo iptables
    else
        echo ""
    fi
}

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

    # Resolve the NAT backend once. If neither nft nor iptables is present we
    # cannot install rules; record an error marker and bail WITHOUT aborting
    # start (the unguarded rule installs below would otherwise hit exit 127
    # under `set -eu` and fail the whole start-host hook).
    BACKEND=$(kento_nat_backend)
    if [ -z "$BACKEND" ]; then
        echo "kento: neither nft nor iptables found; port forwarding for $NAME not active" \
            > "$CONTAINER_DIR/kento-portfwd-error" 2>&1
        echo "kento-hook: neither nft nor iptables available -- skipping port forwarding for $NAME" >&2
        return 0
    fi

    if [ "$BACKEND" = nft ]; then
        # Ensure kento nftables table and base chains exist (idempotent)
        nft add table ip kento 2>/dev/null || true
        nft 'add chain ip kento prerouting { type nat hook prerouting priority dstnat; policy accept; }' 2>/dev/null || true
        nft 'add chain ip kento output { type nat hook output priority dstnat; policy accept; }' 2>/dev/null || true
        nft 'add chain ip kento postrouting { type nat hook postrouting priority srcnat; policy accept; }' 2>/dev/null || true
    fi

    if [ -n "$CONTAINER_IP" ]; then
        if [ "$BACKEND" = nft ]; then
            nft add rule ip kento prerouting tcp dport "$HOST_PORT" dnat to "${CONTAINER_IP}:${GUEST_PORT}" comment "\"kento:${NAME}\""
            nft add rule ip kento output tcp dport "$HOST_PORT" dnat to "${CONTAINER_IP}:${GUEST_PORT}" comment "\"kento:${NAME}\""
            nft add rule ip kento postrouting ip saddr 127.0.0.0/8 ip daddr "${CONTAINER_IP}" tcp dport "$GUEST_PORT" masquerade comment "\"kento:${NAME}\""
        else
            # iptables fallback: append to the standard nat-table chains.
            # Rules are comment-tagged "kento:NAME" so post-stop can find and
            # delete exactly the rules this instance installed.
            iptables -t nat -A PREROUTING -p tcp --dport "$HOST_PORT" -j DNAT --to-destination "${CONTAINER_IP}:${GUEST_PORT}" -m comment --comment "kento:${NAME}"
            iptables -t nat -A OUTPUT -p tcp --dport "$HOST_PORT" -j DNAT --to-destination "${CONTAINER_IP}:${GUEST_PORT}" -m comment --comment "kento:${NAME}"
            iptables -t nat -A POSTROUTING -s 127.0.0.0/8 -d "${CONTAINER_IP}" -p tcp --dport "$GUEST_PORT" -j MASQUERADE -m comment --comment "kento:${NAME}"
        fi
        echo "${HOST_PORT}:${GUEST_PORT}:${CONTAINER_IP}" > "$CONTAINER_DIR/kento-portfwd-active"
        echo "$BACKEND" > "$CONTAINER_DIR/kento-portfwd-backend"
    else
        # DHCP path — detach a worker that polls lxc-info and installs
        # the rules once the guest has an address. The resolved backend is
        # baked into the worker (host NAT state is stable within a boot, so
        # the worker need not re-detect).
        CONTAINER_ID="${LXC_NAME:-$CONTAINER_ID_ARG}"
        WORKER="$CONTAINER_DIR/kento-portfwd-worker.sh"
        cat > "$WORKER" <<WORKER_EOF
#!/bin/sh
CID="$CONTAINER_ID"
NAME="$NAME"
HOST_PORT="$HOST_PORT"
GUEST_PORT="$GUEST_PORT"
CONTAINER_DIR="$CONTAINER_DIR"
BACKEND="$BACKEND"
# Bail out if rules already installed (e.g. another hook raced ahead).
[ -f "\$CONTAINER_DIR/kento-portfwd-active" ] && exit 0
# Bail out if the container was stopped while we were being launched —
# post-stop drops a cancel sentinel at the very start of its teardown.
[ -f "\$CONTAINER_DIR/kento-portfwd-cancel" ] && exit 0
IP=""
TRIES=0
# Retry for up to ~30s; DHCP can take a while on slow guests.
# IPv4-only: our NAT rules live in the ip family, so we must ignore
# IPv6 addresses (which lxc-info prints interleaved with IPv4 ones).
while [ "\$TRIES" -lt 30 ]; do
    # If the container stopped mid-discovery, post-stop wrote a cancel
    # sentinel; abandon the poll WITHOUT installing rules or a marker.
    [ -f "\$CONTAINER_DIR/kento-portfwd-cancel" ] && exit 0
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
# Final cancel check immediately BEFORE installing any rule: this closes
# the race where the container is stopped after we obtained an IP but
# before we commit rules. Without this, a late worker could install
# orphan DNAT/masquerade rules into the shared table for a dead container.
[ -f "\$CONTAINER_DIR/kento-portfwd-cancel" ] && exit 0
if [ "\$BACKEND" = nft ]; then
    nft add rule ip kento prerouting tcp dport "\$HOST_PORT" \\
        dnat to "\$IP:\$GUEST_PORT" comment "\"kento:\$NAME\""
    nft add rule ip kento output tcp dport "\$HOST_PORT" \\
        dnat to "\$IP:\$GUEST_PORT" comment "\"kento:\$NAME\""
    nft add rule ip kento postrouting ip saddr 127.0.0.0/8 \\
        ip daddr "\$IP" tcp dport "\$GUEST_PORT" \\
        masquerade comment "\"kento:\$NAME\""
else
    iptables -t nat -A PREROUTING -p tcp --dport "\$HOST_PORT" \\
        -j DNAT --to-destination "\$IP:\$GUEST_PORT" \\
        -m comment --comment "kento:\$NAME"
    iptables -t nat -A OUTPUT -p tcp --dport "\$HOST_PORT" \\
        -j DNAT --to-destination "\$IP:\$GUEST_PORT" \\
        -m comment --comment "kento:\$NAME"
    iptables -t nat -A POSTROUTING -s 127.0.0.0/8 -d "\$IP" \\
        -p tcp --dport "\$GUEST_PORT" -j MASQUERADE \\
        -m comment --comment "kento:\$NAME"
fi
echo "\$HOST_PORT:\$GUEST_PORT:\$IP" > "\$CONTAINER_DIR/kento-portfwd-active"
echo "\$BACKEND" > "\$CONTAINER_DIR/kento-portfwd-backend"
WORKER_EOF
        chmod +x "$WORKER"
        # A fresh launch supersedes any stale cancel sentinel from a prior
        # boot, so the new worker isn't aborted on its first check.
        rm -f "$CONTAINER_DIR/kento-portfwd-cancel" 2>/dev/null || true
        # setsid + redirection detaches fully from the monitor process
        # group, so lxc-start can reap its children and return. setsid makes
        # the worker its own process-group leader, so its PID is also the
        # PGID — post-stop signals the whole group to reap a still-polling
        # worker promptly.
        setsid sh "$WORKER" </dev/null >/dev/null 2>&1 &
        echo "$!" > "$CONTAINER_DIR/kento-portfwd-pid" 2>/dev/null || true
    fi
}

case "$HOOK_TYPE" in
    pre-start|pre-mount|mount)
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

        # pre-mount (PVE privileged): mount at lxc.rootfs.path source so LXC picks it up
        # mount (PVE unprivileged): same — LXC_ROOTFS_PATH is set at the mount hook point
        # pre-start (plain LXC): mount at $CONTAINER_DIR/rootfs directly
        # All three use $LXC_ROOTFS_PATH when available, else $CONTAINER_DIR/rootfs
        ROOTFS="${LXC_ROOTFS_PATH:-$CONTAINER_DIR/rootfs}"

        mkdir -p "$STATE_DIR/upper" "$STATE_DIR/work" "$ROOTFS"
        export LIBMOUNT_FORCE_MOUNT2=always

        if [ -f "$CONTAINER_DIR/kento-unprivileged" ]; then
            # ---------------------------------------------------------------------------
            # Unprivileged (per-layer idmap) path — mainline kernel 5.19+, util-linux 2.40+
            #
            # For each lowerdir Lᵢ, create an idmapped bind mount so the
            # on-disk uid/gid 0 appears as <BASE> (the container's host uid/gid
            # base) through the overlay. The overlay is then mounted over the
            # idmapped lowers with userxattr,index=off,metacopy=off so
            # container-root (host-<BASE>) can read/write the rootfs.
            #
            # The idmap range is read from $LXC_CONFIG_FILE, which LXC has
            # fully parsed and written before invoking any hook (race-free).
            # Plain-lxc: kento emits `lxc.idmap = u 0 <BASE> <COUNT>` lines.
            # pve-lxc: PVE generates them and writes them to the container config.
            # ---------------------------------------------------------------------------

            # Fail closed if LXC_CONFIG_FILE is not set or not readable.
            if [ -z "${LXC_CONFIG_FILE:-}" ] || [ ! -r "$LXC_CONFIG_FILE" ]; then
                echo "kento-hook: error: unprivileged mode requires LXC_CONFIG_FILE to be set and readable" >&2
                echo "kento-hook: LXC_CONFIG_FILE='${LXC_CONFIG_FILE:-<unset>}'" >&2
                exit 1
            fi

            # Parse the first `lxc.idmap = u 0 <BASE> <COUNT>` line.
            # Format is exactly: lxc.idmap = u 0 BASE COUNT
            _idmap_line=$(grep -m1 '^[[:space:]]*lxc\.idmap[[:space:]]*=[[:space:]]*u[[:space:]]' "$LXC_CONFIG_FILE" 2>/dev/null || true)
            if [ -z "$_idmap_line" ]; then
                echo "kento-hook: error: unprivileged mode requires lxc.idmap u ... lines in $LXC_CONFIG_FILE" >&2
                echo "kento-hook: no 'lxc.idmap = u ...' line found in $LXC_CONFIG_FILE" >&2
                exit 1
            fi

            # Extract BASE and COUNT: strip to the last two whitespace-separated tokens
            # after the `u 0` prefix. Line format: `lxc.idmap = u 0 BASE COUNT`
            _idmap_rest=$(printf '%s' "$_idmap_line" | sed 's/^[[:space:]]*lxc\.idmap[[:space:]]*=[[:space:]]*u[[:space:]]*//')
            # _idmap_rest is now: "0 BASE COUNT" — skip the container-side id (0)
            BASE=$(printf '%s' "$_idmap_rest" | awk '{print $2}')
            COUNT=$(printf '%s' "$_idmap_rest" | awk '{print $3}')

            if [ -z "$BASE" ] || [ -z "$COUNT" ]; then
                echo "kento-hook: error: could not parse BASE and COUNT from lxc.idmap line: $_idmap_line" >&2
                exit 1
            fi

            # Validate BASE and COUNT are integers
            case "$BASE" in
                ''|*[!0-9]*) echo "kento-hook: error: idmap BASE is not a non-negative integer: '$BASE'" >&2; exit 1 ;;
            esac
            case "$COUNT" in
                ''|*[!0-9]*) echo "kento-hook: error: idmap COUNT is not a non-negative integer: '$COUNT'" >&2; exit 1 ;;
            esac

            # Create idmapped bind mounts for each lowerdir, preserving order.
            # $STATE_DIR/idmap/0, /1, /2, ... correspond to $LAYERS order.
            mkdir -p "$STATE_DIR/idmap"
            IDLAYERS=""
            _idx=0
            IFS=:
            for _layer in $LAYERS; do
                _idmap_target="$STATE_DIR/idmap/$_idx"
                mkdir -p "$_idmap_target"
                # Idempotent: skip re-binding if already a mountpoint.
                if ! mountpoint -q "$_idmap_target" 2>/dev/null; then
                    mount --bind \
                        -o "X-mount.idmap=u:0:${BASE}:${COUNT} g:0:${BASE}:${COUNT}" \
                        "$_layer" "$_idmap_target"
                fi
                if [ -z "$IDLAYERS" ]; then
                    IDLAYERS="$_idmap_target"
                else
                    IDLAYERS="$IDLAYERS:$_idmap_target"
                fi
                _idx=$((_idx + 1))
            done
            unset IFS

            # chown upper and work so container-root (host-$BASE) can write them.
            chown "${BASE}:${BASE}" "$STATE_DIR/upper" "$STATE_DIR/work"

            # Mount the overlay over the idmapped lowers.
            # userxattr: required for overlay-on-idmapped-lowers (kernel 5.19+).
            # index=off,metacopy=off: avoid inode-index and metacopy features
            # that are incompatible with the per-layer idmap path.
            mount -t overlay overlay \
                -o "lowerdir=$IDLAYERS,upperdir=$STATE_DIR/upper,workdir=$STATE_DIR/work,userxattr,index=off,metacopy=off" \
                "$ROOTFS"
        else
            # Privileged path — unchanged.
            mount -t overlay overlay \
                -o "lowerdir=$LAYERS,upperdir=$STATE_DIR/upper,workdir=$STATE_DIR/work" \
                "$ROOTFS"
        fi

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
                # Validate before arithmetic: a non-numeric/empty value would
                # make $(( )) fail and, under `set -e`, abort the hook (a
                # start error on pve-lxc). Warn-and-skip instead, mirroring the
                # kento-port guard above.
                case "$MEM_MB" in
                    ''|*[!0-9]*)
                        echo "kento: warning: invalid kento-memory '$MEM_MB' (expected positive integer MB) -- skipping memory.max" >&2
                        ;;
                    *)
                        MEM_BYTES=$((MEM_MB * 1024 * 1024))
                        echo "$MEM_BYTES" > "$NS_CGROUP/memory.max" 2>/dev/null \
                            || echo "kento: warning: could not set memory.max on $NS_CGROUP" >&2
                        ;;
                esac
            fi
            if [ -f "$CONTAINER_DIR/kento-cores" ]; then
                CORES=$(cat "$CONTAINER_DIR/kento-cores" | tr -d '[:space:]')
                # Validate before arithmetic (see kento-memory above): a
                # corrupted non-numeric value would otherwise abort the hook
                # under `set -e`.
                case "$CORES" in
                    ''|*[!0-9]*)
                        echo "kento: warning: invalid kento-cores '$CORES' (expected positive integer) -- skipping cpu.max" >&2
                        ;;
                    *)
                        QUOTA=$((CORES * 100000))
                        echo "$QUOTA 100000" > "$NS_CGROUP/cpu.max" 2>/dev/null \
                            || echo "kento: warning: could not set cpu.max on $NS_CGROUP" >&2
                        ;;
                esac
            fi
        fi

        # Port forwarding via nftables DNAT. For pve-lxc, this branch is
        # reached via the snippets hookscript wrapper (post-start phase).
        setup_port_forwarding "$CONTAINER_ID"
        ;;
    post-stop)
        # Drop a cancel sentinel FIRST, before anything else. A DHCP
        # port-forward worker may still be polling (or about to install
        # rules) for this now-dead container; the sentinel tells it to
        # abort without installing rules or writing the active marker. This
        # closes the race where a worker wakes up after stop and leaves
        # orphan DNAT/masquerade rules in the shared table.
        : > "$CONTAINER_DIR/kento-portfwd-cancel" 2>/dev/null || true

        # Reap a still-polling worker promptly: it was launched under setsid
        # so its recorded PID is also its process-group ID. TERM the whole
        # group. Tolerate the worker already being gone (kill -> non-zero).
        if [ -f "$CONTAINER_DIR/kento-portfwd-pid" ]; then
            WORKER_PID=$(cat "$CONTAINER_DIR/kento-portfwd-pid" 2>/dev/null | tr -d '[:space:]')
            case "$WORKER_PID" in
                ''|*[!0-9]*) : ;;
                *) kill -TERM -- "-$WORKER_PID" 2>/dev/null || true ;;
            esac
        fi

        # Tear down port forwarding rules by comment tag, using whichever
        # backend installed them. The backend was recorded at install time in
        # kento-portfwd-backend; default to nft if the marker is absent
        # (pre-1.5.0 containers only ever used nft).
        #
        # Teardown runs UNCONDITIONALLY — NOT gated on kento-portfwd-active.
        # A worker that won the race against the cancel sentinel may have
        # installed rules without (or just before) writing the marker, so
        # always attempt removal of this instance's tagged rules. The
        # anchored greps below match only `kento:${NAME}` exactly, so a
        # no-op teardown on an instance that never installed rules is quiet
        # and harmless.
        BACKEND=nft
        if [ -f "$CONTAINER_DIR/kento-portfwd-backend" ]; then
            BACKEND=$(cat "$CONTAINER_DIR/kento-portfwd-backend" | tr -d '[:space:]')
        fi
        # NAME is interpolated into an ERE below, where a kento name's `.`
        # would otherwise act as a one-char wildcard (so `web.api` would also
        # match a sibling `web1api`'s rule and tear down the wrong instance).
        # Escape the ERE metacharacters that can appear in or around a name
        # before using it in the teardown greps. The install side writes
        # literal comments and is intentionally left untouched.
        NAME_RE=$(printf '%s' "$NAME" | sed 's/[.[\*^$]/\\&/g')
        if [ "$BACKEND" = iptables ]; then
            # iptables: line numbers shift on every delete, so re-list and
            # delete the first matching rule until none remain.
            for chain in PREROUTING OUTPUT POSTROUTING; do
                while :; do
                    # iptables renders the tag as `/* kento:NAME */`
                    # (space-delimited). Anchor to a trailing boundary so
                    # `kento:web` does NOT match `kento:web2`'s rule; NAME is
                    # regex-escaped (NAME_RE) so a name's `.` is a literal dot.
                    n=$(iptables -t nat -L "$chain" --line-numbers -n 2>/dev/null \
                        | grep -E "kento:${NAME_RE}( |\$)" | head -1 | awk '{print $1}')
                    [ -n "$n" ] || break
                    iptables -t nat -D "$chain" "$n" 2>/dev/null || break
                done
            done
        else
            for chain in prerouting output postrouting; do
                # nft renders the tag as `comment "kento:NAME"`. Match the
                # full quoted token so `kento:web` does NOT also delete
                # `kento:web2`'s rule (prefix collision); NAME is regex-escaped
                # (NAME_RE) so a name's `.` is a literal dot, not a wildcard.
                nft -a list chain ip kento "$chain" 2>/dev/null \
                    | grep -E "comment \"kento:${NAME_RE}\"( |\$)" | \
                    awk '{print $NF}' | while read -r handle; do
                        nft delete rule ip kento "$chain" handle "$handle" 2>/dev/null || true
                    done
            done
        fi
        # Remove the worker script and all kento-portfwd-* sentinels
        # (active/backend/cancel/pid/worker.sh). Idempotent and quiet.
        rm -f "$CONTAINER_DIR/kento-portfwd-active" \
            "$CONTAINER_DIR/kento-portfwd-backend" \
            "$CONTAINER_DIR/kento-portfwd-cancel" \
            "$CONTAINER_DIR/kento-portfwd-pid" \
            "$CONTAINER_DIR/kento-portfwd-worker.sh" 2>/dev/null || true
        # Unmount overlayfs
        mountpoint -q "$CONTAINER_DIR/rootfs" 2>/dev/null && umount "$CONTAINER_DIR/rootfs" || true

        # Unprivileged cleanup: unmount idmapped bind mounts in reverse order
        # and remove the idmap directory. Idempotent and quiet — if the
        # idmap mounts were never created (privileged path) this is a no-op.
        if [ -d "$STATE_DIR/idmap" ]; then
            # Collect mounted targets in forward order, then umount in reverse
            # so overlapping mount namespaces are handled safely.
            _idmap_mounts=""
            for _d in "$STATE_DIR"/idmap/*; do
                [ -d "$_d" ] || continue
                _idmap_mounts="$_idmap_mounts $_d"
            done
            # Reverse the list and unmount
            _idmap_rev=""
            for _d in $_idmap_mounts; do
                _idmap_rev="$_d $_idmap_rev"
            done
            for _d in $_idmap_rev; do
                [ -z "$_d" ] && continue
                mountpoint -q "$_d" 2>/dev/null && umount "$_d" 2>/dev/null || true
            done
            rm -rf "$STATE_DIR/idmap" 2>/dev/null || true
        fi
        ;;
esac
