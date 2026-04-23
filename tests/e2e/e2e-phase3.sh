#!/bin/sh
# E2E validation for Phase 3 (port forwarding) + Phase 3.5 (cloud-init)
# Run on the test host (stuffer for VM, loom for LXC) with current kento installed.
# Usage: sudo ./e2e-phase3.sh <test-image>
# Example: sudo ./e2e-phase3.sh ghcr.io/doctorjei/droste-wool:latest
set -eu

IMAGE="${1:-}"
if [ -z "$IMAGE" ]; then
    echo "Usage: $0 <oci-image-ref>"
    echo "The image should have: systemd, openssh-server, cloud-init"
    exit 1
fi

PASS=0
FAIL=0
report() {
    if [ "$1" = "ok" ]; then
        PASS=$((PASS + 1))
        printf "  \033[32mPASS\033[0m %s\n" "$2"
    else
        FAIL=$((FAIL + 1))
        printf "  \033[31mFAIL\033[0m %s\n" "$2"
    fi
}

cleanup() {
    echo "--- Cleanup ---"
    kento destroy -f e2e-inject 2>/dev/null || true
    kento destroy -f e2e-cloudinit 2>/dev/null || true
    kento destroy -f e2e-portfwd 2>/dev/null || true
}
trap cleanup EXIT

echo "=== Phase 3+3.5 E2E Tests ==="
echo "Image: $IMAGE"
echo ""

# --- Pull image ---
echo "--- Pulling image ---"
kento pull "$IMAGE"

# ==========================================================
# TEST 1: Injection mode (explicit) + port forwarding (LXC)
# ==========================================================
echo ""
echo "--- Test 1: LXC injection + port forwarding ---"

kento create "$IMAGE" --name e2e-portfwd \
    --config-mode injection \
    --ip 10.0.3.100/24 --gateway 10.0.3.1 \
    --dns 8.8.8.8 \
    --port 10080:22 \
    --ssh-host-keys \
    --start

sleep 3  # wait for systemd to reach multi-user

# Check: container is running
if kento info e2e-portfwd | grep -q "running"; then
    report ok "container running"
else
    report fail "container not running"
fi

# Check: kento-port file exists
DIR=$(kento info e2e-portfwd --json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('dir',''))" 2>/dev/null || echo "")
if [ -n "$DIR" ] && [ -f "$DIR/kento-port" ]; then
    report ok "kento-port metadata exists"
else
    report fail "kento-port metadata missing"
fi

# Check: nftables rules present
if nft list chain ip kento prerouting 2>/dev/null | grep -q "kento:e2e-portfwd"; then
    report ok "nftables DNAT rule present"
else
    report fail "nftables DNAT rule missing"
fi

# Check: portfwd-active file
if [ -n "$DIR" ] && [ -f "$DIR/kento-portfwd-active" ]; then
    report ok "kento-portfwd-active written"
else
    report fail "kento-portfwd-active missing"
fi

# Check: config mode is injection
if [ -n "$DIR" ] && grep -q "injection" "$DIR/kento-config-mode" 2>/dev/null; then
    report ok "config mode = injection"
else
    report fail "config mode not injection"
fi

# Check: SSH reachable via forwarded port (if sshpass available)
if command -v sshpass >/dev/null 2>&1; then
    if sshpass -p droste ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
        -p 10080 droste@127.0.0.1 "echo ok" 2>/dev/null | grep -q "ok"; then
        report ok "SSH via port forward works"
    else
        report fail "SSH via port forward failed"
    fi
else
    echo "  SKIP SSH test (sshpass not installed)"
fi

# Stop and verify nftables rules cleaned up
kento shutdown e2e-portfwd
sleep 1
if nft list chain ip kento prerouting 2>/dev/null | grep -q "kento:e2e-portfwd"; then
    report fail "nftables rules NOT cleaned up after stop"
else
    report ok "nftables rules cleaned up after stop"
fi

# Check: portfwd-active removed
if [ -n "$DIR" ] && [ -f "$DIR/kento-portfwd-active" ]; then
    report fail "kento-portfwd-active not cleaned up"
else
    report ok "kento-portfwd-active cleaned up"
fi

kento destroy e2e-portfwd

# ==========================================================
# TEST 2: Cloud-init mode (auto-detected)
# ==========================================================
echo ""
echo "--- Test 2: Cloud-init mode (auto-detected) ---"

kento create "$IMAGE" --name e2e-cloudinit \
    --ip 10.0.3.101/24 --gateway 10.0.3.1 \
    --dns 8.8.8.8 \
    --timezone Europe/Berlin \
    --env "KENTO_TEST=hello" \
    --ssh-host-keys

DIR=$(kento info e2e-cloudinit --json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('dir',''))" 2>/dev/null || echo "")

# Check: auto-detected cloud-init mode
if [ -n "$DIR" ] && grep -q "cloudinit" "$DIR/kento-config-mode" 2>/dev/null; then
    report ok "auto-detected cloudinit mode"
else
    report fail "did not auto-detect cloudinit (is cloud-init in image?)"
fi

# Check: cloud-seed directory exists
if [ -n "$DIR" ] && [ -d "$DIR/cloud-seed" ]; then
    report ok "cloud-seed/ directory exists"
else
    report fail "cloud-seed/ directory missing"
fi

# Check: meta-data has instance-id
if [ -n "$DIR" ] && grep -q "^instance-id: kento-" "$DIR/cloud-seed/meta-data" 2>/dev/null; then
    report ok "meta-data has content-hash instance-id"
else
    report fail "meta-data missing or bad instance-id"
fi

# Check: user-data has cloud-config header
if [ -n "$DIR" ] && head -1 "$DIR/cloud-seed/user-data" 2>/dev/null | grep -q "^#cloud-config"; then
    report ok "user-data has #cloud-config header"
else
    report fail "user-data missing cloud-config header"
fi

# Check: user-data has timezone
if [ -n "$DIR" ] && grep -q "timezone: Europe/Berlin" "$DIR/cloud-seed/user-data" 2>/dev/null; then
    report ok "user-data has timezone"
else
    report fail "user-data missing timezone"
fi

# Check: network-config has static IP
if [ -n "$DIR" ] && grep -q "10.0.3.101/24" "$DIR/cloud-seed/network-config" 2>/dev/null; then
    report ok "network-config has static IP"
else
    report fail "network-config missing static IP"
fi

# Check: user-data has env write_files
if [ -n "$DIR" ] && grep -q "KENTO_TEST=hello" "$DIR/cloud-seed/user-data" 2>/dev/null; then
    report ok "user-data has env via write_files"
else
    report fail "user-data missing env"
fi

# Start and verify cloud-init runs
kento start e2e-cloudinit
sleep 10  # cloud-init needs time

if kento info e2e-cloudinit | grep -q "running"; then
    report ok "cloudinit container running"
else
    report fail "cloudinit container not running"
fi

kento shutdown e2e-cloudinit
kento destroy e2e-cloudinit

# ==========================================================
# TEST 3: Injection mode (forced on cloud-init image)
# ==========================================================
echo ""
echo "--- Test 3: Forced injection mode ---"

kento create "$IMAGE" --name e2e-inject \
    --config-mode injection \
    --ip 10.0.3.102/24 --gateway 10.0.3.1 \
    --timezone America/New_York

DIR=$(kento info e2e-inject --json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('dir',''))" 2>/dev/null || echo "")

# Check: forced injection mode despite cloud-init in image
if [ -n "$DIR" ] && grep -q "injection" "$DIR/kento-config-mode" 2>/dev/null; then
    report ok "forced injection mode"
else
    report fail "config mode not forced to injection"
fi

# Check: no cloud-seed directory
if [ -n "$DIR" ] && [ ! -d "$DIR/cloud-seed" ]; then
    report ok "no cloud-seed/ in injection mode"
else
    report fail "cloud-seed/ present in injection mode (should not be)"
fi

kento destroy e2e-inject

# ==========================================================
# Summary
# ==========================================================
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
