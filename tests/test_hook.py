"""Tests for hook script generation."""

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


def test_generate_hook_reads_lxc_config_for_ip():
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    assert "ipv4" in script
    assert "10-static.network" in script


def test_generate_hook_reads_pve_config_for_ip():
    script = generate_hook(Path("/var/lib/lxc/200"), "/a:/b", "test")
    assert "/etc/pve/lxc/" in script
    assert "net0:" in script
    assert 'ip=//p' in script


def test_generate_hook_falls_back_to_kento_net():
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    assert "kento-net" in script
    assert "kento-mode" in script


def test_generate_hook_injects_hostname():
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    assert "/etc/hostname" in script
    assert "CFG_HOSTNAME" in script


def test_generate_hook_reads_pve_nameserver():
    script = generate_hook(Path("/var/lib/lxc/200"), "/a:/b", "test")
    assert "nameserver:" in script
    assert "searchdomain:" in script


def test_generate_hook_injects_timezone():
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    assert "/etc/localtime" in script
    assert "/etc/timezone" in script
    assert "kento-tz" in script
    assert "zoneinfo" in script


def test_generate_hook_injects_env():
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    assert "/etc/environment" in script
    assert "kento-env" in script
    assert "lxc\\.environment" in script


def test_generate_hook_resolved_dropin_for_dns_without_ip():
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    assert "resolved.conf.d" in script
    assert "90-kento.conf" in script


def test_generate_hook_has_mount_point_creation():
    script = generate_hook(Path("/var/lib/lxc/200"), "/a:/b", "test")
    assert "mount point directories for mp" in script
    assert "grep '^mp[0-9]*:'" in script
    assert 'mkdir -p "$ROOTFS$MP_PATH"' in script


def test_mount_point_parsing_patterns():
    """Verify the grep/sed patterns for extracting mp= from PVE config lines."""
    script = generate_hook(Path("/var/lib/lxc/200"), "/a:/b", "test")
    # grep selects mp0:, mp1:, etc. lines
    assert "grep '^mp[0-9]*:'" in script
    # tr splits on comma, sed extracts mp= value
    assert "tr ',' '\\n'" in script
    assert "sed -n 's/^mp=//p'" in script
    # MP_PATH used to create directory under ROOTFS
    assert 'MP_PATH' in script


def test_generate_hook_injects_authorized_keys():
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    assert "kento-authorized-keys" in script
    assert "/root/.ssh" in script
    assert "authorized_keys" in script
    assert "chmod 700" in script
    assert "chmod 600" in script


def test_authorized_keys_injection_is_posix_sh():
    """Injection block uses only POSIX shell constructs (no bashisms)."""
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    # Locate the SSH injection block
    idx = script.index("SSH authorized_keys injection")
    end = script.index(";;", idx)
    block = script[idx:end]
    # POSIX: `[` not `[[`, no `==`, no process substitution
    assert "[[" not in block
    assert "==" not in block
    assert "<(" not in block
    # Uses POSIX-compatible commands
    assert "mkdir -p" in block
    assert "cp " in block
    assert "chmod" in block


def test_authorized_keys_injection_after_env():
    """SSH key injection should come after env injection, before ;;."""
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    env_pos = script.index("/etc/environment")
    ssh_pos = script.index("SSH authorized_keys injection")
    assert env_pos < ssh_pos


def test_write_hook(tmp_path):
    hook = write_hook(tmp_path, "/a:/b", "mycontainer")
    assert hook == tmp_path / "kento-hook"
    assert hook.exists()
    assert hook.stat().st_mode & 0o755 == 0o755
    content = hook.read_text()
    assert 'NAME="mycontainer"' in content
    assert 'LAYERS="/a:/b"' in content
