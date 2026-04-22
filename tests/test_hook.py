"""Tests for hook script generation.

The hook template is a thin LXC wrapper: it validates layer paths, mounts the
overlayfs, and delegates guest-side config injection to ``kento-inject.sh``
(see ``tests/test_inject.py``). Tests here only assert template structure.
"""

from pathlib import Path

from kento.hook import generate_hook, write_hook


def test_generate_hook_contains_paths():
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b:/c", "test")
    assert 'CONTAINER_DIR="/var/lib/lxc/test"' in script
    assert 'LAYERS="/a:/b:/c"' in script
    assert 'NAME="test"' in script


def test_generate_hook_default_state_dir():
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    assert 'STATE_DIR="/var/lib/lxc/test"' in script


def test_generate_hook_custom_state_dir():
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test",
                           state_dir=Path("/home/alice/.local/share/kento/test"))
    assert 'STATE_DIR="/home/alice/.local/share/kento/test"' in script
    assert "$STATE_DIR/upper" in script
    assert "$STATE_DIR/work" in script


def test_generate_hook_has_mount_workaround():
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    assert "LIBMOUNT_FORCE_MOUNT2=always" in script
    assert "mount -t overlay" in script


def test_generate_hook_validates_layers():
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    assert "layer path missing" in script
    assert "kento scrub $NAME" in script


def test_generate_hook_has_pre_start_pre_mount_and_post_stop():
    script = generate_hook(Path("/var/lib/lxc/test"), "/a", "test")
    assert "pre-start|pre-mount)" in script
    assert "post-stop)" in script


def test_generate_hook_is_posix_sh():
    script = generate_hook(Path("/var/lib/lxc/test"), "/a", "test")
    assert script.startswith("#!/bin/sh\n")


def test_generate_hook_uses_lxc_rootfs_path():
    script = generate_hook(Path("/var/lib/lxc/100"), "/a:/b", "test")
    assert "LXC_ROOTFS_PATH" in script
    assert 'ROOTFS="${LXC_ROOTFS_PATH:-$CONTAINER_DIR/rootfs}"' in script


def test_generate_hook_delegates_to_inject_script():
    """After mount, hook invokes the standalone injection script."""
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    assert "kento-inject.sh" in script
    # Called with ROOTFS and CONTAINER_DIR as positional args.
    assert '"$ROOTFS"' in script
    assert '"$CONTAINER_DIR"' in script


def test_generate_hook_inject_call_after_mount():
    """Injection must run after the overlayfs mount, not before."""
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    mount_pos = script.index("mount -t overlay")
    inject_pos = script.index("kento-inject.sh")
    assert mount_pos < inject_pos


def test_write_hook(tmp_path):
    hook = write_hook(tmp_path, "/a:/b", "mycontainer")
    assert hook == tmp_path / "kento-hook"
    assert hook.exists()
    assert hook.stat().st_mode & 0o755 == 0o755
    content = hook.read_text()
    assert 'NAME="mycontainer"' in content
    assert 'LAYERS="/a:/b"' in content


# --- Port forwarding (Phase 3) ---


def test_generate_hook_has_start_host_case():
    """Hook template includes start-host case for port forwarding."""
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    assert "start-host)" in script


def test_generate_hook_has_portfwd_active_cleanup():
    """Hook template cleans up kento-portfwd-active in post-stop."""
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    assert "kento-portfwd-active" in script


def test_generate_hook_start_host_reads_kento_port():
    """start-host case reads kento-port file."""
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    assert "kento-port" in script


def test_generate_hook_start_host_uses_nftables():
    """start-host case uses nftables for DNAT."""
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    assert "nft" in script
    assert "dnat to" in script


def test_generate_hook_start_host_has_ip_discovery():
    """start-host case tries kento-net then lxc-info for IP discovery."""
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    assert "kento-net" in script
    assert "lxc-info" in script


def test_generate_hook_start_host_enables_route_localnet():
    """start-host enables route_localnet so localhost:<port> DNAT works."""
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    assert "route_localnet" in script


def test_generate_hook_propagates_memory_to_ns_cgroup():
    """PVE-LXC nests the container cgroup via `dir.container.inner = ns`. The
    start-host hook must write memory.max to the inner ns/ cgroup so the guest
    sees its own limit (cgroup v2 enforces the outer ceiling regardless, but
    memory-aware apps like JVMs read this file to size themselves)."""
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    assert "kento-memory" in script
    assert "/sys/fs/cgroup/lxc/" in script
    assert "/ns" in script
    assert "memory.max" in script


def test_generate_hook_propagates_cores_to_ns_cgroup():
    """Same as memory — cpu.max must be written to ns/ so the guest sees it."""
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    assert "kento-cores" in script
    assert "cpu.max" in script


def test_generate_hook_ns_cgroup_guarded_by_existence_check():
    """The ns/ cgroup is a PVE-specific convention. Guard the writes with a
    directory check so plain LXC (no ns nesting) and future PVE layout changes
    don't trip the hook."""
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    assert '[ -d "$NS_CGROUP" ]' in script


def test_generate_hook_ns_cgroup_writes_are_best_effort():
    """If the cgroup write fails (permissions, controller not enabled,
    concurrent teardown), don't abort the hook — the outer ceiling still
    enforces the limit. Emit a warning instead."""
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    assert '2>/dev/null' in script
    assert 'kento: warning' in script


def test_generate_hook_start_host_dhcp_uses_background_worker():
    """DHCP discovery must fork a detached worker to avoid a deadlock where
    lxc-info (inside the hook) blocks on the monitor that is running the
    hook. The hook should write a worker script and launch it via setsid."""
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    # Worker script path
    assert "kento-portfwd-worker.sh" in script
    # Detachment primitive — setsid in a backgrounded subshell
    assert "setsid" in script
    # Worker itself uses lxc-info (not the synchronous hook path)
    assert "lxc-info -n" in script
    # Port forwarding logic should be factored into a reusable shell function
    # so it can be invoked from multiple hook points (start-host on plain LXC,
    # pre-mount on PVE-LXC where start-host is stripped by pct).
    assert "setup_port_forwarding" in script


def test_generate_hook_portfwd_worker_is_ipv4_only():
    """nft rules live in the ip family, so the worker must filter lxc-info
    output to IPv4 addresses only. An IPv6 address would produce an invalid
    nftables rule and silently break port forwarding."""
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    # Dotted-quad filter appears with escaped dots in the generated sh
    # heredoc (`\\.` inside the shell string). Just check the grep invocation.
    assert "grep -E" in script
    assert "[0-9]+" in script
    # Make sure the worker performs filtering, not just `head -1` of raw
    # lxc-info output which would surface IPv6 first on many hosts.
    worker_section = script.split("WORKER_EOF")[1] if "WORKER_EOF" in script else ""
    # (split splits around heredoc body; the body itself is between
    # the first and second WORKER_EOF)
    body = script.split("WORKER_EOF")[1] if script.count("WORKER_EOF") >= 2 else ""
    # The grep must appear before `head -1` in the worker body.
    full_body = script
    grep_idx = full_body.find("grep -E")
    head_idx = full_body.find("head -1", grep_idx if grep_idx > 0 else 0)
    assert grep_idx > 0
    assert head_idx > grep_idx


def test_generate_hook_start_host_no_sync_lxc_info():
    """Hook must never call lxc-info on the synchronous path (which would
    deadlock with the monitor). The actual `lxc-info -n $CID` invocation
    should only appear inside the detached worker heredoc body — regardless of
    which hook branch invokes setup_port_forwarding."""
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    # Grab only the port-forwarding helper (where the worker lives).
    fn_start = script.index("setup_port_forwarding()")
    fn_body = script[fn_start:script.index("\n}\n", fn_start) + 2]
    assert "WORKER_EOF" in fn_body
    invocation_idx = fn_body.index('lxc-info -n "\\$CID"')
    assert fn_body.index("WORKER_EOF") < invocation_idx


def test_generate_hook_portfwd_idempotency_guard():
    """setup_port_forwarding must short-circuit via kento-portfwd-active so
    repeated invocations across hook points don't double-install rules."""
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    assert "kento-portfwd-active" in script
    fn_start = script.index("setup_port_forwarding()")
    fn_body = script[fn_start:script.index("\n}\n", fn_start) + 2]
    assert 'kento-portfwd-active" ] && return 0' in fn_body
