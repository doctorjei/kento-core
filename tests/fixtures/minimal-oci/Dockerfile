# Minimal busybox-based OCI image used by Tier 2 (real LXC-in-LXC harness)
# and Tier 3 (E2E SECTION D nested-LXC) tests.
#
# Design goals:
#   - Tiny (busybox:latest is ~4-5 MB compressed)
#   - Provides /sbin/init that keeps the container alive via sleep loop
#   - udhcpc on eth0 so port-forwarding tests (which depend on kento's
#     start-host hook discovering the guest IP via lxc-info) have an IP
#     to find. No systemd, no networkd, no cloud-init -- minimal surface
#
# Build:
#   podman build -t localhost/kento-test-minimal:latest tests/fixtures/minimal-oci/
FROM busybox:latest

# busybox images ship with /bin/sh (ash). We write a trivial init that blocks
# forever so the container stays up for kento's lifecycle tests.
RUN mkdir -p /sbin /etc /etc/udhcpc \
    && printf '%s\n' \
        '#!/bin/sh' \
        '# kento test fixture init: sleep forever but stay reactive to signals.' \
        '# A bare "sleep" would block signals until it returns, making' \
        '# "kento stop" wait until the 3600s timeout. Putting sleep in the' \
        '# background and using "wait" lets the trap fire promptly.' \
        '#' \
        '# Signals trapped:' \
        '#   TERM / INT  -- kill/kill -k and generic stop paths' \
        '#   PWR         -- lxc-stop (without -k) sends SIGPWR to PID 1 for' \
        '#                  a clean shutdown request. busybox sh ignores SIGPWR' \
        '#                  by default, so we must trap it explicitly or' \
        '#                  lxc-stop hangs until its 60s timeout.' \
        'trap '"'"'exit 0'"'"' TERM INT PWR' \
        '' \
        '# DHCP: acquire an eth0 lease in the background so the start-host' \
        '# hook can discover the IP via lxc-info and install port-forward' \
        '# rules. -b daemonizes once the lease is acquired. Failure is' \
        '# non-fatal -- the fixture is still useful for lifecycle tests' \
        '# on networks without DHCP.' \
        'if [ -x /sbin/udhcpc ] || [ -x /bin/udhcpc ]; then' \
        '    ip link set eth0 up 2>/dev/null' \
        '    udhcpc -i eth0 -b -s /etc/udhcpc/default.script -q -t 5 -T 3 >/dev/null 2>&1 &' \
        'fi' \
        '' \
        'while :; do' \
        '    sleep 3600 &' \
        '    wait $!' \
        'done' \
        > /sbin/init \
    && chmod 0755 /sbin/init \
    && sh -n /sbin/init \
    && printf '%s\n' \
        '#!/bin/sh' \
        '# Minimal udhcpc handler: on bound/renew, configure the interface.' \
        '# busybox udhcpc exports: interface, ip, subnet (dotted-quad or CIDR),' \
        '# router, dns, etc. We only need ip+subnet+router here.' \
        'case "$1" in' \
        '    deconfig)' \
        '        ip addr flush dev "$interface" 2>/dev/null' \
        '        ;;' \
        '    bound|renew)' \
        '        ip addr flush dev "$interface" 2>/dev/null' \
        '        # iproute2 accepts /255.255.255.0 and normalizes to /24.' \
        '        ip addr add "$ip/${subnet:-24}" dev "$interface"' \
        '        if [ -n "$router" ]; then' \
        '            ip route add default via "$router" 2>/dev/null' \
        '        fi' \
        '        ;;' \
        'esac' \
        'exit 0' \
        > /etc/udhcpc/default.script \
    && chmod 0755 /etc/udhcpc/default.script \
    && sh -n /etc/udhcpc/default.script \
    && printf 'kento-test-minimal\n' > /etc/hostname

ENTRYPOINT ["/sbin/init"]
CMD []
