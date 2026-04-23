# Minimal busybox-based OCI image used by Tier 2 (real LXC-in-LXC harness)
# and Tier 3 (E2E SECTION D nested-LXC) tests.
#
# Design goals:
#   - Tiny (busybox:latest is ~4-5 MB compressed)
#   - Provides /sbin/init that keeps the container alive via sleep loop
#   - No systemd, no DHCP, no networkd, no cloud-init -- minimal surface
#
# Build:
#   podman build -t localhost/kento-test-minimal:latest tests/fixtures/minimal-oci/
FROM busybox:latest

# busybox images ship with /bin/sh (ash). We write a trivial init that blocks
# forever so the container stays up for kento's lifecycle tests.
RUN mkdir -p /sbin /etc \
    && printf '%s\n' \
        '#!/bin/sh' \
        '# kento test fixture init: sleep forever so the container stays alive.' \
        'while true; do' \
        '    sleep 3600' \
        'done' \
        > /sbin/init \
    && chmod 0755 /sbin/init \
    && printf 'kento-test-minimal\n' > /etc/hostname

ENTRYPOINT ["/sbin/init"]
CMD []
