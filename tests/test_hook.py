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


def test_generate_hook_has_fstab_sanitization():
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    assert "Fstab sanitization" in script
    assert "#kento#" in script
    assert "$ROOTFS/etc/fstab" in script
    assert "PARTUUID=" in script
    assert "UUID=" in script
    assert r"/dev/" in script or "/dev/" in script


def test_fstab_sanitization_sed_patterns():
    """Verify the sed expressions in the hook match the expected lines."""
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")

    # Should skip comment lines (branch past substitution)
    assert r"/^[[:space:]]*#/b" in script

    # Should skip blank lines
    assert r"/^[[:space:]]*$/b" in script

    # Should comment out PARTUUID lines with #kento# prefix
    assert r"PARTUUID=" in script
    assert r"#kento# " in script

    # Should comment out UUID= lines
    assert r"/^[[:space:]]*UUID=/s/^/#kento# /" in script

    # Should comment out /dev/ lines
    assert r"/^[[:space:]]*\/dev\//s/^/#kento# /" in script


def test_fstab_sanitization_sed_logic(tmp_path):
    """Run the actual sed expressions against a sample fstab to verify behavior."""
    import subprocess

    fstab_content = """\
# /etc/fstab: static file system information
PARTUUID=abcd-1234 / ext4 defaults 0 1
UUID=1234-5678 /boot vfat defaults 0 2
/dev/sda1 /mnt/data ext4 defaults 0 0
tmpfs /tmp tmpfs defaults,nosuid 0 0
proc /proc proc defaults 0 0
sysfs /sys sysfs defaults 0 0
devpts /dev/pts devpts defaults 0 0
none /run/shm tmpfs defaults 0 0

# This is a comment about /dev/sda2
  UUID=leading-space /data ext4 defaults 0 0
  /dev/vda2 /extra xfs defaults 0 0
"""
    fstab = tmp_path / "fstab"
    fstab.write_text(fstab_content)

    # Run the same sed command from hook.sh
    subprocess.run([
        "sed", "-i",
        "-e", r"/^[[:space:]]*#/b",
        "-e", r"/^[[:space:]]*$/b",
        "-e", r"/^[[:space:]]*PARTUUID=/s/^/#kento# /",
        "-e", r"/^[[:space:]]*UUID=/s/^/#kento# /",
        "-e", r"/^[[:space:]]*\/dev\//s/^/#kento# /",
        str(fstab),
    ], check=True)

    result = fstab.read_text()
    lines = result.splitlines()

    # Comment lines preserved as-is
    assert lines[0] == "# /etc/fstab: static file system information"

    # Block device lines commented out with #kento# prefix
    assert lines[1] == "#kento# PARTUUID=abcd-1234 / ext4 defaults 0 1"
    assert lines[2] == "#kento# UUID=1234-5678 /boot vfat defaults 0 2"
    assert lines[3] == "#kento# /dev/sda1 /mnt/data ext4 defaults 0 0"

    # Virtual filesystem lines preserved
    assert lines[4] == "tmpfs /tmp tmpfs defaults,nosuid 0 0"
    assert lines[5] == "proc /proc proc defaults 0 0"
    assert lines[6] == "sysfs /sys sysfs defaults 0 0"
    assert lines[7] == "devpts /dev/pts devpts defaults 0 0"
    assert lines[8] == "none /run/shm tmpfs defaults 0 0"

    # Blank line preserved
    assert lines[9] == ""

    # Comment about /dev/ preserved (it's a comment line)
    assert lines[10] == "# This is a comment about /dev/sda2"

    # Indented lines also matched
    assert lines[11] == "#kento#   UUID=leading-space /data ext4 defaults 0 0"
    assert lines[12] == "#kento#   /dev/vda2 /extra xfs defaults 0 0"


def test_fstab_sanitization_placement():
    """Fstab sanitization must come after overlayfs mount and before guest config."""
    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "test")
    mount_pos = script.index("mount -t overlay")
    fstab_pos = script.index("Fstab sanitization")
    guest_pos = script.index("Guest config injection")
    assert mount_pos < fstab_pos < guest_pos


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


def test_write_hook(tmp_path):
    hook = write_hook(tmp_path, "/a:/b", "mycontainer")
    assert hook == tmp_path / "kento-hook"
    assert hook.exists()
    assert hook.stat().st_mode & 0o755 == 0o755
    content = hook.read_text()
    assert 'NAME="mycontainer"' in content
    assert 'LAYERS="/a:/b"' in content
