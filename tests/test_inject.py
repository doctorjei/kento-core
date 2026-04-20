"""Tests for the standalone guest-config injection script.

``inject.sh`` is a non-templated POSIX shell script shipped verbatim into each
container as ``kento-inject.sh``. It is invoked by the LXC hook (and in
subsequent refactor steps by the VM / PVE-VM code paths) with two positional
args: ROOTFS and CONTAINER_DIR.
"""

from pathlib import Path

from kento.inject import generate_inject, write_inject


# ---------------------------------------------------------------------------
# Content: POSIX shape + argument contract
# ---------------------------------------------------------------------------

def test_inject_is_posix_sh():
    script = generate_inject()
    assert script.startswith("#!/bin/sh\n")


def test_inject_has_set_eu():
    script = generate_inject()
    assert "set -eu" in script


def test_inject_reads_positional_args():
    """First arg is ROOTFS, second is CONTAINER_DIR."""
    script = generate_inject()
    assert 'ROOTFS="$1"' in script
    assert 'CONTAINER_DIR="$2"' in script


def test_inject_has_no_template_placeholders():
    """inject.sh is shipped verbatim — no @@NAME@@ or similar."""
    script = generate_inject()
    assert "@@" not in script


def test_inject_no_bashisms():
    """Script uses only POSIX shell constructs."""
    script = generate_inject()
    assert "[[" not in script
    assert "==" not in script
    assert "<(" not in script


# ---------------------------------------------------------------------------
# Content: config sources
# ---------------------------------------------------------------------------

def test_inject_reads_lxc_config_for_ip():
    script = generate_inject()
    assert "ipv4" in script
    assert "10-static.network" in script


def test_inject_reads_pve_config_for_ip():
    script = generate_inject()
    assert "/etc/pve/lxc/" in script
    assert "net0:" in script
    assert 'ip=//p' in script


def test_inject_falls_back_to_kento_net():
    script = generate_inject()
    assert "kento-net" in script
    assert "kento-mode" in script


def test_inject_reads_pve_nameserver():
    script = generate_inject()
    assert "nameserver:" in script
    assert "searchdomain:" in script


# ---------------------------------------------------------------------------
# Content: hostname / IP / DNS / timezone / env / ssh
# ---------------------------------------------------------------------------

def test_inject_injects_hostname():
    script = generate_inject()
    assert "/etc/hostname" in script
    assert "CFG_HOSTNAME" in script


def test_inject_lxc_match_name_eth0():
    """LXC/PVE modes match eth0 (veth interface name is stable)."""
    script = generate_inject()
    assert "Name=eth0" in script


def test_inject_vm_match_type_ether():
    """VM modes match Type=ether (predictable interface naming varies)."""
    script = generate_inject()
    assert "Type=ether" in script


def test_inject_resolved_dropin_for_dns_without_ip():
    script = generate_inject()
    assert "resolved.conf.d" in script
    assert "90-kento.conf" in script


def test_inject_injects_timezone():
    script = generate_inject()
    assert "/etc/localtime" in script
    assert "/etc/timezone" in script
    assert "kento-tz" in script
    assert "zoneinfo" in script


def test_inject_injects_env():
    script = generate_inject()
    assert "/etc/environment" in script
    assert "kento-env" in script
    assert "lxc\\.environment" in script


def test_inject_env_dedup():
    """Environment dedup uses awk; config takes priority over kento-env."""
    script = generate_inject()
    assert "!seen[$1]++" in script


def test_inject_has_mount_point_creation():
    script = generate_inject()
    assert "mount point directories for mp" in script
    assert "grep '^mp[0-9]*:'" in script
    assert 'mkdir -p "$ROOTFS$MP_PATH"' in script


def test_mount_point_parsing_patterns():
    """Verify the grep/sed patterns for extracting mp= from PVE config lines."""
    script = generate_inject()
    assert "grep '^mp[0-9]*:'" in script
    assert "tr ',' '\\n'" in script
    assert "sed -n 's/^mp=//p'" in script
    assert "MP_PATH" in script


def test_inject_injects_authorized_keys():
    script = generate_inject()
    assert "kento-authorized-keys" in script
    assert "/root/.ssh" in script
    assert "authorized_keys" in script
    assert "chmod 700" in script
    assert "chmod 600" in script


def test_authorized_keys_injection_uses_posix_commands():
    script = generate_inject()
    # Locate the SSH injection block by its comment marker.
    idx = script.index("SSH authorized_keys injection")
    block = script[idx:]
    assert "mkdir -p" in block
    assert "cp " in block
    assert "chmod" in block


def test_authorized_keys_injection_after_env():
    """SSH key injection should come after env injection."""
    script = generate_inject()
    env_pos = script.index("/etc/environment")
    ssh_pos = script.index("SSH authorized_keys injection")
    assert env_pos < ssh_pos


# ---------------------------------------------------------------------------
# write_inject: per-container copy
# ---------------------------------------------------------------------------

def test_write_inject_creates_file(tmp_path):
    out = write_inject(tmp_path)
    assert out == tmp_path / "kento-inject.sh"
    assert out.exists()


def test_write_inject_chmod_755(tmp_path):
    out = write_inject(tmp_path)
    assert out.stat().st_mode & 0o755 == 0o755


def test_write_inject_content_matches_generate(tmp_path):
    out = write_inject(tmp_path)
    assert out.read_text() == generate_inject()
