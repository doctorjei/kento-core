#!/bin/sh
# Build the minimal busybox OCI fixture used by Tier 2 and Tier 3 nested-LXC
# tests. Idempotent -- rerunning is safe and will re-tag the image.
#
# Usage:
#   tests/fixtures/build.sh [--help]
#
# The image is tagged as localhost/kento-test-minimal:latest in the root
# podman store (kento uses root's store, not user storage).
set -eu

usage() {
    cat <<'EOF'
Usage: tests/fixtures/build.sh [--help]

Builds the minimal busybox-based OCI image used by kento's Tier 2 (real
LXC-in-LXC) and Tier 3 (E2E SECTION D nested-LXC) tests.

The image is tagged: localhost/kento-test-minimal:latest

Requirements:
  - podman in PATH
  - Network access to docker.io (to pull busybox:latest on first build)

Exit codes:
  0 on successful build, non-zero on failure.
EOF
}

case "${1:-}" in
    -h|--help)
        usage
        exit 0
        ;;
    "")
        ;;
    *)
        printf 'build.sh: unknown argument: %s\n' "$1" >&2
        usage >&2
        exit 2
        ;;
esac

if ! command -v podman >/dev/null 2>&1; then
    printf 'build.sh: podman not found in PATH\n' >&2
    exit 127
fi

# Resolve the fixture directory relative to this script so the build works
# regardless of the caller's cwd.
script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
context_dir="${script_dir}/minimal-oci"
tag='localhost/kento-test-minimal:latest'

if [ ! -f "${context_dir}/Dockerfile" ]; then
    printf 'build.sh: Dockerfile not found at %s/Dockerfile\n' "${context_dir}" >&2
    exit 1
fi

printf 'Building %s from %s...\n' "${tag}" "${context_dir}"
podman build -t "${tag}" "${context_dir}"
printf 'Built %s\n' "${tag}"
