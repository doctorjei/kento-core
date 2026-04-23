#!/bin/bash
# kento-e2e.sh — Comprehensive end-to-end test suite for kento
#
# Tests all four modes: lxc, pve-lxc, vm, pve-vm
# Output: TAP format (plan at end)
# Exit: 0 if all pass, 1 if any fail
#
# Usage: sudo ./kento-e2e.sh [OPTIONS]
#
# Options:
#   --mode lxc,pve-lxc,vm,pve-vm   Restrict which modes run (default all four)
#   --section phase,iso,image,nested
#                                  Restrict to phases/iso/image/nested sections.
#                                  'nested' runs SECTION D (nested-LXC on PVE).
#   --subgroup NAME                Run only subgroups matching NAME (iso-port,
#                                    iso-ssh, phase3, ygg, nested-lxc-lifecycle,
#                                    nested-lxc-hookfire, etc.). Can repeat.
#   --from N / --to N              Range-limit by TAP test number
#   -h | --help                    Show this help

set -u

# ---------- constants ----------

IMAGE="localhost/gemet-bifrost-kento:1.4.2"
BRIDGE="lxcbr0"
LXC_IP="10.0.3.200/24"
LXC_GW="10.0.3.1"
PVE_LXC_IP="10.0.3.201/24"
PVE_LXC_GW="10.0.3.1"
PVE_VM_IP="10.0.3.202/24"
PVE_VM_GW="10.0.3.1"

LXC_BASE="/var/lib/lxc"
VM_BASE="/var/lib/kento/vm"

BOOT_TIMEOUT_LXC=30
BOOT_TIMEOUT_VM=60
SSH_TIMEOUT=60

# Per-command safety-net timeouts (seconds)
KTO_CREATE=60
KTO_START_LXC=60
KTO_START_VM=120
KTO_STOP=90
KTO_SCRUB=60
KTO_DESTROY=60
KTO_READ=10

SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -o BatchMode=yes)

MODES=(lxc pve-lxc vm pve-vm)

# ---------- state ----------

TEST_NUM=0
FAIL_COUNT=0
SSH_KEY=""
DIAG_DIR="/tmp"
LAST_INSTANCE_NAME=""
LAST_INSTANCE_MODE=""

# Filter state (populated by argv parsing)
FILTER_MODES=""       # comma-separated list or ""
FILTER_SECTIONS=""    # comma-separated: phase,iso,image
FILTER_SUBGROUPS=""   # comma-separated, or "" for all
FILTER_FROM=0
FILTER_TO=0           # 0 = no upper bound
CURRENT_SUBGROUP=""   # set by subgroup_begin

# ---------- argv parsing ----------

usage() {
    sed -n '2,19p' "$0"
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --mode)      FILTER_MODES="$2"; shift 2 ;;
        --mode=*)    FILTER_MODES="${1#--mode=}"; shift ;;
        --section)   FILTER_SECTIONS="$2"; shift 2 ;;
        --section=*) FILTER_SECTIONS="${1#--section=}"; shift ;;
        --subgroup)  FILTER_SUBGROUPS="${FILTER_SUBGROUPS:+$FILTER_SUBGROUPS,}$2"; shift 2 ;;
        --subgroup=*) FILTER_SUBGROUPS="${FILTER_SUBGROUPS:+$FILTER_SUBGROUPS,}${1#--subgroup=}"; shift ;;
        --from)      FILTER_FROM="$2"; shift 2 ;;
        --from=*)    FILTER_FROM="${1#--from=}"; shift ;;
        --to)        FILTER_TO="$2"; shift 2 ;;
        --to=*)      FILTER_TO="${1#--to=}"; shift ;;
        -h|--help)   usage ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

# ---------- root check ----------

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: must run as root" >&2
    exit 1
fi

# ---------- filter helpers ----------

mode_enabled() {
    local m="$1"
    [ -z "$FILTER_MODES" ] && return 0
    case ",$FILTER_MODES," in
        *",$m,"*) return 0 ;;
        *) return 1 ;;
    esac
}

section_enabled() {
    local s="$1"
    [ -z "$FILTER_SECTIONS" ] && return 0
    case ",$FILTER_SECTIONS," in
        *",$s,"*) return 0 ;;
        *) return 1 ;;
    esac
}

subgroup_enabled() {
    local sg="$1"
    [ -z "$FILTER_SUBGROUPS" ] && return 0
    # Match if the filter substring appears in the subgroup name.
    local IFS=','
    for needle in $FILTER_SUBGROUPS; do
        case "$sg" in
            *"$needle"*) return 0 ;;
        esac
    done
    return 1
}

# A subgroup wraps a block of related tests so that --subgroup can skip them
# and so force_teardown has a canonical name. Call subgroup_begin "name" at
# the start, subgroup_end at the finish. If disabled, begin returns 1 and the
# caller should `|| return 0` to bail out of the function.
subgroup_begin() {
    CURRENT_SUBGROUP="$1"
    if ! subgroup_enabled "$CURRENT_SUBGROUP"; then
        return 1
    fi
    return 0
}

subgroup_end() {
    CURRENT_SUBGROUP=""
}

# Range filter: returns 1 if current TEST_NUM falls outside the window.
# Must be called after TEST_NUM is incremented.
range_enabled() {
    [ "$FILTER_FROM" -gt 0 ] && [ "$TEST_NUM" -lt "$FILTER_FROM" ] && return 1
    [ "$FILTER_TO" -gt 0 ] && [ "$TEST_NUM" -gt "$FILTER_TO" ] && return 1
    return 0
}

# ---------- TAP helpers ----------

pass() {
    TEST_NUM=$((TEST_NUM + 1))
    range_enabled || { echo "ok $TEST_NUM - $1 # SKIP out of range"; return 0; }
    echo "ok $TEST_NUM - $1"
}

fail() {
    TEST_NUM=$((TEST_NUM + 1))
    if ! range_enabled; then
        echo "ok $TEST_NUM - $1 # SKIP out of range"
        return 0
    fi
    FAIL_COUNT=$((FAIL_COUNT + 1))
    echo "not ok $TEST_NUM - $1"
    # Print diagnostics (extra args)
    shift
    for diag in "$@"; do
        echo "# $diag"
    done
    # Auto-collect environment diagnostics for the last instance we touched.
    collect_diagnostics "$TEST_NUM"
}

skip() {
    TEST_NUM=$((TEST_NUM + 1))
    if ! range_enabled; then
        echo "ok $TEST_NUM - $1 # SKIP out of range"
        return 0
    fi
    echo "ok $TEST_NUM - $1 # SKIP $2"
}

diag() {
    echo "# $1"
}

# ---------- instance tracking for diagnostics ----------

set_last_instance() {
    LAST_INSTANCE_NAME="$1"
    LAST_INSTANCE_MODE="${2:-}"
}

# ---------- timed kento runner ----------
# Usage: run_kento <timeout_s> <kento args...>
# Captures combined stdout+stderr in RUN_OUTPUT; sets RUN_RC, RUN_TIMED_OUT.
# timeout(1) exits 124 on SIGTERM timeout; we use SIGKILL after 5s grace.
run_kento() {
    local tmo="$1"; shift
    RUN_OUTPUT="$(timeout -k 5 "$tmo" kento "$@" 2>&1)"
    RUN_RC=$?
    if [ "$RUN_RC" -eq 124 ] || [ "$RUN_RC" -eq 137 ]; then
        RUN_TIMED_OUT=1
    else
        RUN_TIMED_OUT=0
    fi
}

# Usage: run_timed <timeout_s> <command...>
# Same as run_kento but for arbitrary commands (used for ssh etc).
run_timed() {
    local tmo="$1"; shift
    RUN_OUTPUT="$(timeout -k 5 "$tmo" "$@" 2>&1)"
    RUN_RC=$?
    if [ "$RUN_RC" -eq 124 ] || [ "$RUN_RC" -eq 137 ]; then
        RUN_TIMED_OUT=1
    else
        RUN_TIMED_OUT=0
    fi
}

# ---------- diagnostics on failure ----------

collect_diagnostics() {
    local tnum="$1"
    local name="${LAST_INSTANCE_NAME:-}"
    local mode="${LAST_INSTANCE_MODE:-}"
    [ -z "$name" ] && return 0
    local out="${DIAG_DIR}/e2e-diag-${tnum}.log"
    {
        echo "=== e2e diagnostics for test $tnum ==="
        echo "instance: $name mode: $mode"
        echo "date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo ""
        echo "=== kento list ==="
        timeout 10 kento list 2>&1 || true
        echo ""
        case "$mode" in
            lxc)
                echo "=== lxc-info -n $name ==="
                timeout 10 lxc-info -n "$name" 2>&1 || true
                echo "=== lxc-ls --fancy ==="
                timeout 10 lxc-ls --fancy 2>&1 || true
                echo "=== /var/log/lxc/$name.log (tail 30) ==="
                [ -f "/var/log/lxc/$name.log" ] && tail -30 "/var/log/lxc/$name.log" 2>&1 || echo "(no log)"
                local cdir="$LXC_BASE/$name"
                echo "=== $cdir (metadata) ==="
                [ -d "$cdir" ] && ls -la "$cdir" 2>&1
                for m in kento-mode kento-port kento-portfwd-active kento-memory kento-cores kento-name kento-net kento-state kento-config-mode; do
                    [ -f "$cdir/$m" ] && { echo "-- $m --"; cat "$cdir/$m"; echo; }
                done
                echo "=== cgroup memory/cpu (if present) ==="
                for p in /sys/fs/cgroup/lxc.payload.$name /sys/fs/cgroup/lxc/$name; do
                    [ -d "$p" ] && {
                        echo "$p/memory.max: $(cat "$p/memory.max" 2>/dev/null)"
                        echo "$p/cpu.max:    $(cat "$p/cpu.max" 2>/dev/null)"
                    }
                done
                ;;
            pve-lxc|pve)
                # find vmid by scanning /var/lib/lxc/*/kento-name
                local vmid=""
                for d in "$LXC_BASE"/*/; do
                    [ -f "$d/kento-name" ] || continue
                    if [ "$(tr -d '[:space:]' < "$d/kento-name")" = "$name" ]; then
                        vmid="$(basename "$d")"
                        break
                    fi
                done
                echo "=== vmid: $vmid ==="
                if [ -n "$vmid" ]; then
                    echo "=== pct status $vmid ==="
                    timeout 10 pct status "$vmid" 2>&1 || true
                    echo "=== pct config $vmid ==="
                    timeout 10 pct config "$vmid" 2>&1 || true
                    echo "=== pct list ==="
                    timeout 10 pct list 2>&1 || true
                    echo "=== /etc/pve/nodes/*/lxc/$vmid.conf ==="
                    cat /etc/pve/nodes/*/lxc/"$vmid".conf 2>&1 || true
                    echo "=== /var/log/lxc/$vmid.log (tail 30) ==="
                    [ -f "/var/log/lxc/$vmid.log" ] && tail -30 "/var/log/lxc/$vmid.log" 2>&1 || echo "(no log)"
                    local cdir="$LXC_BASE/$vmid"
                    echo "=== $cdir (metadata) ==="
                    [ -d "$cdir" ] && ls -la "$cdir" 2>&1
                    for m in kento-mode kento-port kento-portfwd-active kento-memory kento-cores kento-name; do
                        [ -f "$cdir/$m" ] && { echo "-- $m --"; cat "$cdir/$m"; echo; }
                    done
                    echo "=== cgroup memory/cpu ==="
                    for p in /sys/fs/cgroup/lxc.payload.$vmid /sys/fs/cgroup/lxc/$vmid; do
                        [ -d "$p" ] && {
                            echo "$p/memory.max: $(cat "$p/memory.max" 2>/dev/null)"
                            echo "$p/cpu.max:    $(cat "$p/cpu.max" 2>/dev/null)"
                        }
                    done
                fi
                ;;
            vm)
                local cdir="$VM_BASE/$name"
                echo "=== $cdir (metadata) ==="
                [ -d "$cdir" ] && ls -la "$cdir" 2>&1
                for m in kento-mode kento-port kento-memory kento-cores kento-name kento-vm-pid kento-virtiofsd-pid; do
                    [ -f "$cdir/$m" ] && { echo "-- $m --"; cat "$cdir/$m"; echo; }
                done
                ;;
            pve-vm)
                local vmid=""
                local cdir="$VM_BASE/$name"
                [ -f "$cdir/kento-vmid" ] && vmid="$(tr -d '[:space:]' < "$cdir/kento-vmid")"
                echo "=== vmid: $vmid ==="
                [ -d "$cdir" ] && ls -la "$cdir" 2>&1
                for m in kento-mode kento-port kento-memory kento-cores kento-name kento-vmid; do
                    [ -f "$cdir/$m" ] && { echo "-- $m --"; cat "$cdir/$m"; echo; }
                done
                if [ -n "$vmid" ]; then
                    echo "=== qm status $vmid ==="
                    timeout 10 qm status "$vmid" 2>&1 || true
                    echo "=== qm config $vmid ==="
                    timeout 10 qm config "$vmid" 2>&1 || true
                    echo "=== /etc/pve/nodes/*/qemu-server/$vmid.conf ==="
                    cat /etc/pve/nodes/*/qemu-server/"$vmid".conf 2>&1 || true
                fi
                ;;
        esac
        echo ""
        echo "=== ps aux | grep $name (top) ==="
        ps auxf 2>/dev/null | grep -E "$name|virtiofsd|qemu-system|lxc-start|pct|qm " | grep -v grep | head -20 || true
    } > "$out" 2>&1
    echo "# see $out"
}

# ---------- force teardown (for timeouts / stuck instances) ----------

force_teardown() {
    local name="$1"
    local mode="${2:-}"
    diag "force_teardown: $name ($mode)"

    # Try graceful destroy first with short timeout
    timeout 15 kento destroy -f "$name" >/dev/null 2>&1 || true

    # Kill processes
    pkill -9 -f "$name" 2>/dev/null || true
    # PVE modes — find vmid and kill qemu/lxc
    local vmid=""
    case "$mode" in
        pve-lxc|pve)
            for d in "$LXC_BASE"/*/; do
                [ -f "$d/kento-name" ] || continue
                [ "$(tr -d '[:space:]' < "$d/kento-name")" = "$name" ] && { vmid="$(basename "$d")"; break; }
            done
            if [ -n "$vmid" ]; then
                timeout 15 pct stop "$vmid" --skiplock 1 >/dev/null 2>&1 || true
                pkill -9 -f "lxc-start.*$vmid" 2>/dev/null || true
                # Unmount rootfs
                umount -l "$LXC_BASE/$vmid/rootfs" 2>/dev/null || true
                # Best-effort purge
                rm -rf "$LXC_BASE/$vmid" 2>/dev/null || true
                rm -f /etc/pve/nodes/*/lxc/"$vmid".conf 2>/dev/null || true
            fi
            ;;
        lxc)
            timeout 15 lxc-stop -n "$name" -k >/dev/null 2>&1 || true
            umount -l "$LXC_BASE/$name/rootfs" 2>/dev/null || true
            rm -rf "$LXC_BASE/$name" 2>/dev/null || true
            ;;
        vm)
            local cdir="$VM_BASE/$name"
            [ -f "$cdir/kento-vm-pid" ] && kill -9 "$(cat "$cdir/kento-vm-pid")" 2>/dev/null || true
            [ -f "$cdir/kento-virtiofsd-pid" ] && kill -9 "$(cat "$cdir/kento-virtiofsd-pid")" 2>/dev/null || true
            umount -l "$cdir/rootfs" 2>/dev/null || true
            rm -rf "$cdir" 2>/dev/null || true
            ;;
        pve-vm)
            local cdir="$VM_BASE/$name"
            [ -f "$cdir/kento-vmid" ] && vmid="$(tr -d '[:space:]' < "$cdir/kento-vmid")"
            if [ -n "$vmid" ]; then
                timeout 15 qm stop "$vmid" --skiplock 1 >/dev/null 2>&1 || true
                pkill -9 -f "kvm -id $vmid" 2>/dev/null || true
                rm -f /etc/pve/nodes/*/qemu-server/"$vmid".conf 2>/dev/null || true
            fi
            umount -l "$cdir/rootfs" 2>/dev/null || true
            rm -rf "$cdir" 2>/dev/null || true
            ;;
    esac
    podman rm -f "kento-hold.$name" 2>/dev/null || true
}

# ---------- cleanup ----------

cleanup() {
    diag "Cleaning up e2e-* instances..."
    for name in $(kento list 2>/dev/null | grep 'e2e-' | awk '{print $1}'); do
        kento stop "$name" 2>/dev/null || true
        kento destroy -f "$name" 2>/dev/null || true
    done
    # Catch any strays by directory (main, isolation, multi-image)
    for d in "$LXC_BASE"/e2e-* "$VM_BASE"/e2e-*; do
        [ -d "$d" ] && rm -rf "$d"
    done
    # Remove temp SSH key
    if [ -n "${SSH_KEY:-}" ]; then
        rm -f "$SSH_KEY" "${SSH_KEY}.pub"
    fi
}

trap cleanup EXIT

# ---------- helper: setup SSH key ----------

setup_ssh_key() {
    SSH_KEY="$(mktemp /tmp/kento-e2e-key.XXXXXX)"
    rm -f "$SSH_KEY"
    ssh-keygen -t ed25519 -f "$SSH_KEY" -N "" -q
}

# ---------- helper: instance name for a mode ----------

instance_name() {
    local mode="$1"
    echo "e2e-${mode}"
}

# ---------- helper: get container directory ----------

get_container_dir() {
    local name="$1"
    local mode="$2"

    case "$mode" in
        lxc)
            echo "$LXC_BASE/$name"
            ;;
        pve-lxc)
            resolve_pve_lxc_dir "$name"
            ;;
        vm|pve-vm)
            echo "$VM_BASE/$name"
            ;;
    esac
}

# ---------- helper: resolve PVE-LXC container dir ----------
# PVE-LXC uses VMID as directory name, not the kento name.
# Scan /var/lib/lxc/*/kento-name for a match.

resolve_pve_lxc_dir() {
    local name="$1"
    for d in "$LXC_BASE"/*/; do
        [ -d "$d" ] || continue
        local nf="$d/kento-name"
        if [ -f "$nf" ] && [ "$(cat "$nf" | tr -d '[:space:]')" = "$name" ]; then
            # Remove trailing slash
            echo "${d%/}"
            return 0
        fi
    done
    echo ""
    return 1
}

# ---------- helper: get host port from kento-port ----------

get_host_port() {
    local container_dir="$1"
    local pf="$container_dir/kento-port"
    if [ -f "$pf" ]; then
        # Format: host_port:guest_port
        cut -d: -f1 < "$pf" | tr -d '[:space:]'
    else
        echo ""
    fi
}

# ---------- helper: wait for RUNNING in kento list ----------

wait_running() {
    local name="$1"
    local timeout="$2"
    local deadline=$((SECONDS + timeout))
    while [ $SECONDS -lt $deadline ]; do
        if kento list 2>/dev/null | grep -qE "^${name}\s.*running"; then
            return 0
        fi
        sleep 1
    done
    return 1
}

# ---------- helper: wait for SSH ----------

wait_ssh() {
    local host="$1"
    local port="$2"
    local key="$3"
    local timeout="$4"
    local deadline=$((SECONDS + timeout))
    while [ $SECONDS -lt $deadline ]; do
        if ssh "${SSH_OPTS[@]}" -i "$key" -p "$port" "root@${host}" true 2>/dev/null; then
            return 0
        fi
        sleep 2
    done
    return 1
}

# ---------- helper: run command in guest via SSH ----------

guest_ssh() {
    local host="$1"
    local port="$2"
    local key="$3"
    shift 3
    ssh "${SSH_OPTS[@]}" -i "$key" -p "$port" "root@${host}" "$@" 2>/dev/null
}

# ---------- helper: SSH parameters per mode ----------

ssh_host_for_mode() {
    local mode="$1"
    case "$mode" in
        lxc)     echo "10.0.3.200" ;;
        pve-lxc) echo "10.0.3.201" ;;
        vm)      echo "localhost"   ;;
        pve-vm)  echo "10.0.3.202" ;;
    esac
}

ssh_port_for_mode() {
    local mode="$1"
    local cdir="$2"
    case "$mode" in
        lxc)     echo "22" ;;
        pve-lxc) echo "22" ;;
        vm)      get_host_port "$cdir" ;;
        pve-vm)  echo "22" ;;
    esac
}

boot_timeout_for_mode() {
    local mode="$1"
    case "$mode" in
        lxc|pve-lxc) echo "$BOOT_TIMEOUT_LXC" ;;
        vm|pve-vm)   echo "$BOOT_TIMEOUT_VM"   ;;
    esac
}

# ---------- helper: expected kento-mode string ----------

expected_mode_string() {
    local mode="$1"
    case "$mode" in
        lxc)     echo "lxc"    ;;
        pve-lxc) echo "pve"    ;;
        vm)      echo "vm"     ;;
        pve-vm)  echo "pve-vm" ;;
    esac
}

# ---------- helper: expected TYPE column in kento list ----------

expected_type_string() {
    local mode="$1"
    case "$mode" in
        lxc)     echo "lxc"     ;;
        pve-lxc) echo "pve-lxc" ;;
        vm)      echo "vm"      ;;
        pve-vm)  echo "pve-vm"  ;;
    esac
}

# ---------- build create command ----------

build_create_cmd() {
    local mode="$1"
    local name="$2"

    case "$mode" in
        lxc)
            echo "kento lxc create $IMAGE --name $name" \
                 "--network bridge=$BRIDGE --ip $LXC_IP --gateway $LXC_GW" \
                 "--ssh-key ${SSH_KEY}.pub --ssh-host-keys" \
                 "--memory 256 --cores 2 --port 10200:22 --no-pve"
            ;;
        pve-lxc)
            echo "kento lxc create $IMAGE --name $name" \
                 "--network bridge=$BRIDGE --ip $PVE_LXC_IP --gateway $PVE_LXC_GW" \
                 "--ssh-key ${SSH_KEY}.pub --ssh-host-keys" \
                 "--memory 256 --cores 2 --port 10201:22 --pve"
            ;;
        vm)
            # --no-pve forces plain QEMU mode even when running on a PVE host.
            echo "kento vm create $IMAGE --name $name" \
                 "--ssh-key ${SSH_KEY}.pub --ssh-host-keys" \
                 "--memory 256 --cores 2 --no-pve"
            ;;
        pve-vm)
            echo "kento vm create $IMAGE --name $name" \
                 "--network bridge=$BRIDGE --ip $PVE_VM_IP --gateway $PVE_VM_GW" \
                 "--ssh-key ${SSH_KEY}.pub --ssh-host-keys" \
                 "--memory 256 --cores 2 --pve"
            ;;
    esac
}

# ==========================================================================
#  TEST PHASES
# ==========================================================================

# ---------- Phase 1: Create and verify metadata ----------

run_phase1() {
    local mode="$1"
    local name
    name="$(instance_name "$mode")"
    local label="$mode"
    set_last_instance "$name" "$mode"

    subgroup_begin "phase1-$mode" || return 0
    diag "--- Phase 1: Create ($mode) ---"

    # Create
    local cmd
    cmd="$(build_create_cmd "$mode" "$name")"
    local output
    output=$(timeout -k 5 "$KTO_CREATE" bash -c "$cmd" 2>&1)
    local rc=$?

    # Test 1: create succeeded
    if [ $rc -eq 0 ]; then
        pass "$label: create succeeded"
    elif [ $rc -eq 124 ] || [ $rc -eq 137 ]; then
        fail "$label: create succeeded" "TIMEOUT after ${KTO_CREATE}s" "$output"
        force_teardown "$name" "$mode"
        return 1
    else
        fail "$label: create succeeded" "exit code $rc" "$output"
        return 1
    fi

    # Resolve container directory
    local cdir
    cdir="$(get_container_dir "$name" "$mode")"
    if [ -z "$cdir" ] || [ ! -d "$cdir" ]; then
        fail "$label: container directory exists" "dir=$cdir not found"
        return 1
    fi

    # Test 2: metadata files exist
    local missing=""
    for f in kento-image kento-layers kento-mode kento-name; do
        [ -f "$cdir/$f" ] || missing="$missing $f"
    done
    if [ -z "$missing" ]; then
        pass "$label: metadata files exist"
    else
        fail "$label: metadata files exist" "missing:$missing in $cdir"
    fi

    # Test 3: mode file correct
    local actual_mode
    actual_mode="$(cat "$cdir/kento-mode" 2>/dev/null | tr -d '[:space:]')"
    local expected_mode
    expected_mode="$(expected_mode_string "$mode")"
    if [ "$actual_mode" = "$expected_mode" ]; then
        pass "$label: mode file correct ($actual_mode)"
    else
        fail "$label: mode file correct" "expected=$expected_mode actual=$actual_mode"
    fi

    # Test 4: config-mode is injection (gemet images have no cloud-init)
    local cfg_mode
    cfg_mode="$(cat "$cdir/kento-config-mode" 2>/dev/null | tr -d '[:space:]')"
    if [ "$cfg_mode" = "injection" ]; then
        pass "$label: config-mode is injection"
    else
        fail "$label: config-mode is injection" "kento-config-mode=$cfg_mode, expected injection"
    fi

    # Test 5: image hold exists
    local hold_name="kento-hold.$name"
    local hold_out
    hold_out="$(podman ps -a --filter "name=^${hold_name}$" --format '{{.Names}}' 2>/dev/null)"
    if [ "$hold_out" = "$hold_name" ]; then
        pass "$label: image hold exists"
    else
        fail "$label: image hold exists" "podman filter returned: '$hold_out'"
    fi

    # Test 6: info works
    local info_out
    info_out="$(kento info "$name" 2>&1)"
    local info_rc=$?
    if [ $info_rc -eq 0 ] && echo "$info_out" | grep -q "$name"; then
        pass "$label: info works"
    else
        fail "$label: info works" "rc=$info_rc output: $info_out"
    fi

    # Test 7: info --json works
    local json_out
    json_out="$(kento info "$name" --json 2>&1)"
    local json_rc=$?
    if [ $json_rc -eq 0 ] && echo "$json_out" | python3 -m json.tool >/dev/null 2>&1; then
        pass "$label: info --json works"
    else
        fail "$label: info --json works" "rc=$json_rc"
    fi

    # Test 8: list shows instance
    local list_out
    list_out="$(kento list 2>&1)"
    local expected_type
    expected_type="$(expected_type_string "$mode")"
    if echo "$list_out" | grep -q "$name" && echo "$list_out" | grep "$name" | grep -qi "$expected_type"; then
        pass "$label: list shows instance with type $expected_type"
    else
        fail "$label: list shows instance with type $expected_type" \
            "name=$name type=$expected_type" \
            "list output: $(echo "$list_out" | grep -i 'e2e' || echo '(no e2e lines)')"
    fi

    return 0
}

# ---------- Phase 2: Start and verify running ----------

run_phase2() {
    local mode="$1"
    local name
    name="$(instance_name "$mode")"
    local label="$mode"
    set_last_instance "$name" "$mode"

    subgroup_begin "phase2-$mode" || return 0
    diag "--- Phase 2: Start ($mode) ---"

    # Test 9: start succeeds
    local start_tmo="$KTO_START_LXC"
    case "$mode" in vm|pve-vm) start_tmo="$KTO_START_VM" ;; esac
    run_kento "$start_tmo" start "$name"
    if [ "$RUN_TIMED_OUT" -eq 1 ]; then
        fail "$label: start succeeds" "TIMEOUT after ${start_tmo}s" "$RUN_OUTPUT"
        force_teardown "$name" "$mode"
        return 1
    fi
    if [ "$RUN_RC" -eq 0 ]; then
        pass "$label: start succeeds"
    else
        fail "$label: start succeeds" "exit code $RUN_RC" "$RUN_OUTPUT"
        return 1
    fi

    # Test 10: shows running
    local timeout
    timeout="$(boot_timeout_for_mode "$mode")"
    if wait_running "$name" "$timeout"; then
        pass "$label: shows running in list"
    else
        fail "$label: shows running in list" "timed out after ${timeout}s"
        return 1
    fi

    return 0
}

# ---------- Phase 3: Guest verification ----------

run_phase3() {
    local mode="$1"
    local name
    name="$(instance_name "$mode")"
    local label="$mode"
    set_last_instance "$name" "$mode"

    subgroup_begin "phase3-$mode" || return 0
    diag "--- Phase 3: Guest verification ($mode) ---"

    local cdir
    cdir="$(get_container_dir "$name" "$mode")"
    local ssh_host
    ssh_host="$(ssh_host_for_mode "$mode")"
    local ssh_port
    ssh_port="$(ssh_port_for_mode "$mode" "$cdir")"

    if [ -z "$ssh_port" ]; then
        fail "$label: SSH port resolution" "could not determine SSH port"
        # Skip remaining phase 3 tests
        skip "$label: hostname correct" "no SSH port"
        skip "$label: static IP correct" "no SSH port"
        skip "$label: port forwarding works" "no SSH port"
        skip "$label: memory limit applied" "no SSH port"
        skip "$label: cores limit applied" "no SSH port"
        return 1
    fi

    # Test 11: SSH reachable
    if wait_ssh "$ssh_host" "$ssh_port" "$SSH_KEY" "$SSH_TIMEOUT"; then
        pass "$label: SSH reachable ($ssh_host:$ssh_port)"
    else
        fail "$label: SSH reachable ($ssh_host:$ssh_port)" "timed out after ${SSH_TIMEOUT}s"
        skip "$label: hostname correct" "SSH unreachable"
        skip "$label: static IP correct" "SSH unreachable"
        skip "$label: port forwarding works" "SSH unreachable"
        skip "$label: memory limit applied" "SSH unreachable"
        skip "$label: cores limit applied" "SSH unreachable"
        return 1
    fi

    # Test 12: hostname correct
    local guest_hostname
    guest_hostname="$(guest_ssh "$ssh_host" "$ssh_port" "$SSH_KEY" hostname)"
    if [ "$guest_hostname" = "$name" ]; then
        pass "$label: hostname correct ($guest_hostname)"
    else
        fail "$label: hostname correct" "expected=$name got=$guest_hostname"
    fi

    # Test 13: static IP correct (lxc, pve-lxc, pve-vm only)
    case "$mode" in
        lxc)
            local expected_ip="10.0.3.200"
            local ip_out
            ip_out="$(guest_ssh "$ssh_host" "$ssh_port" "$SSH_KEY" "ip -4 addr show" 2>/dev/null)"
            if echo "$ip_out" | grep -q "$expected_ip"; then
                pass "$label: static IP correct ($expected_ip)"
            else
                fail "$label: static IP correct ($expected_ip)" \
                    "ip addr output: $(echo "$ip_out" | grep inet || echo '(none)')"
            fi
            ;;
        pve-lxc)
            local expected_ip="10.0.3.201"
            local ip_out
            ip_out="$(guest_ssh "$ssh_host" "$ssh_port" "$SSH_KEY" "ip -4 addr show" 2>/dev/null)"
            if echo "$ip_out" | grep -q "$expected_ip"; then
                pass "$label: static IP correct ($expected_ip)"
            else
                fail "$label: static IP correct ($expected_ip)" \
                    "ip addr output: $(echo "$ip_out" | grep inet || echo '(none)')"
            fi
            ;;
        pve-vm)
            local expected_ip="10.0.3.202"
            local ip_out
            ip_out="$(guest_ssh "$ssh_host" "$ssh_port" "$SSH_KEY" "ip -4 addr show" 2>/dev/null)"
            if echo "$ip_out" | grep -q "$expected_ip"; then
                pass "$label: static IP correct ($expected_ip)"
            else
                fail "$label: static IP correct ($expected_ip)" \
                    "ip addr output: $(echo "$ip_out" | grep inet || echo '(none)')"
            fi
            ;;
        vm)
            skip "$label: static IP correct" "usermode networking, no static IP"
            ;;
    esac

    # Test 14: port forwarding works (lxc, pve-lxc only via forwarded port)
    case "$mode" in
        lxc)
            local fwd_port
            fwd_port="$(get_host_port "$cdir")"
            if [ -n "$fwd_port" ]; then
                if ssh "${SSH_OPTS[@]}" -i "$SSH_KEY" -p "$fwd_port" root@localhost true 2>/dev/null; then
                    pass "$label: port forwarding works (localhost:$fwd_port)"
                else
                    fail "$label: port forwarding works (localhost:$fwd_port)" "SSH via forwarded port failed"
                fi
            else
                fail "$label: port forwarding works" "no kento-port file"
            fi
            ;;
        pve-lxc)
            local fwd_port
            fwd_port="$(get_host_port "$cdir")"
            if [ -n "$fwd_port" ]; then
                if ssh "${SSH_OPTS[@]}" -i "$SSH_KEY" -p "$fwd_port" root@localhost true 2>/dev/null; then
                    pass "$label: port forwarding works (localhost:$fwd_port)"
                else
                    fail "$label: port forwarding works (localhost:$fwd_port)" "SSH via forwarded port failed"
                fi
            else
                fail "$label: port forwarding works" "no kento-port file"
            fi
            ;;
        vm)
            # For VM usermode, the primary SSH is already via port forwarding (test 11)
            pass "$label: port forwarding works (primary SSH uses forwarded port)"
            ;;
        pve-vm)
            skip "$label: port forwarding works" "bridge networking, no port forwarding"
            ;;
    esac

    # Test 15: memory limit applied
    case "$mode" in
        vm|pve-vm)
            # VM: check /proc/meminfo MemTotal is ~256M (180-320MB tolerance)
            local memtotal_kb
            memtotal_kb="$(guest_ssh "$ssh_host" "$ssh_port" "$SSH_KEY" \
                "grep MemTotal /proc/meminfo | awk '{print \$2}'")"
            if [ -n "$memtotal_kb" ]; then
                local memtotal_mb=$((memtotal_kb / 1024))
                if [ "$memtotal_mb" -ge 180 ] && [ "$memtotal_mb" -le 320 ]; then
                    pass "$label: memory limit applied (${memtotal_mb}MB)"
                else
                    fail "$label: memory limit applied" \
                        "MemTotal=${memtotal_mb}MB, expected 180-320MB"
                fi
            else
                fail "$label: memory limit applied" "could not read MemTotal"
            fi
            ;;
        lxc|pve-lxc)
            # LXC: check cgroup memory.max (256*1024*1024 = 268435456)
            local mem_max
            mem_max="$(guest_ssh "$ssh_host" "$ssh_port" "$SSH_KEY" \
                "cat /sys/fs/cgroup/memory.max 2>/dev/null")"
            mem_max="$(echo "$mem_max" | tr -d '[:space:]')"
            if [ "$mem_max" = "268435456" ]; then
                pass "$label: memory limit applied (cgroup memory.max=$mem_max)"
            else
                fail "$label: memory limit applied" \
                    "cgroup memory.max=$mem_max, expected 268435456"
            fi
            ;;
    esac

    # Test 16: cores limit applied
    case "$mode" in
        vm|pve-vm)
            local nproc_out
            nproc_out="$(guest_ssh "$ssh_host" "$ssh_port" "$SSH_KEY" nproc)"
            nproc_out="$(echo "$nproc_out" | tr -d '[:space:]')"
            if [ "$nproc_out" = "2" ]; then
                pass "$label: cores limit applied (nproc=$nproc_out)"
            else
                fail "$label: cores limit applied" "nproc=$nproc_out, expected 2"
            fi
            ;;
        lxc|pve-lxc)
            # LXC: check cpu.max starts with "200000"
            local cpu_max
            cpu_max="$(guest_ssh "$ssh_host" "$ssh_port" "$SSH_KEY" \
                "cat /sys/fs/cgroup/cpu.max 2>/dev/null")"
            if echo "$cpu_max" | grep -q "^200000"; then
                pass "$label: cores limit applied (cpu.max=$cpu_max)"
            else
                fail "$label: cores limit applied" "cpu.max='$cpu_max', expected starts with 200000"
            fi
            ;;
    esac

    return 0
}

# ---------- Phase 4: Scrub and restart ----------

run_phase4() {
    local mode="$1"
    local name
    name="$(instance_name "$mode")"
    local label="$mode"
    set_last_instance "$name" "$mode"

    subgroup_begin "phase4-$mode" || return 0
    diag "--- Phase 4: Scrub and restart ($mode) ---"

    # Test 17: stop succeeds
    run_kento "$KTO_STOP" stop "$name"
    if [ "$RUN_TIMED_OUT" -eq 1 ]; then
        fail "$label: stop succeeds (before scrub)" "TIMEOUT after ${KTO_STOP}s" "$RUN_OUTPUT"
        force_teardown "$name" "$mode"
        return 1
    fi
    if [ "$RUN_RC" -eq 0 ]; then
        pass "$label: stop succeeds (before scrub)"
    else
        fail "$label: stop succeeds (before scrub)" "exit code $RUN_RC" "$RUN_OUTPUT"
        return 1
    fi

    # Test 18: scrub succeeds
    run_kento "$KTO_SCRUB" scrub "$name"
    if [ "$RUN_TIMED_OUT" -eq 1 ]; then
        fail "$label: scrub succeeds" "TIMEOUT after ${KTO_SCRUB}s" "$RUN_OUTPUT"
    elif [ "$RUN_RC" -eq 0 ]; then
        pass "$label: scrub succeeds"
    else
        fail "$label: scrub succeeds" "exit code $RUN_RC" "$RUN_OUTPUT"
    fi

    # Test 19: restart after scrub
    local start_tmo="$KTO_START_LXC"
    case "$mode" in vm|pve-vm) start_tmo="$KTO_START_VM" ;; esac
    run_kento "$start_tmo" start "$name"
    local timeout_s
    timeout_s="$(boot_timeout_for_mode "$mode")"
    if [ "$RUN_TIMED_OUT" -eq 1 ]; then
        fail "$label: restart after scrub" "TIMEOUT after ${start_tmo}s" "$RUN_OUTPUT"
    elif [ "$RUN_RC" -eq 0 ] && wait_running "$name" "$timeout_s"; then
        pass "$label: restart after scrub"
    else
        fail "$label: restart after scrub" "start rc=$RUN_RC" "$RUN_OUTPUT"
    fi

    return 0
}

# ---------- Phase 5: Cleanup (destroy) ----------

run_phase5() {
    local mode="$1"
    local name
    name="$(instance_name "$mode")"
    local label="$mode"
    set_last_instance "$name" "$mode"

    subgroup_begin "phase5-$mode" || return 0
    diag "--- Phase 5: Cleanup ($mode) ---"

    # Resolve container dir before destroy (needed for post-checks)
    local cdir
    cdir="$(get_container_dir "$name" "$mode")"

    # Test 20: stop succeeds
    run_kento "$KTO_STOP" stop "$name"
    if [ "$RUN_TIMED_OUT" -eq 1 ]; then
        fail "$label: stop succeeds (before destroy)" "TIMEOUT after ${KTO_STOP}s" "$RUN_OUTPUT"
        force_teardown "$name" "$mode"
    elif [ "$RUN_RC" -eq 0 ]; then
        pass "$label: stop succeeds (before destroy)"
    else
        fail "$label: stop succeeds (before destroy)" "exit code $RUN_RC" "$RUN_OUTPUT"
    fi

    # Test 21: destroy succeeds
    run_kento "$KTO_DESTROY" destroy -f "$name"
    if [ "$RUN_TIMED_OUT" -eq 1 ]; then
        fail "$label: destroy succeeds" "TIMEOUT after ${KTO_DESTROY}s" "$RUN_OUTPUT"
        force_teardown "$name" "$mode"
    elif [ "$RUN_RC" -eq 0 ]; then
        pass "$label: destroy succeeds"
    else
        fail "$label: destroy succeeds" "exit code $RUN_RC" "$RUN_OUTPUT"
    fi

    # Test 22: directory removed
    if [ -n "$cdir" ] && [ ! -d "$cdir" ]; then
        pass "$label: directory removed ($cdir)"
    else
        fail "$label: directory removed" "directory still exists: $cdir"
    fi

    # Test 23: image hold removed
    local hold_name="kento-hold.$name"
    local hold_out
    hold_out="$(podman ps -a --filter "name=^${hold_name}$" --format '{{.Names}}' 2>/dev/null)"
    if [ -z "$hold_out" ]; then
        pass "$label: image hold removed"
    else
        fail "$label: image hold removed" "podman filter returned: '$hold_out'"
    fi

    return 0
}

# ---------- Phase 6: Error paths ----------

run_phase6() {
    local mode="$1"
    local label="$mode"
    local dup_name="e2e-${mode}-dup"
    set_last_instance "$dup_name" "$mode"

    subgroup_begin "phase6-$mode" || return 0
    diag "--- Phase 6: Error paths ($mode) ---"

    # Build a minimal create command for the dup test
    local create_cmd
    case "$mode" in
        lxc)
            create_cmd="kento lxc create $IMAGE --name $dup_name --no-pve"
            ;;
        pve-lxc)
            create_cmd="kento lxc create $IMAGE --name $dup_name --pve"
            ;;
        vm)
            create_cmd="kento vm create $IMAGE --name $dup_name --no-pve"
            ;;
        pve-vm)
            create_cmd="kento vm create $IMAGE --name $dup_name --pve"
            ;;
    esac

    # First create should succeed
    eval "$create_cmd" >/dev/null 2>&1

    # Test 24: duplicate name rejected
    local dup_out
    dup_out="$(eval "$create_cmd" 2>&1)"
    local dup_rc=$?
    if [ $dup_rc -ne 0 ]; then
        pass "$label: duplicate name rejected"
    else
        fail "$label: duplicate name rejected" "create with duplicate name succeeded (rc=0)"
    fi

    # Clean up the dup instance
    kento stop "$dup_name" 2>/dev/null || true
    kento destroy -f "$dup_name" 2>/dev/null || true

    return 0
}

# ==========================================================================
#  MAIN
# ==========================================================================

diag "kento E2E test suite"
diag "image: $IMAGE"
diag "date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
diag "host: $(hostname)"
diag ""

# Setup
setup_ssh_key
diag "SSH key: $SSH_KEY"
diag ""

# Pre-flight: verify image exists
if ! podman image exists "$IMAGE" 2>/dev/null; then
    echo "Bail out! Image not found: $IMAGE"
    exit 1
fi

# Pre-flight: verify kento is available
if ! command -v kento >/dev/null 2>&1; then
    echo "Bail out! kento not found in PATH"
    exit 1
fi

diag "kento version: $(kento --version 2>&1)"
diag ""

# Run all modes sequentially.
# Each mode goes through phases 1-6 in order.
if section_enabled "phase"; then
for mode in "${MODES[@]}"; do
    if ! mode_enabled "$mode"; then
        diag "skipping mode $mode (filtered)"
        continue
    fi
    diag "========================================"
    diag "MODE: $mode"
    diag "========================================"

    phase1_ok=true
    run_phase1 "$mode" || phase1_ok=false

    if $phase1_ok; then
        phase2_ok=true
        run_phase2 "$mode" || phase2_ok=false

        if $phase2_ok; then
            run_phase3 "$mode"
            run_phase4 "$mode"
        else
            # Skip phase 3 and 4 tests if start failed
            diag "Skipping phases 3-4 for $mode (start failed)"
            for t in "SSH reachable" "hostname correct" "static IP correct" \
                     "port forwarding works" "memory limit applied" "cores limit applied" \
                     "stop succeeds (before scrub)" "scrub succeeds" "restart after scrub"; do
                skip "$mode: $t" "start failed"
            done
        fi

        run_phase5 "$mode"
    else
        # Skip phases 2-5 tests if create failed
        diag "Skipping phases 2-5 for $mode (create failed)"
        for t in "start succeeds" "shows running in list" \
                 "SSH reachable" "hostname correct" "static IP correct" \
                 "port forwarding works" "memory limit applied" "cores limit applied" \
                 "stop succeeds (before scrub)" "scrub succeeds" "restart after scrub" \
                 "stop succeeds (before destroy)" "destroy succeeds" \
                 "directory removed" "image hold removed"; do
            skip "$mode: $t" "create failed"
        done
    fi

    run_phase6 "$mode"

    diag ""
done
else
    diag "section 'phase' disabled by --section filter"
fi

# ==========================================================================
#  SECTION A: FEATURE ISOLATION TESTS
# ==========================================================================
#
# Test each feature in isolation (one feature per instance) to catch bugs
# where a feature only works when combined with others.
# Run on lxc and vm modes only (isolation behavior is mode-independent).

if section_enabled "iso"; then

diag "========================================"
diag "SECTION A: Feature isolation tests"
diag "========================================"

# ---------- helper: isolation create-start-stop-destroy lifecycle ----------

iso_lifecycle() {
    local name="$1"
    local create_cmd="$2"
    local label="$3"
    local boot_timeout="${4:-$BOOT_TIMEOUT_LXC}"

    # Create
    local output
    output=$(eval "$create_cmd" 2>&1)
    local rc=$?
    if [ $rc -eq 0 ]; then
        pass "$label: create"
    else
        fail "$label: create" "exit code $rc" "$output"
        return 1
    fi

    # Start
    local start_out
    start_out="$(kento start "$name" 2>&1)"
    local start_rc=$?
    if [ $start_rc -eq 0 ]; then
        pass "$label: start"
    else
        fail "$label: start" "exit code $start_rc" "$start_out"
        # Still try to destroy for cleanup
        kento destroy -f "$name" 2>/dev/null || true
        return 1
    fi

    # Wait for running
    if wait_running "$name" "$boot_timeout"; then
        pass "$label: running"
    else
        fail "$label: running" "timed out after ${boot_timeout}s"
    fi

    return 0
}

iso_teardown() {
    local name="$1"
    local label="$2"

    # Stop
    local stop_out
    stop_out="$(kento stop "$name" 2>&1)"
    local stop_rc=$?
    if [ $stop_rc -eq 0 ]; then
        pass "$label: stop"
    else
        fail "$label: stop" "exit code $stop_rc" "$stop_out"
    fi

    # Destroy
    local destroy_out
    destroy_out="$(kento destroy -f "$name" 2>&1)"
    local destroy_rc=$?
    if [ $destroy_rc -eq 0 ]; then
        pass "$label: destroy"
    else
        fail "$label: destroy" "exit code $destroy_rc" "$destroy_out"
    fi
}

# --- A1: Plain create (LXC) ---

if subgroup_begin "iso-plain-lxc"; then
diag "--- A1: Plain create (LXC) ---"
ISO_PLAIN_LXC="e2e-iso-plain-lxc"
ISO_PLAIN_LABEL="iso-plain-lxc"
set_last_instance "$ISO_PLAIN_LXC" "lxc"

if iso_lifecycle "$ISO_PLAIN_LXC" \
    "kento lxc create $IMAGE --name $ISO_PLAIN_LXC --no-pve" \
    "$ISO_PLAIN_LABEL" "$BOOT_TIMEOUT_LXC"; then
    iso_teardown "$ISO_PLAIN_LXC" "$ISO_PLAIN_LABEL"
else
    force_teardown "$ISO_PLAIN_LXC" "lxc"
fi
subgroup_end
fi

# --- A1b: Plain create (VM) ---

if subgroup_begin "iso-plain-vm"; then
diag "--- A1b: Plain create (VM) ---"
ISO_PLAIN_VM="e2e-iso-plain-vm"
ISO_PLAIN_VM_LABEL="iso-plain-vm"
set_last_instance "$ISO_PLAIN_VM" "vm"

if iso_lifecycle "$ISO_PLAIN_VM" \
    "kento vm create $IMAGE --name $ISO_PLAIN_VM --no-pve" \
    "$ISO_PLAIN_VM_LABEL" "$BOOT_TIMEOUT_VM"; then
    iso_teardown "$ISO_PLAIN_VM" "$ISO_PLAIN_VM_LABEL"
else
    force_teardown "$ISO_PLAIN_VM" "vm"
fi
subgroup_end
fi

# --- A2: SSH key only (LXC) ---

if subgroup_begin "iso-ssh-lxc"; then
diag "--- A2: SSH key only (LXC) ---"
ISO_SSH_LXC="e2e-iso-ssh-lxc"
ISO_SSH_LXC_LABEL="iso-ssh-lxc"
set_last_instance "$ISO_SSH_LXC" "lxc"

if iso_lifecycle "$ISO_SSH_LXC" \
    "kento lxc create $IMAGE --name $ISO_SSH_LXC --network bridge=$BRIDGE --ssh-key ${SSH_KEY}.pub --no-pve" \
    "$ISO_SSH_LXC_LABEL" "$BOOT_TIMEOUT_LXC"; then

    # Verify SSH works — need to find the container's IP via lxc-attach.
    # DHCP can take several seconds after boot, so poll until an IP shows up.
    local_ssh_ip=""
    ip_deadline=$((SECONDS + 20))
    while [ $SECONDS -lt $ip_deadline ]; do
        local_ssh_ip="$(lxc-attach -n "$ISO_SSH_LXC" -- ip -4 addr show 2>/dev/null \
            | grep 'inet ' | grep -v '127.0.0.1' | awk '{print $2}' | cut -d/ -f1 | head -1)"
        [ -n "$local_ssh_ip" ] && break
        sleep 1
    done
    if [ -n "$local_ssh_ip" ]; then
        if wait_ssh "$local_ssh_ip" 22 "$SSH_KEY" "$SSH_TIMEOUT"; then
            pass "$ISO_SSH_LXC_LABEL: SSH with key works ($local_ssh_ip)"
        else
            fail "$ISO_SSH_LXC_LABEL: SSH with key works" "timed out connecting to $local_ssh_ip:22"
        fi
    else
        fail "$ISO_SSH_LXC_LABEL: SSH with key works" "could not determine guest IP"
    fi

    iso_teardown "$ISO_SSH_LXC" "$ISO_SSH_LXC_LABEL"
else
    force_teardown "$ISO_SSH_LXC" "lxc"
fi
subgroup_end
fi

# --- A2b: SSH key only (VM) ---

if subgroup_begin "iso-ssh-vm"; then
diag "--- A2b: SSH key only (VM) ---"
ISO_SSH_VM="e2e-iso-ssh-vm"
ISO_SSH_VM_LABEL="iso-ssh-vm"
set_last_instance "$ISO_SSH_VM" "vm"

if iso_lifecycle "$ISO_SSH_VM" \
    "kento vm create $IMAGE --name $ISO_SSH_VM --ssh-key ${SSH_KEY}.pub --no-pve" \
    "$ISO_SSH_VM_LABEL" "$BOOT_TIMEOUT_VM"; then

    # VM usermode: SSH via port forwarding. Get the host port.
    iso_ssh_vm_cdir="$VM_BASE/$ISO_SSH_VM"
    iso_ssh_vm_port="$(get_host_port "$iso_ssh_vm_cdir")"
    if [ -n "$iso_ssh_vm_port" ]; then
        if wait_ssh "localhost" "$iso_ssh_vm_port" "$SSH_KEY" "$SSH_TIMEOUT"; then
            pass "$ISO_SSH_VM_LABEL: SSH with key works (localhost:$iso_ssh_vm_port)"
        else
            fail "$ISO_SSH_VM_LABEL: SSH with key works" "timed out connecting to localhost:$iso_ssh_vm_port"
        fi
    else
        # No port forwarding configured — just verify it started (already done above)
        skip "$ISO_SSH_VM_LABEL: SSH with key works" "no port forwarding — no SSH path into VM"
    fi

    iso_teardown "$ISO_SSH_VM" "$ISO_SSH_VM_LABEL"
else
    force_teardown "$ISO_SSH_VM" "vm"
fi
subgroup_end
fi

# --- A3: Memory only (LXC) ---

if subgroup_begin "iso-mem-lxc"; then
diag "--- A3: Memory only (LXC) ---"
ISO_MEM_LXC="e2e-iso-mem-lxc"
ISO_MEM_LXC_LABEL="iso-mem-lxc"
set_last_instance "$ISO_MEM_LXC" "lxc"

if iso_lifecycle "$ISO_MEM_LXC" \
    "kento lxc create $IMAGE --name $ISO_MEM_LXC --memory 384 --no-pve" \
    "$ISO_MEM_LXC_LABEL" "$BOOT_TIMEOUT_LXC"; then

    # Verify memory via lxc-attach (384*1024*1024 = 402653184)
    mem_max="$(lxc-attach -n "$ISO_MEM_LXC" -- cat /sys/fs/cgroup/memory.max 2>/dev/null | tr -d '[:space:]')"
    if [ "$mem_max" = "402653184" ]; then
        pass "$ISO_MEM_LXC_LABEL: memory 384MB applied (memory.max=$mem_max)"
    else
        fail "$ISO_MEM_LXC_LABEL: memory 384MB applied" "memory.max=$mem_max, expected 402653184"
    fi

    iso_teardown "$ISO_MEM_LXC" "$ISO_MEM_LXC_LABEL"
else
    force_teardown "$ISO_MEM_LXC" "lxc"
fi
subgroup_end
fi

# --- A3b: Memory only (VM) ---

if subgroup_begin "iso-mem-vm"; then
diag "--- A3b: Memory only (VM) ---"
ISO_MEM_VM="e2e-iso-mem-vm"
ISO_MEM_VM_LABEL="iso-mem-vm"
set_last_instance "$ISO_MEM_VM" "vm"

if iso_lifecycle "$ISO_MEM_VM" \
    "kento vm create $IMAGE --name $ISO_MEM_VM --memory 384 --no-pve" \
    "$ISO_MEM_VM_LABEL" "$BOOT_TIMEOUT_VM"; then

    # Can't SSH in (no key), verify metadata file instead
    iso_mem_vm_cdir="$VM_BASE/$ISO_MEM_VM"
    mem_meta="$(cat "$iso_mem_vm_cdir/kento-memory" 2>/dev/null | tr -d '[:space:]')"
    if [ "$mem_meta" = "384" ]; then
        pass "$ISO_MEM_VM_LABEL: memory metadata correct ($mem_meta)"
    else
        fail "$ISO_MEM_VM_LABEL: memory metadata correct" "kento-memory=$mem_meta, expected 384"
    fi

    iso_teardown "$ISO_MEM_VM" "$ISO_MEM_VM_LABEL"
else
    force_teardown "$ISO_MEM_VM" "vm"
fi
subgroup_end
fi

# --- A4: Cores only (LXC) ---

if subgroup_begin "iso-cores-lxc"; then
diag "--- A4: Cores only (LXC) ---"
ISO_CORES_LXC="e2e-iso-cores-lxc"
ISO_CORES_LXC_LABEL="iso-cores-lxc"
set_last_instance "$ISO_CORES_LXC" "lxc"

if iso_lifecycle "$ISO_CORES_LXC" \
    "kento lxc create $IMAGE --name $ISO_CORES_LXC --cores 1 --no-pve" \
    "$ISO_CORES_LXC_LABEL" "$BOOT_TIMEOUT_LXC"; then

    # Verify cores via lxc-attach: cpu.max should start with "100000"
    cpu_max="$(lxc-attach -n "$ISO_CORES_LXC" -- cat /sys/fs/cgroup/cpu.max 2>/dev/null)"
    if echo "$cpu_max" | grep -q "^100000"; then
        pass "$ISO_CORES_LXC_LABEL: cores 1 applied (cpu.max=$cpu_max)"
    else
        fail "$ISO_CORES_LXC_LABEL: cores 1 applied" "cpu.max='$cpu_max', expected starts with 100000"
    fi

    iso_teardown "$ISO_CORES_LXC" "$ISO_CORES_LXC_LABEL"
else
    force_teardown "$ISO_CORES_LXC" "lxc"
fi
subgroup_end
fi

# --- A4b: Cores only (VM) ---

if subgroup_begin "iso-cores-vm"; then
diag "--- A4b: Cores only (VM) ---"
ISO_CORES_VM="e2e-iso-cores-vm"
ISO_CORES_VM_LABEL="iso-cores-vm"
set_last_instance "$ISO_CORES_VM" "vm"

if iso_lifecycle "$ISO_CORES_VM" \
    "kento vm create $IMAGE --name $ISO_CORES_VM --cores 1 --no-pve" \
    "$ISO_CORES_VM_LABEL" "$BOOT_TIMEOUT_VM"; then

    # Can't SSH in (no key), verify metadata file instead
    iso_cores_vm_cdir="$VM_BASE/$ISO_CORES_VM"
    cores_meta="$(cat "$iso_cores_vm_cdir/kento-cores" 2>/dev/null | tr -d '[:space:]')"
    if [ "$cores_meta" = "1" ]; then
        pass "$ISO_CORES_VM_LABEL: cores metadata correct ($cores_meta)"
    else
        fail "$ISO_CORES_VM_LABEL: cores metadata correct" "kento-cores=$cores_meta, expected 1"
    fi

    iso_teardown "$ISO_CORES_VM" "$ISO_CORES_VM_LABEL"
else
    force_teardown "$ISO_CORES_VM" "vm"
fi
subgroup_end
fi

# --- A5: Port only (LXC) ---

if subgroup_begin "iso-port-lxc"; then
diag "--- A5: Port only (LXC) ---"
ISO_PORT_LXC="e2e-iso-port-lxc"
ISO_PORT_LXC_LABEL="iso-port-lxc"
set_last_instance "$ISO_PORT_LXC" "lxc"

if iso_lifecycle "$ISO_PORT_LXC" \
    "kento lxc create $IMAGE --name $ISO_PORT_LXC --network bridge=$BRIDGE --port 10250:22 --no-pve" \
    "$ISO_PORT_LXC_LABEL" "$BOOT_TIMEOUT_LXC"; then

    # Verify port forwarding: wait for guest DHCP to acquire IPv4 (portfwd
    # needs a destination) then try connecting to localhost:10250. A fixed
    # sleep here races with DHCP acquisition (seen taking 3+s under load).
    port_deadline=$((SECONDS + 20))
    port_ipv4=""
    while [ $SECONDS -lt $port_deadline ]; do
        port_ipv4=$(lxc-attach -n "$ISO_PORT_LXC" -- ip -4 addr show 2>/dev/null \
            | grep 'inet ' | grep -v '127.0.0.1' | awk '{print $2}' | cut -d/ -f1 | head -1)
        [ -n "$port_ipv4" ] && break
        sleep 1
    done
    if timeout 5 bash -c "echo >/dev/tcp/localhost/10250" 2>/dev/null; then
        pass "$ISO_PORT_LXC_LABEL: port 10250:22 forwarding works"
    else
        fail "$ISO_PORT_LXC_LABEL: port 10250:22 forwarding works" \
             "could not connect to localhost:10250 (guest ipv4=$port_ipv4)"
    fi

    iso_teardown "$ISO_PORT_LXC" "$ISO_PORT_LXC_LABEL"
else
    force_teardown "$ISO_PORT_LXC" "lxc"
fi
subgroup_end
fi

# --- A5b: Port only (VM) ---

if subgroup_begin "iso-port-vm"; then
diag "--- A5b: Port only (VM) ---"
ISO_PORT_VM="e2e-iso-port-vm"
ISO_PORT_VM_LABEL="iso-port-vm"
set_last_instance "$ISO_PORT_VM" "vm"

if iso_lifecycle "$ISO_PORT_VM" \
    "kento vm create $IMAGE --name $ISO_PORT_VM --port 10251:22 --no-pve" \
    "$ISO_PORT_VM_LABEL" "$BOOT_TIMEOUT_VM"; then

    # Verify port forwarding: check localhost:10251 is open
    sleep 3
    if timeout 5 bash -c "echo >/dev/tcp/localhost/10251" 2>/dev/null; then
        pass "$ISO_PORT_VM_LABEL: port 10251:22 forwarding works"
    else
        fail "$ISO_PORT_VM_LABEL: port 10251:22 forwarding works" "could not connect to localhost:10251"
    fi

    iso_teardown "$ISO_PORT_VM" "$ISO_PORT_VM_LABEL"
else
    force_teardown "$ISO_PORT_VM" "vm"
fi
subgroup_end
fi

# --- A6: Static IP only (LXC only) ---

if subgroup_begin "iso-ip-lxc"; then
diag "--- A6: Static IP only (LXC) ---"
ISO_IP_LXC="e2e-iso-ip-lxc"
ISO_IP_LXC_LABEL="iso-ip-lxc"
set_last_instance "$ISO_IP_LXC" "lxc"

if iso_lifecycle "$ISO_IP_LXC" \
    "kento lxc create $IMAGE --name $ISO_IP_LXC --network bridge=$BRIDGE --ip 10.0.3.210/24 --gateway 10.0.3.1 --no-pve" \
    "$ISO_IP_LXC_LABEL" "$BOOT_TIMEOUT_LXC"; then

    # Verify IP via lxc-attach
    guest_ip="$(lxc-attach -n "$ISO_IP_LXC" -- ip -4 addr show 2>/dev/null \
        | grep 'inet ' | grep -v '127.0.0.1' | awk '{print $2}' | cut -d/ -f1 | head -1)"
    if [ "$guest_ip" = "10.0.3.210" ]; then
        pass "$ISO_IP_LXC_LABEL: static IP 10.0.3.210 applied ($guest_ip)"
    else
        fail "$ISO_IP_LXC_LABEL: static IP 10.0.3.210 applied" "guest IP=$guest_ip, expected 10.0.3.210"
    fi

    iso_teardown "$ISO_IP_LXC" "$ISO_IP_LXC_LABEL"
else
    force_teardown "$ISO_IP_LXC" "lxc"
fi
subgroup_end
fi

if subgroup_begin "iso-ip-vm"; then
diag "--- A6b: Static IP (VM) — skipped (usermode networking) ---"
skip "iso-ip-vm: static IP" "VM uses usermode networking, no static IP test"
subgroup_end
fi

# --- A7: Injection mode forced (LXC) ---

if subgroup_begin "iso-inject-lxc"; then
diag "--- A7: Injection mode forced (LXC) ---"
ISO_INJ_LXC="e2e-iso-inject-lxc"
ISO_INJ_LXC_LABEL="iso-inject-lxc"
set_last_instance "$ISO_INJ_LXC" "lxc"

create_out="$(kento lxc create $IMAGE --name $ISO_INJ_LXC --config-mode injection --no-pve 2>&1)"
create_rc=$?
if [ $create_rc -eq 0 ]; then
    pass "$ISO_INJ_LXC_LABEL: create with --config-mode injection"

    iso_inj_cdir="$LXC_BASE/$ISO_INJ_LXC"
    cfg_mode="$(cat "$iso_inj_cdir/kento-config-mode" 2>/dev/null | tr -d '[:space:]')"
    if [ "$cfg_mode" = "injection" ]; then
        pass "$ISO_INJ_LXC_LABEL: config-mode is injection"
    else
        fail "$ISO_INJ_LXC_LABEL: config-mode is injection" "kento-config-mode=$cfg_mode, expected injection"
    fi

    # No need to boot — just verify metadata, then destroy
    kento destroy -f "$ISO_INJ_LXC" 2>/dev/null
    pass "$ISO_INJ_LXC_LABEL: destroy"
else
    fail "$ISO_INJ_LXC_LABEL: create with --config-mode injection" "exit code $create_rc" "$create_out"
fi
subgroup_end
fi

# --- A7b: Injection mode forced (VM) ---

if subgroup_begin "iso-inject-vm"; then
diag "--- A7b: Injection mode forced (VM) ---"
ISO_INJ_VM="e2e-iso-inject-vm"
ISO_INJ_VM_LABEL="iso-inject-vm"
set_last_instance "$ISO_INJ_VM" "vm"

create_out="$(kento vm create $IMAGE --name $ISO_INJ_VM --config-mode injection --no-pve 2>&1)"
create_rc=$?
if [ $create_rc -eq 0 ]; then
    pass "$ISO_INJ_VM_LABEL: create with --config-mode injection"

    iso_inj_vm_cdir="$VM_BASE/$ISO_INJ_VM"
    cfg_mode="$(cat "$iso_inj_vm_cdir/kento-config-mode" 2>/dev/null | tr -d '[:space:]')"
    if [ "$cfg_mode" = "injection" ]; then
        pass "$ISO_INJ_VM_LABEL: config-mode is injection"
    else
        fail "$ISO_INJ_VM_LABEL: config-mode is injection" "kento-config-mode=$cfg_mode, expected injection"
    fi

    # No need to boot — just verify metadata, then destroy
    kento destroy -f "$ISO_INJ_VM" 2>/dev/null
    pass "$ISO_INJ_VM_LABEL: destroy"
else
    fail "$ISO_INJ_VM_LABEL: create with --config-mode injection" "exit code $create_rc" "$create_out"
fi
subgroup_end
fi

diag ""

fi  # end section_enabled "iso"

# ==========================================================================
#  SECTION B: MULTI-IMAGE TESTS
# ==========================================================================
#
# Test different gemet image variants to verify kento works beyond bifrost.

if section_enabled "image"; then

diag "========================================"
diag "SECTION B: Multi-image tests"
diag "========================================"

YGG_IMAGE="localhost/gemet-ygg-kento:1.4.2"

# Pre-flight: check if yggdrasil image exists
if podman image exists "$YGG_IMAGE" 2>/dev/null; then

    # --- B1: Yggdrasil LXC lifecycle ---

    if subgroup_begin "ygg-lxc"; then
    diag "--- B1: Yggdrasil LXC lifecycle ---"
    YGG_LXC="e2e-ygg-lxc"
    YGG_LXC_LABEL="ygg-lxc"
    set_last_instance "$YGG_LXC" "lxc"

    ygg_lxc_out="$(kento lxc create "$YGG_IMAGE" --name "$YGG_LXC" --no-pve 2>&1)"
    ygg_lxc_rc=$?
    if [ $ygg_lxc_rc -eq 0 ]; then
        pass "$YGG_LXC_LABEL: create"

        # Verify config-mode is injection (yggdrasil has no cloud-init)
        ygg_lxc_cdir="$LXC_BASE/$YGG_LXC"
        ygg_cfg_mode="$(cat "$ygg_lxc_cdir/kento-config-mode" 2>/dev/null | tr -d '[:space:]')"
        if [ "$ygg_cfg_mode" = "injection" ]; then
            pass "$YGG_LXC_LABEL: config-mode is injection"
        else
            fail "$YGG_LXC_LABEL: config-mode is injection" "kento-config-mode=$ygg_cfg_mode, expected injection"
        fi

        # Start and wait for running
        ygg_start_out="$(kento start "$YGG_LXC" 2>&1)"
        ygg_start_rc=$?
        if [ $ygg_start_rc -eq 0 ]; then
            pass "$YGG_LXC_LABEL: start"

            if wait_running "$YGG_LXC" "$BOOT_TIMEOUT_LXC"; then
                pass "$YGG_LXC_LABEL: running"
            else
                fail "$YGG_LXC_LABEL: running" "timed out after ${BOOT_TIMEOUT_LXC}s"
            fi
        else
            fail "$YGG_LXC_LABEL: start" "exit code $ygg_start_rc" "$ygg_start_out"
            skip "$YGG_LXC_LABEL: running" "start failed"
        fi

        # Stop
        ygg_stop_out="$(kento stop "$YGG_LXC" 2>&1)"
        ygg_stop_rc=$?
        if [ $ygg_stop_rc -eq 0 ]; then
            pass "$YGG_LXC_LABEL: stop"
        else
            fail "$YGG_LXC_LABEL: stop" "exit code $ygg_stop_rc" "$ygg_stop_out"
        fi

        # Destroy
        ygg_destroy_out="$(kento destroy -f "$YGG_LXC" 2>&1)"
        ygg_destroy_rc=$?
        if [ $ygg_destroy_rc -eq 0 ]; then
            pass "$YGG_LXC_LABEL: destroy"
        else
            fail "$YGG_LXC_LABEL: destroy" "exit code $ygg_destroy_rc" "$ygg_destroy_out"
        fi
    else
        fail "$YGG_LXC_LABEL: create" "exit code $ygg_lxc_rc" "$ygg_lxc_out"
        for t in "config-mode is injection" "start" "running" "stop" "destroy"; do
            skip "$YGG_LXC_LABEL: $t" "create failed"
        done
    fi
    subgroup_end
    fi

    # --- B2: Yggdrasil VM lifecycle ---

    if subgroup_begin "ygg-vm"; then
    diag "--- B2: Yggdrasil VM lifecycle ---"
    YGG_VM="e2e-ygg-vm"
    YGG_VM_LABEL="ygg-vm"
    set_last_instance "$YGG_VM" "vm"

    ygg_vm_out="$(kento vm create "$YGG_IMAGE" --name "$YGG_VM" --no-pve 2>&1)"
    ygg_vm_rc=$?
    if [ $ygg_vm_rc -eq 0 ]; then
        pass "$YGG_VM_LABEL: create"

        # Verify config-mode is injection (yggdrasil has no cloud-init)
        ygg_vm_cdir="$VM_BASE/$YGG_VM"
        ygg_vm_cfg_mode="$(cat "$ygg_vm_cdir/kento-config-mode" 2>/dev/null | tr -d '[:space:]')"
        if [ "$ygg_vm_cfg_mode" = "injection" ]; then
            pass "$YGG_VM_LABEL: config-mode is injection"
        else
            fail "$YGG_VM_LABEL: config-mode is injection" "kento-config-mode=$ygg_vm_cfg_mode, expected injection"
        fi

        # Start and wait for running
        ygg_vm_start_out="$(kento start "$YGG_VM" 2>&1)"
        ygg_vm_start_rc=$?
        if [ $ygg_vm_start_rc -eq 0 ]; then
            pass "$YGG_VM_LABEL: start"

            if wait_running "$YGG_VM" "$BOOT_TIMEOUT_VM"; then
                pass "$YGG_VM_LABEL: running"
            else
                fail "$YGG_VM_LABEL: running" "timed out after ${BOOT_TIMEOUT_VM}s"
            fi
        else
            fail "$YGG_VM_LABEL: start" "exit code $ygg_vm_start_rc" "$ygg_vm_start_out"
            skip "$YGG_VM_LABEL: running" "start failed"
        fi

        # Stop
        ygg_vm_stop_out="$(kento stop "$YGG_VM" 2>&1)"
        ygg_vm_stop_rc=$?
        if [ $ygg_vm_stop_rc -eq 0 ]; then
            pass "$YGG_VM_LABEL: stop"
        else
            fail "$YGG_VM_LABEL: stop" "exit code $ygg_vm_stop_rc" "$ygg_vm_stop_out"
        fi

        # Destroy
        ygg_vm_destroy_out="$(kento destroy -f "$YGG_VM" 2>&1)"
        ygg_vm_destroy_rc=$?
        if [ $ygg_vm_destroy_rc -eq 0 ]; then
            pass "$YGG_VM_LABEL: destroy"
        else
            fail "$YGG_VM_LABEL: destroy" "exit code $ygg_vm_destroy_rc" "$ygg_vm_destroy_out"
        fi
    else
        fail "$YGG_VM_LABEL: create" "exit code $ygg_vm_rc" "$ygg_vm_out"
        for t in "config-mode is injection" "start" "running" "stop" "destroy"; do
            skip "$YGG_VM_LABEL: $t" "create failed"
        done
    fi
    subgroup_end
    fi

else
    diag "Yggdrasil image ($YGG_IMAGE) not found, skipping B1-B2"
    for label in "ygg-lxc" "ygg-vm"; do
        for t in "create" "config-mode is injection" "start" "running" "stop" "destroy"; do
            skip "$label: $t" "image not available"
        done
    done
fi

# --- B3: Canopy — not available ---
diag "--- B3: Canopy image not available on test VM (not yet composed) ---"

diag ""

# ==========================================================================
#  SECTION C: Cloud-init integration (droste-hair)
# ==========================================================================
#
# droste-hair is built from Debian with cloud-init installed. Use it to
# exercise kento's cloud-init config-mode path (seed generation, override,
# and end-to-end boot where cloud-init actually runs inside the guest).
# LXC mode only — VM mode would also work but would double the runtime.

diag "========================================"
diag "SECTION C: Cloud-init integration (droste-hair)"
diag "========================================"

HAIR_IMAGE="ghcr.io/doctorjei/droste-hair:latest"

if podman image exists "$HAIR_IMAGE" 2>/dev/null; then

    if subgroup_begin "droste-hair-ci"; then
    diag "--- C1: droste-hair cloud-init (LXC) ---"
    HAIR_LXC="e2e-droste-hair-ci"
    HAIR_LXC_IP="e2e-droste-hair-ci-ip"
    HAIR_LXC_INJ="e2e-droste-hair-ci-inj"
    HAIR_LABEL="droste-hair-ci"
    set_last_instance "$HAIR_LXC" "lxc"

    # C1.1: create (auto mode, cloud-init should be detected)
    hair_out="$(kento lxc create "$HAIR_IMAGE" --name "$HAIR_LXC" --no-pve 2>&1)"
    hair_rc=$?
    if [ $hair_rc -eq 0 ]; then
        pass "$HAIR_LABEL: create"

        # C1.2: config-mode is cloudinit
        hair_cdir="$LXC_BASE/$HAIR_LXC"
        hair_cfg_mode="$(cat "$hair_cdir/kento-config-mode" 2>/dev/null | tr -d '[:space:]')"
        if [ "$hair_cfg_mode" = "cloudinit" ]; then
            pass "$HAIR_LABEL: config-mode is cloudinit"
        else
            fail "$HAIR_LABEL: config-mode is cloudinit" "kento-config-mode=$hair_cfg_mode, expected cloudinit"
        fi

        # C1.3: cloud-seed files exist
        if [ -f "$hair_cdir/cloud-seed/meta-data" ] && [ -f "$hair_cdir/cloud-seed/user-data" ]; then
            pass "$HAIR_LABEL: cloud-seed files exist"
        else
            fail "$HAIR_LABEL: cloud-seed files exist" "missing meta-data or user-data under $hair_cdir/cloud-seed/"
        fi

        # C1.4: meta-data hostname matches container name
        if grep -q "local-hostname: $HAIR_LXC" "$hair_cdir/cloud-seed/meta-data" 2>/dev/null; then
            pass "$HAIR_LABEL: meta-data hostname"
        else
            fail "$HAIR_LABEL: meta-data hostname" "'local-hostname: $HAIR_LXC' not found in meta-data"
        fi

        # Destroy the auto-mode container so we can reuse the name later.
        hair_destroy_out="$(kento destroy -f "$HAIR_LXC" 2>&1)"
        hair_destroy_rc=$?
        if [ $hair_destroy_rc -ne 0 ]; then
            diag "$HAIR_LABEL: intermediate destroy failed: $hair_destroy_out"
            force_teardown "$HAIR_LXC" "lxc"
        fi
    else
        fail "$HAIR_LABEL: create" "exit code $hair_rc" "$hair_out"
        for t in "config-mode is cloudinit" "cloud-seed files exist" "meta-data hostname"; do
            skip "$HAIR_LABEL: $t" "create failed"
        done
        force_teardown "$HAIR_LXC" "lxc"
    fi

    # C1.5: network-config written when --ip is provided
    set_last_instance "$HAIR_LXC_IP" "lxc"
    hair_ip_out="$(kento lxc create "$HAIR_IMAGE" --name "$HAIR_LXC_IP" \
        --ip 10.0.3.210/24 --gateway 10.0.3.1 --no-pve 2>&1)"
    hair_ip_rc=$?
    if [ $hair_ip_rc -eq 0 ]; then
        hair_ip_cdir="$LXC_BASE/$HAIR_LXC_IP"
        if [ -f "$hair_ip_cdir/cloud-seed/network-config" ]; then
            pass "$HAIR_LABEL: network-config written with --ip"
        else
            fail "$HAIR_LABEL: network-config written with --ip" "missing $hair_ip_cdir/cloud-seed/network-config"
        fi
        hair_ip_destroy_out="$(kento destroy -f "$HAIR_LXC_IP" 2>&1)"
        if [ $? -ne 0 ]; then
            diag "$HAIR_LABEL: --ip container destroy failed: $hair_ip_destroy_out"
            force_teardown "$HAIR_LXC_IP" "lxc"
        fi
    else
        fail "$HAIR_LABEL: network-config written with --ip" "create rc=$hair_ip_rc" "$hair_ip_out"
        force_teardown "$HAIR_LXC_IP" "lxc"
    fi

    # C1.6: injection override beats cloud-init detection
    set_last_instance "$HAIR_LXC_INJ" "lxc"
    hair_inj_out="$(kento lxc create "$HAIR_IMAGE" --name "$HAIR_LXC_INJ" \
        --config-mode injection --no-pve 2>&1)"
    hair_inj_rc=$?
    if [ $hair_inj_rc -eq 0 ]; then
        hair_inj_cdir="$LXC_BASE/$HAIR_LXC_INJ"
        hair_inj_mode="$(cat "$hair_inj_cdir/kento-config-mode" 2>/dev/null | tr -d '[:space:]')"
        if [ "$hair_inj_mode" = "injection" ]; then
            pass "$HAIR_LABEL: injection override"
        else
            fail "$HAIR_LABEL: injection override" "kento-config-mode=$hair_inj_mode, expected injection"
        fi
        hair_inj_destroy_out="$(kento destroy -f "$HAIR_LXC_INJ" 2>&1)"
        if [ $? -ne 0 ]; then
            diag "$HAIR_LABEL: injection container destroy failed: $hair_inj_destroy_out"
            force_teardown "$HAIR_LXC_INJ" "lxc"
        fi
    else
        fail "$HAIR_LABEL: injection override" "create rc=$hair_inj_rc" "$hair_inj_out"
        force_teardown "$HAIR_LXC_INJ" "lxc"
    fi

    # C1.7: end-to-end — boot the container and verify cloud-init actually ran.
    set_last_instance "$HAIR_LXC" "lxc"
    hair_e2e_out="$(kento lxc create "$HAIR_IMAGE" --name "$HAIR_LXC" \
        --network bridge=$BRIDGE --ssh-key "${SSH_KEY}.pub" --no-pve 2>&1)"
    hair_e2e_rc=$?
    if [ $hair_e2e_rc -eq 0 ]; then
        hair_start_out="$(kento start "$HAIR_LXC" 2>&1)"
        hair_start_rc=$?
        if [ $hair_start_rc -eq 0 ]; then
            if wait_running "$HAIR_LXC" "$BOOT_TIMEOUT_LXC"; then
                # Poll for guest IPv4 via lxc-attach (same pattern as iso-ssh-lxc).
                hair_ip=""
                hair_ip_deadline=$((SECONDS + 20))
                while [ $SECONDS -lt $hair_ip_deadline ]; do
                    hair_ip="$(lxc-attach -n "$HAIR_LXC" -- ip -4 addr show 2>/dev/null \
                        | grep 'inet ' | grep -v '127.0.0.1' | awk '{print $2}' | cut -d/ -f1 | head -1)"
                    [ -n "$hair_ip" ] && break
                    sleep 1
                done
                if [ -n "$hair_ip" ] && wait_ssh "$hair_ip" 22 "$SSH_KEY" "$SSH_TIMEOUT"; then
                    # cloud-init status --wait blocks until cloud-init is done; exits 0 on success.
                    hair_ci_out="$(timeout 60 ssh "${SSH_OPTS[@]}" -i "$SSH_KEY" -p 22 \
                        "root@${hair_ip}" 'cloud-init status --wait' </dev/null 2>&1)"
                    hair_ci_rc=$?
                    if [ $hair_ci_rc -eq 0 ]; then
                        pass "$HAIR_LABEL: cloud-init ran"
                    else
                        fail "$HAIR_LABEL: cloud-init ran" "rc=$hair_ci_rc" "$hair_ci_out"
                    fi
                else
                    fail "$HAIR_LABEL: cloud-init ran" "could not reach guest via SSH (ip=$hair_ip)"
                fi
            else
                fail "$HAIR_LABEL: cloud-init ran" "wait_running timed out after ${BOOT_TIMEOUT_LXC}s"
            fi
        else
            fail "$HAIR_LABEL: cloud-init ran" "start rc=$hair_start_rc" "$hair_start_out"
        fi

        # Tear down the end-to-end container.
        hair_e2e_destroy_out="$(kento destroy -f "$HAIR_LXC" 2>&1)"
        if [ $? -ne 0 ]; then
            diag "$HAIR_LABEL: e2e container destroy failed: $hair_e2e_destroy_out"
            force_teardown "$HAIR_LXC" "lxc"
        fi
    else
        fail "$HAIR_LABEL: cloud-init ran" "e2e create rc=$hair_e2e_rc" "$hair_e2e_out"
        force_teardown "$HAIR_LXC" "lxc"
    fi

    subgroup_end
    fi

else
    diag "droste-hair image ($HAIR_IMAGE) not found, skipping C1"
    for t in "create" "config-mode is cloudinit" "cloud-seed files exist" \
             "meta-data hostname" "network-config written with --ip" \
             "injection override" "cloud-init ran"; do
        skip "droste-hair-ci: $t" "image not available"
    done
fi

diag ""

fi  # end section_enabled "image"

# ==========================================================================
#  SECTION D: Nested-LXC (kento-inside-pve-lxc creates inner LXC)
# ==========================================================================
#
# Validates the kanibako production topology: kento runs inside a pve-lxc
# outer container and creates plain-LXC inners from there. Section D is the
# Tier 3 coverage described in ~/playbook/plans/lxc-in-lxc-tests.md.
#
# Prerequisites on the PVE host:
#   - ghcr.io/doctorjei/droste-hair:latest available in root's podman store
#     (or pullable). If missing, SECTION D is skipped cleanly.
#   - Kento source tree at /home/droste/kento-src/ (git checkout of main),
#     used to build a wheel and push into the outer so the nested inner
#     gets the same version being tested. If missing, SECTION D is skipped.
#   - Running script has NOPASSWD sudo and `pct` / `podman` on PATH.
#
# Inner image: localhost/kento-test-minimal:latest (Tier 2 busybox-init
# fixture, built by /home/droste/kento-src/tests/fixtures/build.sh). If the
# host build fails, falls back to docker.io/library/alpine:latest.
#
# Runs --section nested; subgroups nested-lxc-lifecycle, nested-lxc-hookfire.

if section_enabled "nested"; then

diag "========================================"
diag "SECTION D: Nested-LXC (kento-in-lxc)"
diag "========================================"

D_OUTER="e2e-t3-outer"
D_OUTER_IMAGE="ghcr.io/doctorjei/droste-hair:latest"
D_INNER_IMAGE_PRIMARY="localhost/kento-test-minimal:latest"
D_INNER_IMAGE_FALLBACK="docker.io/library/alpine:latest"
D_INNER_IMAGE=""   # resolved during preamble
D_KENTO_SRC="/home/droste/kento-src"
D_SDIST=""         # path to built sdist on host
D_FIXTURE_TAR=""   # path to saved fixture tarball on host
D_TMPDIR=""        # scratch for build artifacts
D_STATE_DIR="/tmp/kento-state"
D_OUTER_READY=0    # set to 1 when outer is prepped (kento upgraded + image loaded)

# ---------- SECTION D helpers ----------

# Resolve outer VMID by scanning /var/lib/lxc/*/kento-name.
d_outer_vmid() {
    local d vmid=""
    for d in "$LXC_BASE"/*/; do
        [ -f "$d/kento-name" ] || continue
        if [ "$(tr -d '[:space:]' < "$d/kento-name")" = "$D_OUTER" ]; then
            vmid="$(basename "$d")"
            break
        fi
    done
    echo "$vmid"
}

# Capture journal + diag output from inside the outer on failure. Writes
# into the standard /tmp/e2e-diag-<TEST_NUM>.log stream used elsewhere.
d_dump_outer_journal() {
    local vmid="$1"
    local label="$2"
    local out="${DIAG_DIR}/e2e-diag-${TEST_NUM}-outer.log"
    [ -z "$vmid" ] && return 0
    {
        echo "=== SECTION D diagnostics ($label, outer vmid=$vmid) ==="
        echo "date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "--- journalctl -xb --no-pager | tail -40 ---"
        timeout 15 pct exec "$vmid" -- sh -c 'journalctl -xb --no-pager 2>/dev/null | tail -40' 2>&1 || true
        echo "--- kento list (inside outer) ---"
        timeout 15 pct exec "$vmid" --  sh -c 'KENTO_STATE_DIR='"$D_STATE_DIR"' /usr/local/bin/kento list 2>&1' || true
        echo "--- lxc-ls -f (inside outer) ---"
        timeout 15 pct exec "$vmid" -- lxc-ls -f 2>&1 || true
    } > "$out" 2>&1
    diag "see $out"
}

# Force teardown of SECTION D state. Called on trap EXIT (for the nested
# bits the global cleanup can't reach) and after a mid-run abort.
d_force_teardown() {
    local vmid
    vmid="$(d_outer_vmid)"
    diag "SECTION D: force_teardown outer=$D_OUTER vmid=$vmid"
    if [ -n "$vmid" ]; then
        # Best-effort destroy every inner that kento knows about inside outer.
        timeout 30 pct exec "$vmid" -- sh -c \
            "KENTO_STATE_DIR=$D_STATE_DIR /usr/local/bin/kento list 2>/dev/null | awk 'NR>1{print \$1}' | while read n; do KENTO_STATE_DIR=$D_STATE_DIR /usr/local/bin/kento stop \"\$n\" 2>/dev/null || true; KENTO_STATE_DIR=$D_STATE_DIR /usr/local/bin/kento destroy -f \"\$n\" 2>/dev/null || true; done" \
            >/dev/null 2>&1 || true
    fi
    force_teardown "$D_OUTER" "pve-lxc"
    # Clean up build artefacts
    if [ -n "$D_TMPDIR" ] && [ -d "$D_TMPDIR" ]; then
        rm -rf "$D_TMPDIR" || true
    fi
}

# Register a trap so aborts mid-SECTION-D clean up the outer.
d_register_trap() {
    # Chain onto the existing cleanup() trap — run d_force_teardown first
    # so any nested inners get torn down while the outer is still up.
    trap 'd_force_teardown; cleanup' EXIT
}

d_unregister_trap() {
    # Restore the default cleanup trap after SECTION D finishes.
    trap cleanup EXIT
}

# Run a command inside the outer via pct exec, with KENTO_STATE_DIR set and
# /usr/local/bin on PATH (pct exec defaults to a minimal PATH that lacks the
# kento shim). Usage: d_pct_exec <vmid> <timeout> <sh-command-string>
d_pct_exec() {
    local vmid="$1"; local tmo="$2"; shift 2
    local cmd="$*"
    timeout -k 5 "$tmo" pct exec "$vmid" -- sh -c \
        "export PATH=/usr/local/bin:/usr/local/sbin:/sbin:/bin:/usr/sbin:/usr/bin; export KENTO_STATE_DIR=$D_STATE_DIR; $cmd" \
        2>&1
}

# ---------- SECTION D preamble ----------

d_preamble_ok=1

# Guard 1: host kento is available (already checked globally, but be explicit).
if ! command -v kento >/dev/null 2>&1; then
    diag "SECTION D: kento missing on host, skipping"
    d_preamble_ok=0
fi

# Guard 2: outer image available (pull if missing, skip on failure).
if [ $d_preamble_ok -eq 1 ]; then
    if ! podman image exists "$D_OUTER_IMAGE" 2>/dev/null; then
        diag "SECTION D: pulling $D_OUTER_IMAGE..."
        if ! timeout 180 podman pull "$D_OUTER_IMAGE" >/dev/null 2>&1; then
            diag "SECTION D: podman pull failed, skipping"
            d_preamble_ok=0
        fi
    fi
fi

# Guard 3: kento source tree present on host.
if [ $d_preamble_ok -eq 1 ]; then
    if [ ! -d "$D_KENTO_SRC" ] || [ ! -f "$D_KENTO_SRC/pyproject.toml" ]; then
        diag "SECTION D: kento source tree not at $D_KENTO_SRC, skipping"
        d_preamble_ok=0
    fi
fi

# Guard 4: build kento sdist and fixture tarball in a scratch dir.
if [ $d_preamble_ok -eq 1 ]; then
    D_TMPDIR="$(mktemp -d /tmp/kento-e2e-d.XXXXXX)"
    diag "SECTION D: building artefacts in $D_TMPDIR"

    # Build sdist. We prefer sdist over wheel because pipx's python may not
    # have the right build backend for wheels; pip install of a .tar.gz is
    # universally supported.
    if ! (cd "$D_KENTO_SRC" && timeout 120 python3 -m build --sdist \
            --outdir "$D_TMPDIR" >/dev/null 2>&1); then
        # Fallback: just tar the source tree and pip-install from that.
        diag "SECTION D: python3 -m build failed, falling back to source tar"
        if ! (cd "$D_KENTO_SRC" && tar --exclude=.git --exclude=vault \
                --exclude='__pycache__' --exclude='*.egg-info' \
                -czf "$D_TMPDIR/kento-src.tar.gz" .); then
            diag "SECTION D: source tar failed, skipping"
            d_preamble_ok=0
        else
            D_SDIST="$D_TMPDIR/kento-src.tar.gz"
        fi
    else
        D_SDIST="$(ls "$D_TMPDIR"/kento-*.tar.gz 2>/dev/null | head -1)"
        if [ -z "$D_SDIST" ] || [ ! -f "$D_SDIST" ]; then
            diag "SECTION D: sdist build produced no file, skipping"
            d_preamble_ok=0
        fi
    fi
fi

# Guard 5: build or locate the Tier 2 fixture image, then save to a tarball.
if [ $d_preamble_ok -eq 1 ]; then
    D_INNER_IMAGE="$D_INNER_IMAGE_PRIMARY"
    if ! podman image exists "$D_INNER_IMAGE" 2>/dev/null; then
        if [ -x "$D_KENTO_SRC/tests/fixtures/build.sh" ]; then
            diag "SECTION D: building fixture $D_INNER_IMAGE via build.sh"
            if ! timeout 180 "$D_KENTO_SRC/tests/fixtures/build.sh" >/dev/null 2>&1; then
                diag "SECTION D: fixture build failed, falling back to alpine"
                D_INNER_IMAGE="$D_INNER_IMAGE_FALLBACK"
            fi
        else
            diag "SECTION D: fixture build.sh missing, falling back to alpine"
            D_INNER_IMAGE="$D_INNER_IMAGE_FALLBACK"
        fi
    fi

    # Ensure the chosen inner image is present locally (pull alpine if we
    # fell back).
    if ! podman image exists "$D_INNER_IMAGE" 2>/dev/null; then
        diag "SECTION D: pulling inner $D_INNER_IMAGE"
        if ! timeout 180 podman pull "$D_INNER_IMAGE" >/dev/null 2>&1; then
            diag "SECTION D: inner image unavailable, skipping"
            d_preamble_ok=0
        fi
    fi
fi

# Guard 6: save inner image to tarball for push into outer.
if [ $d_preamble_ok -eq 1 ]; then
    D_FIXTURE_TAR="$D_TMPDIR/inner-image.tar"
    if ! timeout 120 podman save -o "$D_FIXTURE_TAR" "$D_INNER_IMAGE" >/dev/null 2>&1; then
        diag "SECTION D: podman save failed, skipping"
        d_preamble_ok=0
    fi
fi

if [ $d_preamble_ok -eq 0 ]; then
    # Emit skips for every test SECTION D would otherwise run so the TAP
    # plan stays deterministic.
    for t in "outer pve-lxc create" "outer start" "outer has network" \
             "install kento-from-main into outer" \
             "push Tier 2 fixture into outer" \
             "inner create" "inner start" \
             "inner shows RUNNING via kento list inside outer" \
             "inner shows in host-side lxc-ls from outer" \
             "inner clean stop" "inner clean destroy"; do
        skip "nested-lxc-lifecycle: $t" "preamble failed"
    done
    for t in "inner create with --port 10270:22" \
             "inner start with port forwarding" \
             "kento-portfwd-active file exists after start" \
             "inner overlayfs is mounted at expected path"; do
        skip "nested-lxc-hookfire: $t" "preamble failed"
    done
fi

# ---------- D1: nested-lxc-lifecycle ----------

if [ $d_preamble_ok -eq 1 ] && subgroup_begin "nested-lxc-lifecycle"; then
    diag "--- D1: nested-lxc-lifecycle ---"
    set_last_instance "$D_OUTER" "pve-lxc"
    d_register_trap

    d1_ok=1

    # D1.1: outer pve-lxc create
    d1_out="$(timeout -k 5 90 kento lxc create --pve "$D_OUTER_IMAGE" \
        --name "$D_OUTER" --memory 2048 --cores 2 2>&1)"
    d1_rc=$?
    if [ $d1_rc -eq 0 ]; then
        pass "nested-lxc-lifecycle: outer pve-lxc create"
    else
        fail "nested-lxc-lifecycle: outer pve-lxc create" "rc=$d1_rc" "$d1_out"
        d1_ok=0
    fi

    # D1.2: outer start + wait RUNNING
    if [ $d1_ok -eq 1 ]; then
        d1_start_out="$(timeout -k 5 90 kento start "$D_OUTER" 2>&1)"
        d1_start_rc=$?
        if [ $d1_start_rc -eq 0 ] && wait_running "$D_OUTER" 30; then
            pass "nested-lxc-lifecycle: outer start"
        else
            fail "nested-lxc-lifecycle: outer start" \
                "rc=$d1_start_rc or not running" "$d1_start_out"
            d1_ok=0
        fi
    else
        skip "nested-lxc-lifecycle: outer start" "create failed"
    fi

    D_VMID=""
    if [ $d1_ok -eq 1 ]; then
        D_VMID="$(d_outer_vmid)"
        if [ -z "$D_VMID" ]; then
            fail "nested-lxc-lifecycle: outer pve-lxc vmid resolvable" "no vmid found"
            d1_ok=0
        fi
    fi

    # D1.3: outer has network (DNS resolves after fixup for dangling resolv.conf)
    if [ $d1_ok -eq 1 ]; then
        # Override /etc/resolv.conf (which points at systemd-resolved's stub,
        # but systemd-resolved isn't running in this image) with a real file
        # pointing at public resolvers.
        d_pct_exec "$D_VMID" 10 'rm -f /etc/resolv.conf; printf "nameserver 8.8.8.8\nnameserver 1.1.1.1\n" > /etc/resolv.conf' \
            >/dev/null 2>&1 || true
        # Poll getent up to 30s — DHCP may still be bringing up eth0 after
        # wait_running returns (kento reports RUNNING on cgroup start, well
        # before systemd-networkd has finished dhcp).
        d1_net_deadline=$((SECONDS + 30))
        d1_net_rc=1
        d1_net_out=""
        while [ $SECONDS -lt $d1_net_deadline ]; do
            d1_net_out="$(timeout -k 5 10 pct exec "$D_VMID" -- \
                getent hosts pypi.org 2>&1)"
            d1_net_rc=$?
            if [ $d1_net_rc -eq 0 ] && [ -n "$d1_net_out" ]; then
                break
            fi
            sleep 2
        done
        if [ $d1_net_rc -eq 0 ] && [ -n "$d1_net_out" ]; then
            pass "nested-lxc-lifecycle: outer has network"
        else
            fail "nested-lxc-lifecycle: outer has network" \
                "rc=$d1_net_rc" "$d1_net_out"
            d_dump_outer_journal "$D_VMID" "outer has network"
            d1_ok=0
        fi
    else
        skip "nested-lxc-lifecycle: outer has network" "outer not running"
    fi

    # D1.4: install kento-from-main into outer
    if [ $d1_ok -eq 1 ]; then
        d1_push_rc=0
        timeout 30 pct push "$D_VMID" "$D_SDIST" /tmp/kento-src.tar.gz \
            >/dev/null 2>&1 || d1_push_rc=$?
        if [ $d1_push_rc -eq 0 ]; then
            d1_inst_out="$(timeout -k 5 180 pct exec "$D_VMID" -- \
                /opt/pipx/venvs/kento/bin/python -m pip install --upgrade \
                    /tmp/kento-src.tar.gz 2>&1)"
            d1_inst_rc=$?
            if [ $d1_inst_rc -eq 0 ]; then
                d1_ver_out="$(timeout -k 5 10 pct exec "$D_VMID" -- \
                    /usr/local/bin/kento --version 2>&1)"
                if echo "$d1_ver_out" | grep -qE '1\.[0-9]+\.'; then
                    pass "nested-lxc-lifecycle: install kento-from-main into outer"
                    D_OUTER_READY=1
                else
                    fail "nested-lxc-lifecycle: install kento-from-main into outer" \
                        "version not 1.x: $d1_ver_out"
                    d_dump_outer_journal "$D_VMID" "kento version wrong"
                    d1_ok=0
                fi
            else
                fail "nested-lxc-lifecycle: install kento-from-main into outer" \
                    "pip install rc=$d1_inst_rc" "$d1_inst_out"
                d_dump_outer_journal "$D_VMID" "kento pip install"
                d1_ok=0
            fi
        else
            fail "nested-lxc-lifecycle: install kento-from-main into outer" \
                "pct push rc=$d1_push_rc"
            d1_ok=0
        fi
    else
        skip "nested-lxc-lifecycle: install kento-from-main into outer" "outer not ready"
    fi

    # D1.5: push Tier 2 fixture into outer
    if [ $d1_ok -eq 1 ]; then
        d1_pushf_rc=0
        timeout 60 pct push "$D_VMID" "$D_FIXTURE_TAR" /tmp/kento-test-minimal.tar \
            >/dev/null 2>&1 || d1_pushf_rc=$?
        if [ $d1_pushf_rc -eq 0 ]; then
            d1_load_out="$(timeout -k 5 90 pct exec "$D_VMID" -- \
                podman load -i /tmp/kento-test-minimal.tar 2>&1)"
            d1_load_rc=$?
            if [ $d1_load_rc -eq 0 ]; then
                pass "nested-lxc-lifecycle: push Tier 2 fixture into outer"
            else
                fail "nested-lxc-lifecycle: push Tier 2 fixture into outer" \
                    "podman load rc=$d1_load_rc" "$d1_load_out"
                d1_ok=0
            fi
        else
            fail "nested-lxc-lifecycle: push Tier 2 fixture into outer" \
                "pct push rc=$d1_pushf_rc"
            d1_ok=0
        fi
    else
        skip "nested-lxc-lifecycle: push Tier 2 fixture into outer" "outer not ready"
    fi

    # D1.6: inner create inside outer
    D_INNER1="inner1"
    if [ $d1_ok -eq 1 ]; then
        d1_create_out="$(d_pct_exec "$D_VMID" 90 \
            "mkdir -p $D_STATE_DIR && KENTO_APPARMOR_PROFILE=unconfined /usr/local/bin/kento lxc create --no-pve $D_INNER_IMAGE --name $D_INNER1")"
        d1_create_rc=$?
        if [ $d1_create_rc -eq 0 ]; then
            pass "nested-lxc-lifecycle: inner create"
        else
            fail "nested-lxc-lifecycle: inner create" \
                "rc=$d1_create_rc" "$d1_create_out"
            d_dump_outer_journal "$D_VMID" "inner create"
            d1_ok=0
        fi
    else
        skip "nested-lxc-lifecycle: inner create" "outer not ready"
    fi

    # D1.7: inner start
    if [ $d1_ok -eq 1 ]; then
        d1_istart_out="$(d_pct_exec "$D_VMID" 90 \
            "/usr/local/bin/kento start $D_INNER1")"
        d1_istart_rc=$?
        if [ $d1_istart_rc -eq 0 ]; then
            pass "nested-lxc-lifecycle: inner start"
        else
            fail "nested-lxc-lifecycle: inner start" \
                "rc=$d1_istart_rc" "$d1_istart_out"
            d_dump_outer_journal "$D_VMID" "inner start"
            d1_ok=0
        fi
    else
        skip "nested-lxc-lifecycle: inner start" "inner create failed"
    fi

    # D1.8: inner shows RUNNING via kento list inside outer (poll up to 15s)
    if [ $d1_ok -eq 1 ]; then
        d1_list_ok=0
        d1_list_deadline=$((SECONDS + 15))
        d1_list_out=""
        while [ $SECONDS -lt $d1_list_deadline ]; do
            d1_list_out="$(d_pct_exec "$D_VMID" 10 "/usr/local/bin/kento list")"
            if echo "$d1_list_out" | grep -qE "^${D_INNER1}[[:space:]].*running"; then
                d1_list_ok=1
                break
            fi
            sleep 1
        done
        if [ $d1_list_ok -eq 1 ]; then
            pass "nested-lxc-lifecycle: inner shows RUNNING via kento list inside outer"
        else
            fail "nested-lxc-lifecycle: inner shows RUNNING via kento list inside outer" \
                "never reached running" "$d1_list_out"
            d_dump_outer_journal "$D_VMID" "kento list RUNNING"
        fi
    else
        skip "nested-lxc-lifecycle: inner shows RUNNING via kento list inside outer" \
            "inner start failed"
    fi

    # D1.9: inner shows in host-side lxc-ls from outer
    if [ $d1_ok -eq 1 ]; then
        d1_lxcls_out="$(timeout -k 5 15 pct exec "$D_VMID" -- \
            lxc-ls -f 2>&1)"
        if echo "$d1_lxcls_out" | grep -qE "${D_INNER1}[[:space:]].*RUNNING"; then
            pass "nested-lxc-lifecycle: inner shows in host-side lxc-ls from outer"
        else
            fail "nested-lxc-lifecycle: inner shows in host-side lxc-ls from outer" \
                "inner not RUNNING in lxc-ls" "$d1_lxcls_out"
            d_dump_outer_journal "$D_VMID" "lxc-ls"
        fi
    else
        skip "nested-lxc-lifecycle: inner shows in host-side lxc-ls from outer" \
            "inner start failed"
    fi

    # D1.10: inner clean stop (verify STOPPED within 15s)
    if [ $d1_ok -eq 1 ]; then
        d1_stop_out="$(d_pct_exec "$D_VMID" 30 \
            "/usr/local/bin/kento stop $D_INNER1")"
        d1_stop_rc=$?
        d1_stopped_ok=0
        if [ $d1_stop_rc -eq 0 ]; then
            d1_stopped_deadline=$((SECONDS + 15))
            while [ $SECONDS -lt $d1_stopped_deadline ]; do
                d1_status_out="$(d_pct_exec "$D_VMID" 10 "/usr/local/bin/kento list")"
                if ! echo "$d1_status_out" | grep -qE "^${D_INNER1}[[:space:]].*running"; then
                    d1_stopped_ok=1
                    break
                fi
                sleep 1
            done
        fi
        if [ $d1_stopped_ok -eq 1 ]; then
            pass "nested-lxc-lifecycle: inner clean stop"
        else
            fail "nested-lxc-lifecycle: inner clean stop" \
                "stop rc=$d1_stop_rc, still running" "$d1_stop_out"
            d_dump_outer_journal "$D_VMID" "inner stop"
        fi
    else
        skip "nested-lxc-lifecycle: inner clean stop" "inner start failed"
    fi

    # D1.11: inner clean destroy
    if [ $d1_ok -eq 1 ]; then
        d1_destroy_out="$(d_pct_exec "$D_VMID" 30 \
            "/usr/local/bin/kento destroy $D_INNER1 -f")"
        d1_destroy_rc=$?
        d1_gone_out="$(d_pct_exec "$D_VMID" 10 "/usr/local/bin/kento list")"
        if [ $d1_destroy_rc -eq 0 ] && ! echo "$d1_gone_out" | grep -qE "^${D_INNER1}[[:space:]]"; then
            pass "nested-lxc-lifecycle: inner clean destroy"
        else
            fail "nested-lxc-lifecycle: inner clean destroy" \
                "destroy rc=$d1_destroy_rc still present in list" "$d1_destroy_out"
            d_dump_outer_journal "$D_VMID" "inner destroy"
        fi
    else
        skip "nested-lxc-lifecycle: inner clean destroy" "inner start failed"
    fi

    subgroup_end
fi

# ---------- D2: nested-lxc-hookfire ----------
#
# Reuses the outer from D1 if it's still prepped; otherwise creates fresh.

if [ $d_preamble_ok -eq 1 ] && subgroup_begin "nested-lxc-hookfire"; then
    diag "--- D2: nested-lxc-hookfire ---"
    set_last_instance "$D_OUTER" "pve-lxc"

    d2_ok=1
    D_INNER2="inner2"
    D_VMID="$(d_outer_vmid)"

    # If the outer isn't already up (e.g. D1 was filtered out), spin it up
    # now with the full preflight.
    if [ -z "$D_VMID" ] || [ "$D_OUTER_READY" -ne 1 ]; then
        d_register_trap
        # If a stale outer of this name exists (e.g. D1 failed partway and left
        # it running without marking D_OUTER_READY=1), clear it first so create
        # doesn't fail with "instance name already taken".
        if kento list 2>/dev/null | grep -qE "^\s*$D_OUTER\s"; then
            diag "D2 preflight: stale outer '$D_OUTER' present, destroying before create"
            timeout -k 5 60 kento destroy "$D_OUTER" -f >/dev/null 2>&1 || true
        fi
        d2_create_out="$(timeout -k 5 90 kento lxc create --pve "$D_OUTER_IMAGE" \
            --name "$D_OUTER" --memory 2048 --cores 2 2>&1)"
        if [ $? -ne 0 ]; then
            diag "D2 preflight: outer create failed: $d2_create_out"
            d2_ok=0
        fi
        if [ $d2_ok -eq 1 ]; then
            timeout -k 5 90 kento start "$D_OUTER" >/dev/null 2>&1 || d2_ok=0
            wait_running "$D_OUTER" 30 || d2_ok=0
            D_VMID="$(d_outer_vmid)"
        fi
        if [ $d2_ok -eq 1 ] && [ -n "$D_VMID" ]; then
            # Override the dangling /etc/resolv.conf symlink with a real file pointing at a working resolver.
            d_pct_exec "$D_VMID" 10 'rm -f /etc/resolv.conf; printf "nameserver 8.8.8.8\nnameserver 1.1.1.1\n" > /etc/resolv.conf' \
                >/dev/null 2>&1 || true
            timeout 30 pct push "$D_VMID" "$D_SDIST" /tmp/kento-src.tar.gz >/dev/null 2>&1 || d2_ok=0
            timeout 180 pct exec "$D_VMID" -- \
                /opt/pipx/venvs/kento/bin/python -m pip install --upgrade \
                    /tmp/kento-src.tar.gz >/dev/null 2>&1 || d2_ok=0
            timeout 60 pct push "$D_VMID" "$D_FIXTURE_TAR" /tmp/kento-test-minimal.tar >/dev/null 2>&1 || d2_ok=0
            timeout 90 pct exec "$D_VMID" -- podman load -i /tmp/kento-test-minimal.tar >/dev/null 2>&1 || d2_ok=0
            d_pct_exec "$D_VMID" 10 "mkdir -p $D_STATE_DIR" >/dev/null 2>&1 || true
        fi
    fi

    # ---------- D2 preflight: bridge + DHCP + NAT inside outer ----------
    #
    # The pve-lxc outer (droste-hair) has no bridge of its own. kento's inner
    # start-host hook installs iptables DNAT rules that target the inner's
    # bridge IP — without a bridge + DHCP server inside the outer, --port has
    # nothing to forward to and D2.1–D2.4 can't exercise the hook meaningfully.
    #
    # We create lxcbr0 on 10.0.4.0/24 (chosen to avoid clashing with the PVE
    # host's lxcbr0 at 10.0.3.0/24), bind dnsmasq to it for DHCP, enable
    # ip_forward, and add a MASQUERADE rule. Each step is guarded; on failure
    # we set d2_ok=0 and the rest of D2 skips cleanly.
    D_BRIDGE_READY=0
    if [ $d2_ok -eq 1 ]; then
        diag "D2 preflight: installing dnsmasq + bridge + NAT inside outer"
        # Step 1: install dnsmasq if missing. Cold apt cache: 30-60s.
        if ! d_pct_exec "$D_VMID" 180 '
            set -e
            if ! command -v dnsmasq >/dev/null 2>&1; then
                apt-get update -qq >/dev/null 2>&1
                DEBIAN_FRONTEND=noninteractive apt-get install -y -qq dnsmasq >/dev/null 2>&1
            fi
        ' >/dev/null 2>&1; then
            diag "D2 preflight: dnsmasq install failed"
            d2_ok=0
            D2_PREFLIGHT_FAIL="dnsmasq-install"
        fi
    fi
    if [ $d2_ok -eq 1 ]; then
        # Step 2: create lxcbr0 on 10.0.4.0/24 if absent (idempotent).
        if ! d_pct_exec "$D_VMID" 30 '
            set -e
            if ! ip link show lxcbr0 >/dev/null 2>&1; then
                ip link add lxcbr0 type bridge
            fi
            ip link set lxcbr0 up
            if ! ip addr show lxcbr0 | grep -q "inet 10.0.4.1/24"; then
                ip addr add 10.0.4.1/24 dev lxcbr0 2>/dev/null || true
            fi
        ' >/dev/null 2>&1; then
            diag "D2 preflight: bridge create failed"
            d2_ok=0
            D2_PREFLIGHT_FAIL="bridge-create"
        fi
    fi
    if [ $d2_ok -eq 1 ]; then
        # Step 3: ip_forward + MASQUERADE (idempotent).
        if ! d_pct_exec "$D_VMID" 30 '
            set -e
            sysctl -w net.ipv4.ip_forward=1 >/dev/null
            iptables -t nat -C POSTROUTING -s 10.0.4.0/24 -j MASQUERADE 2>/dev/null \
                || iptables -t nat -A POSTROUTING -s 10.0.4.0/24 -j MASQUERADE
        ' >/dev/null 2>&1; then
            diag "D2 preflight: ip_forward/masquerade failed"
            d2_ok=0
            D2_PREFLIGHT_FAIL="nat-setup"
        fi
    fi
    if [ $d2_ok -eq 1 ]; then
        # Step 4: configure and start dnsmasq for the bridge.
        if ! d_pct_exec "$D_VMID" 30 '
            set -e
            mkdir -p /etc/dnsmasq.d
            cat > /etc/dnsmasq.d/lxc-e2e.conf <<EOF
interface=lxcbr0
bind-interfaces
dhcp-range=10.0.4.10,10.0.4.250,12h
EOF
            if command -v systemctl >/dev/null 2>&1; then
                systemctl restart dnsmasq >/dev/null 2>&1 || service dnsmasq restart >/dev/null 2>&1
            else
                service dnsmasq restart >/dev/null 2>&1
            fi
        ' >/dev/null 2>&1; then
            diag "D2 preflight: dnsmasq config/start failed"
            d2_ok=0
            D2_PREFLIGHT_FAIL="dnsmasq-start"
        fi
    fi
    if [ $d2_ok -eq 1 ]; then
        D_BRIDGE_READY=1
        diag "D2 preflight: bridge ready (lxcbr0 10.0.4.1/24, dnsmasq up)"
    fi

    # D2.1: inner create with --port 10270:22 + static --ip.
    #
    # Why --ip when we've just built a DHCP-capable bridge?
    # LXC inside a pve-lxc outer frequently fails to actually enslave the
    # newly-created veth to the bridge (lxc logs "Attached" but `bridge
    # link show` reports empty; likely an apparmor/nesting quirk). That
    # breaks DHCP inside the inner. Using --ip drives the start-host
    # hook's static-IP fast path, which writes kento-portfwd-active
    # directly without waiting on DHCP discovery — and still exercises
    # the exact hook code path D14 is designed to verify. The inner's
    # veth still lives on lxcbr0 so the nft DNAT rule points somewhere
    # addressable.
    if [ $d2_ok -eq 1 ]; then
        d2_create_out="$(d_pct_exec "$D_VMID" 90 \
            "KENTO_APPARMOR_PROFILE=unconfined /usr/local/bin/kento lxc create --no-pve $D_INNER_IMAGE --name $D_INNER2 --port 10270:22 --ip 10.0.4.50/24 --gateway 10.0.4.1")"
        d2_create_rc=$?
        if [ $d2_create_rc -eq 0 ]; then
            pass "nested-lxc-hookfire: inner create with --port 10270:22"
        else
            fail "nested-lxc-hookfire: inner create with --port 10270:22" \
                "rc=$d2_create_rc" "$d2_create_out"
            d_dump_outer_journal "$D_VMID" "inner2 create"
            d2_ok=0
        fi
    else
        if [ -n "${D2_PREFLIGHT_FAIL:-}" ]; then
            skip "nested-lxc-hookfire: inner create with --port 10270:22" \
                "preflight failed ($D2_PREFLIGHT_FAIL)"
        else
            skip "nested-lxc-hookfire: inner create with --port 10270:22" "outer not ready"
        fi
    fi

    # D2.2: inner start with port forwarding
    if [ $d2_ok -eq 1 ]; then
        d2_start_out="$(d_pct_exec "$D_VMID" 90 \
            "/usr/local/bin/kento start $D_INNER2")"
        d2_start_rc=$?
        if [ $d2_start_rc -eq 0 ]; then
            pass "nested-lxc-hookfire: inner start with port forwarding"
        else
            fail "nested-lxc-hookfire: inner start with port forwarding" \
                "rc=$d2_start_rc" "$d2_start_out"
            d_dump_outer_journal "$D_VMID" "inner2 start"
            d2_ok=0
        fi
    else
        skip "nested-lxc-hookfire: inner start with port forwarding" "inner create failed"
    fi

    # D2.3: kento-portfwd-active exists inside outer (poll up to 10s — the
    # start-host hook fires after kento start returns).
    if [ $d2_ok -eq 1 ]; then
        d2_portfwd_ok=0
        d2_portfwd_deadline=$((SECONDS + 10))
        while [ $SECONDS -lt $d2_portfwd_deadline ]; do
            if timeout 5 pct exec "$D_VMID" -- \
                    test -f "/var/lib/lxc/$D_INNER2/kento-portfwd-active" \
                    >/dev/null 2>&1; then
                d2_portfwd_ok=1
                break
            fi
            sleep 1
        done
        if [ $d2_portfwd_ok -eq 1 ]; then
            pass "nested-lxc-hookfire: kento-portfwd-active file exists after start"
        else
            fail "nested-lxc-hookfire: kento-portfwd-active file exists after start" \
                "hook did not fire within 10s"
            d_dump_outer_journal "$D_VMID" "portfwd-active"
        fi
    else
        skip "nested-lxc-hookfire: kento-portfwd-active file exists after start" \
            "inner start failed"
    fi

    # D2.4: overlayfs mounted at expected path (inside outer)
    if [ $d2_ok -eq 1 ]; then
        if timeout 5 pct exec "$D_VMID" -- \
                mountpoint -q "/var/lib/lxc/$D_INNER2/rootfs" >/dev/null 2>&1; then
            pass "nested-lxc-hookfire: inner overlayfs is mounted at expected path"
        else
            fail "nested-lxc-hookfire: inner overlayfs is mounted at expected path" \
                "rootfs not a mountpoint"
            d_dump_outer_journal "$D_VMID" "overlayfs mount"
        fi
    else
        skip "nested-lxc-hookfire: inner overlayfs is mounted at expected path" \
            "inner start failed"
    fi

    # Best-effort inner teardown (ignore errors — force_teardown covers it).
    if [ -n "$D_VMID" ]; then
        d_pct_exec "$D_VMID" 30 "/usr/local/bin/kento stop $D_INNER2 2>/dev/null; /usr/local/bin/kento destroy -f $D_INNER2 2>/dev/null" \
            >/dev/null 2>&1 || true
        # Best-effort bridge teardown. The outer is about to be destroyed, so
        # this is hygiene only (helps if the outer is reused across runs).
        if [ "$D_BRIDGE_READY" = "1" ]; then
            d_pct_exec "$D_VMID" 20 '
                iptables -t nat -D POSTROUTING -s 10.0.4.0/24 -j MASQUERADE 2>/dev/null || true
                rm -f /etc/dnsmasq.d/lxc-e2e.conf
                systemctl stop dnsmasq 2>/dev/null || service dnsmasq stop 2>/dev/null || true
                ip link set lxcbr0 down 2>/dev/null || true
                ip link delete lxcbr0 2>/dev/null || true
            ' >/dev/null 2>&1 || true
        fi
    fi

    subgroup_end
fi

# ---------- SECTION D teardown ----------

# Always tear down the outer, even if only one of D1/D2 ran.
if [ $d_preamble_ok -eq 1 ]; then
    d_force_teardown
    d_unregister_trap
fi

diag ""

fi  # end section_enabled "nested"

# TAP plan (at end, since test count varies with skips)
echo "1..$TEST_NUM"

if [ "$FAIL_COUNT" -gt 0 ]; then
    diag "$FAIL_COUNT of $TEST_NUM tests failed"
    exit 1
else
    diag "All $TEST_NUM tests passed"
    exit 0
fi
