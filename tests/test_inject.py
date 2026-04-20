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


# ---------------------------------------------------------------------------
# TZ env var auto-injection (Feature 1 of Step 1d)
# ---------------------------------------------------------------------------

def test_inject_tz_env_line_appended():
    """When CFG_TZ is set, TZ=<zone> is appended to CFG_ENV before dedup."""
    script = generate_inject()
    assert 'TZ=$CFG_TZ' in script


def test_inject_tz_after_kento_env():
    """TZ must be appended AFTER kento-env — auto-TZ is lowest priority,
    so user --env TZ=... (which appears before kento-env) wins the dedup."""
    script = generate_inject()
    # Locate both landmarks; TZ append must come after kento-env read.
    kento_env_pos = script.index("kento-env")
    tz_append_pos = script.index("TZ=$CFG_TZ")
    assert kento_env_pos < tz_append_pos


class TestInjectScriptTZExecution:
    """Execute inject.sh against a fake rootfs to verify TZ injection behavior.

    These are shell-level integration tests — they exercise the real script
    content, not Python.
    """

    def _setup(self, tmp_path):
        """Create rootfs + container_dir skeleton; return (rootfs, container_dir)."""
        rootfs = tmp_path / "rootfs"
        rootfs.mkdir()
        (rootfs / "etc").mkdir()
        container = tmp_path / "container"
        container.mkdir()
        (container / "kento-mode").write_text("lxc")
        return rootfs, container

    def _run(self, rootfs, container):
        import subprocess
        from kento.inject import write_inject
        script = write_inject(container)
        subprocess.run(
            ["sh", str(script), str(rootfs), str(container)],
            check=True,
        )

    def test_tz_lands_in_etc_environment_alone(self, tmp_path):
        """CFG_TZ set and no other env → /etc/environment contains TZ line."""
        rootfs, container = self._setup(tmp_path)
        (container / "kento-tz").write_text("Europe/Berlin\n")
        self._run(rootfs, container)
        content = (rootfs / "etc" / "environment").read_text()
        assert "TZ=Europe/Berlin" in content

    def test_tz_alongside_other_env(self, tmp_path):
        """CFG_TZ set plus kento-env → both land in /etc/environment."""
        rootfs, container = self._setup(tmp_path)
        (container / "kento-tz").write_text("Asia/Tokyo\n")
        (container / "kento-env").write_text("FOO=bar\nBAZ=qux\n")
        self._run(rootfs, container)
        content = (rootfs / "etc" / "environment").read_text()
        assert "FOO=bar" in content
        assert "BAZ=qux" in content
        assert "TZ=Asia/Tokyo" in content

    def test_user_env_tz_wins_over_auto(self, tmp_path):
        """User --env TZ=... (in kento-env) wins over auto-TZ from kento-tz."""
        rootfs, container = self._setup(tmp_path)
        (container / "kento-tz").write_text("Europe/Berlin\n")
        (container / "kento-env").write_text("TZ=America/New_York\n")
        self._run(rootfs, container)
        content = (rootfs / "etc" / "environment").read_text()
        assert "TZ=America/New_York" in content
        assert "TZ=Europe/Berlin" not in content

    def test_no_tz_no_tz_line(self, tmp_path):
        """No CFG_TZ → no TZ= line at all."""
        rootfs, container = self._setup(tmp_path)
        (container / "kento-env").write_text("FOO=bar\n")
        self._run(rootfs, container)
        content = (rootfs / "etc" / "environment").read_text()
        assert "FOO=bar" in content
        assert "TZ=" not in content

    def test_no_env_at_all_no_file(self, tmp_path):
        """No CFG_TZ and no env at all → /etc/environment is not written."""
        rootfs, container = self._setup(tmp_path)
        self._run(rootfs, container)
        assert not (rootfs / "etc" / "environment").exists()

    def test_tz_only_produces_valid_file(self, tmp_path):
        """Only CFG_TZ (no kento-env, no config env) still produces valid file."""
        rootfs, container = self._setup(tmp_path)
        (container / "kento-tz").write_text("UTC\n")
        self._run(rootfs, container)
        env_file = rootfs / "etc" / "environment"
        assert env_file.is_file()
        # Single line TZ=UTC (with trailing newline from awk)
        content = env_file.read_text()
        lines = [l for l in content.splitlines() if l]
        assert lines == ["TZ=UTC"]


class TestInjectSSHUserExecution:
    """Execute inject.sh to verify SSH user-based key injection."""

    def _setup(self, tmp_path):
        rootfs = tmp_path / "rootfs"
        rootfs.mkdir()
        (rootfs / "etc").mkdir()
        container = tmp_path / "container"
        container.mkdir()
        (container / "kento-mode").write_text("lxc")
        return rootfs, container

    def _run(self, rootfs, container):
        import subprocess
        script = write_inject(container)
        subprocess.run(
            ["sh", str(script), str(rootfs), str(container)],
            check=True,
        )

    def test_default_root_user(self, tmp_path):
        """No kento-ssh-user file means keys go to /root/.ssh/."""
        rootfs, container = self._setup(tmp_path)
        (container / "kento-authorized-keys").write_text(
            "ssh-rsa AAAA test@host\n")
        self._run(rootfs, container)
        ak = rootfs / "root" / ".ssh" / "authorized_keys"
        assert ak.is_file()
        assert "ssh-rsa AAAA" in ak.read_text()

    def test_nonroot_user_resolves_home(self, tmp_path):
        """kento-ssh-user=droste resolves home from /etc/passwd and writes there."""
        rootfs, container = self._setup(tmp_path)
        (rootfs / "etc" / "passwd").write_text(
            "root:x:0:0:root:/root:/bin/bash\n"
            "droste:x:1000:1000:Droste:/home/droste:/bin/bash\n"
        )
        (container / "kento-authorized-keys").write_text(
            "ssh-ed25519 BBBB droste@host\n")
        (container / "kento-ssh-user").write_text("droste\n")
        self._run(rootfs, container)
        ak = rootfs / "home" / "droste" / ".ssh" / "authorized_keys"
        assert ak.is_file()
        assert "ssh-ed25519 BBBB" in ak.read_text()
        # Check ownership (numeric UID:GID from passwd)
        import os
        st = os.stat(ak)
        assert st.st_uid == 1000
        assert st.st_gid == 1000
        # Also check .ssh dir ownership
        ssh_dir = rootfs / "home" / "droste" / ".ssh"
        st_dir = os.stat(ssh_dir)
        assert st_dir.st_uid == 1000
        assert st_dir.st_gid == 1000

    def test_missing_user_skips_with_warning(self, tmp_path):
        """User not in /etc/passwd skips injection with warning, no crash."""
        rootfs, container = self._setup(tmp_path)
        (rootfs / "etc" / "passwd").write_text(
            "root:x:0:0:root:/root:/bin/bash\n"
        )
        (container / "kento-authorized-keys").write_text(
            "ssh-rsa AAAA test@host\n")
        (container / "kento-ssh-user").write_text("nobody_here\n")
        import subprocess
        script = write_inject(container)
        result = subprocess.run(
            ["sh", str(script), str(rootfs), str(container)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "not found" in result.stderr or "Warning" in result.stderr
        # No authorized_keys written anywhere
        assert not (rootfs / "root" / ".ssh" / "authorized_keys").exists()

    def test_nonroot_creates_home_dir(self, tmp_path):
        """If home dir doesn't exist in rootfs, mkdir -p creates it."""
        import os
        uid = os.getuid()
        gid = os.getgid()
        rootfs, container = self._setup(tmp_path)
        (rootfs / "etc" / "passwd").write_text(
            f"appuser:x:{uid}:{gid}:App:/opt/appuser:/bin/sh\n"
        )
        (container / "kento-authorized-keys").write_text(
            "ssh-rsa AAAA app@host\n")
        (container / "kento-ssh-user").write_text("appuser\n")
        # /opt/appuser does NOT exist yet
        self._run(rootfs, container)
        ak = rootfs / "opt" / "appuser" / ".ssh" / "authorized_keys"
        assert ak.is_file()
        assert "ssh-rsa AAAA" in ak.read_text()

    def test_explicit_root_same_as_default(self, tmp_path):
        """kento-ssh-user=root behaves same as no file (keys in /root/.ssh/)."""
        rootfs, container = self._setup(tmp_path)
        (container / "kento-authorized-keys").write_text(
            "ssh-rsa AAAA test@host\n")
        (container / "kento-ssh-user").write_text("root\n")
        self._run(rootfs, container)
        ak = rootfs / "root" / ".ssh" / "authorized_keys"
        assert ak.is_file()
        assert "ssh-rsa AAAA" in ak.read_text()


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
    assert "SSH_HOME" in script
    assert ".ssh" in script
    assert "authorized_keys" in script
    assert "chmod 700" in script
    assert "chmod 600" in script


def test_inject_reads_ssh_user():
    """inject.sh reads kento-ssh-user file for SSH key target user."""
    script = generate_inject()
    assert "kento-ssh-user" in script
    assert "SSH_USER" in script


def test_inject_resolves_home_from_passwd():
    """inject.sh greps /etc/passwd to resolve non-root home dir."""
    script = generate_inject()
    assert "/etc/passwd" in script
    assert "cut -d: -f6" in script  # home dir field
    assert "cut -d: -f3" in script  # UID field
    assert "cut -d: -f4" in script  # GID field


def test_inject_chowns_ssh_for_nonroot():
    """inject.sh chowns .ssh dir and authorized_keys for non-root users."""
    script = generate_inject()
    assert "chown" in script


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


