"""Tests for kento __init__ shared utilities."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from kento import (
    require_root, upper_base, LXC_BASE, VM_BASE,
    sanitize_image_name, next_instance_name, resolve_container,
    resolve_in_namespace, resolve_any, check_name_conflict,
    detect_bridge, resolve_network, is_running,
)


def test_require_root_fails_non_root():
    with patch("kento.os.getuid", return_value=1000):
        with pytest.raises(SystemExit):
            require_root()


def test_require_root_passes_as_root():
    with patch("kento.os.getuid", return_value=0):
        require_root()  # should not raise


def test_upper_base_root_direct():
    with patch.dict(os.environ, {}, clear=True):
        result = upper_base("test")
    assert result == LXC_BASE / "test"


def test_upper_base_sudo_user():
    with patch.dict(os.environ, {"SUDO_USER": "alice"}), \
         patch("kento.pwd.getpwnam") as mock_pwd:
        mock_pwd.return_value.pw_dir = "/home/alice"
        result = upper_base("test")
    assert result == Path("/home/alice/.local/share/kento/test")


# --- sanitize_image_name ---


def test_sanitize_simple():
    assert sanitize_image_name("debian:12") == "debian_12"


def test_sanitize_full_reference():
    assert sanitize_image_name("docker.io/library/debian:12") == "docker.io-library-debian_12"


def test_sanitize_with_dashes():
    assert sanitize_image_name("my-registry/my-image:v1") == "my--registry-my--image_v1"


def test_sanitize_with_underscores():
    assert sanitize_image_name("my_image:latest") == "my__image_latest"


def test_sanitize_complex():
    """Test all four replacement rules together."""
    assert sanitize_image_name("my-reg/lib_img:v1.0-beta") == "my--reg-lib__img_v1.0--beta"


def test_sanitize_is_bijective():
    """Verify the transformation is reversible."""
    original = "docker.io/jrei/systemd-debian:12"
    sanitized = sanitize_image_name(original)
    # Reverse: '_' → ':', '__' → '_', '-' → '/', '--' → '-'
    r = sanitized.replace("--", "\x00")
    r = r.replace("-", "/")
    r = r.replace("\x00", "-")
    r = r.replace("__", "\x00")
    r = r.replace("_", ":")
    r = r.replace("\x00", "_")
    assert r == original


# --- next_instance_name ---


def test_next_instance_name_empty(tmp_path):
    assert next_instance_name("debian_12", tmp_path) == "debian_12-0"


def test_next_instance_name_increments(tmp_path):
    (tmp_path / "debian_12-0").mkdir()
    assert next_instance_name("debian_12", tmp_path) == "debian_12-1"


def test_next_instance_name_fills_gaps(tmp_path):
    (tmp_path / "debian_12-0").mkdir()
    (tmp_path / "debian_12-2").mkdir()
    # -1 is free
    assert next_instance_name("debian_12", tmp_path) == "debian_12-1"


def test_next_instance_name_checks_kento_name(tmp_path):
    """Names from kento-name files also block candidates."""
    vmid_dir = tmp_path / "100"
    vmid_dir.mkdir()
    (vmid_dir / "kento-name").write_text("debian_12-0\n")
    assert next_instance_name("debian_12", tmp_path) == "debian_12-1"


def test_next_instance_name_nonexistent_dir(tmp_path):
    assert next_instance_name("foo", tmp_path / "nope") == "foo-0"


# --- resolve_container ---


def test_resolve_container_direct(tmp_path):
    d = tmp_path / "mybox"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    result = resolve_container("mybox", tmp_path)
    assert result == d


def test_resolve_container_by_kento_name(tmp_path):
    d = tmp_path / "100"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    (d / "kento-name").write_text("webbox\n")
    result = resolve_container("webbox", tmp_path)
    assert result == d


def test_resolve_container_not_found(tmp_path):
    with pytest.raises(SystemExit):
        resolve_container("nonexistent", tmp_path)


def test_resolve_container_prefers_direct_match(tmp_path):
    """Direct directory match takes priority over kento-name scan."""
    # Direct match
    d1 = tmp_path / "mybox"
    d1.mkdir()
    (d1 / "kento-image").write_text("debian:12\n")
    # Another container whose kento-name is also "mybox"
    d2 = tmp_path / "100"
    d2.mkdir()
    (d2 / "kento-image").write_text("debian:12\n")
    (d2 / "kento-name").write_text("mybox\n")
    result = resolve_container("mybox", tmp_path)
    assert result == d1


def test_resolve_container_searches_vm_base(tmp_path):
    """When scan_dir is None, searches both LXC_BASE and VM_BASE."""
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    d = vm / "testvm"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    (d / "kento-name").write_text("testvm\n")

    with patch("kento.LXC_BASE", lxc), \
         patch("kento.VM_BASE", vm):
        result = resolve_container("testvm")
    assert result == d


def test_resolve_container_lxc_before_vm(tmp_path):
    """LXC_BASE is searched before VM_BASE."""
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    d_lxc = lxc / "mybox"
    d_lxc.mkdir()
    (d_lxc / "kento-image").write_text("debian:12\n")
    d_vm = vm / "mybox"
    d_vm.mkdir()
    (d_vm / "kento-image").write_text("debian:12\n")

    with patch("kento.LXC_BASE", lxc), \
         patch("kento.VM_BASE", vm):
        result = resolve_container("mybox")
    assert result == d_lxc


def test_upper_base_with_custom_base(tmp_path):
    """upper_base accepts an optional base directory."""
    with patch.dict(os.environ, {}, clear=True):
        result = upper_base("test", tmp_path)
    assert result == tmp_path / "test"


# --- resolve_in_namespace ---


def test_resolve_in_namespace_found(tmp_path):
    """resolve_in_namespace returns path when found in the target namespace."""
    lxc = tmp_path / "lxc"
    lxc.mkdir()
    d = lxc / "webbox"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")

    with patch("kento.LXC_BASE", lxc):
        result = resolve_in_namespace("webbox", "container")
    assert result == d


def test_resolve_in_namespace_found_by_kento_name(tmp_path):
    """resolve_in_namespace finds containers via kento-name files."""
    vm = tmp_path / "vm"
    vm.mkdir()
    d = vm / "myvm-dir"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    (d / "kento-name").write_text("myvm\n")

    with patch("kento.VM_BASE", vm):
        result = resolve_in_namespace("myvm", "vm")
    assert result == d


def test_resolve_in_namespace_not_found(tmp_path):
    """resolve_in_namespace exits with error when not found."""
    lxc = tmp_path / "lxc"
    lxc.mkdir()

    with patch("kento.LXC_BASE", lxc):
        with pytest.raises(SystemExit):
            resolve_in_namespace("nope", "container")


def test_resolve_in_namespace_ignores_other(tmp_path):
    """resolve_in_namespace only searches the target namespace."""
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    # Container exists in VM namespace only
    d = vm / "mybox"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")

    with patch("kento.LXC_BASE", lxc), patch("kento.VM_BASE", vm):
        with pytest.raises(SystemExit):
            resolve_in_namespace("mybox", "container")


# --- resolve_any ---


def test_resolve_any_found_in_lxc(tmp_path):
    """resolve_any returns (path, mode) when found only in LXC_BASE."""
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    d = lxc / "webbox"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    (d / "kento-mode").write_text("pve\n")

    with patch("kento.LXC_BASE", lxc), patch("kento.VM_BASE", vm):
        path, mode = resolve_any("webbox")
    assert path == d
    assert mode == "pve"


def test_resolve_any_found_in_vm(tmp_path):
    """resolve_any returns (path, mode) when found only in VM_BASE."""
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    d = vm / "myvm"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    (d / "kento-mode").write_text("vm\n")

    with patch("kento.LXC_BASE", lxc), patch("kento.VM_BASE", vm):
        path, mode = resolve_any("myvm")
    assert path == d
    assert mode == "vm"


def test_resolve_any_default_mode_lxc(tmp_path):
    """resolve_any defaults to 'lxc' mode when no kento-mode file exists."""
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    d = lxc / "webbox"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")

    with patch("kento.LXC_BASE", lxc), patch("kento.VM_BASE", vm):
        path, mode = resolve_any("webbox")
    assert path == d
    assert mode == "lxc"


def test_resolve_any_default_mode_vm(tmp_path):
    """resolve_any defaults to 'vm' mode when no kento-mode file in VM_BASE."""
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    d = vm / "myvm"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")

    with patch("kento.LXC_BASE", lxc), patch("kento.VM_BASE", vm):
        path, mode = resolve_any("myvm")
    assert path == d
    assert mode == "vm"


def test_resolve_any_ambiguous(tmp_path):
    """resolve_any exits with error when name exists in both namespaces."""
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    d_lxc = lxc / "mybox"
    d_lxc.mkdir()
    (d_lxc / "kento-image").write_text("debian:12\n")
    d_vm = vm / "mybox"
    d_vm.mkdir()
    (d_vm / "kento-image").write_text("debian:12\n")

    with patch("kento.LXC_BASE", lxc), patch("kento.VM_BASE", vm):
        with pytest.raises(SystemExit):
            resolve_any("mybox")


class TestResolveAnyPveVm:
    def test_finds_pve_vm(self, tmp_path):
        """resolve_any returns 'pve-vm' mode for containers in VM_BASE with that mode."""
        lxc = tmp_path / "lxc"
        vm = tmp_path / "vm"
        lxc.mkdir()
        vm.mkdir()
        vm_dir = vm / "test"
        vm_dir.mkdir()
        (vm_dir / "kento-image").write_text("myimage\n")
        (vm_dir / "kento-mode").write_text("pve-vm\n")
        (vm_dir / "kento-name").write_text("test\n")

        with patch("kento.LXC_BASE", lxc), \
             patch("kento.VM_BASE", vm):
            container_dir, mode = resolve_any("test")

        assert container_dir == vm_dir
        assert mode == "pve-vm"

    def test_pve_vm_found_via_vm_namespace(self, tmp_path):
        """resolve_in_namespace('vm') finds pve-vm containers in VM_BASE."""
        vm = tmp_path / "vm"
        vm.mkdir()
        vm_dir = vm / "test"
        vm_dir.mkdir()
        (vm_dir / "kento-image").write_text("myimage\n")
        (vm_dir / "kento-mode").write_text("pve-vm\n")
        (vm_dir / "kento-name").write_text("test\n")

        with patch("kento.VM_BASE", vm):
            result = resolve_in_namespace("test", "vm")

        assert result == vm_dir


def test_resolve_any_not_found(tmp_path):
    """resolve_any exits with error when name is not found anywhere."""
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()

    with patch("kento.LXC_BASE", lxc), patch("kento.VM_BASE", vm):
        with pytest.raises(SystemExit):
            resolve_any("ghost")


# --- check_name_conflict ---


def test_check_name_conflict_exists(tmp_path):
    """check_name_conflict returns True when name exists in the other namespace."""
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    d = vm / "mybox"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")

    with patch("kento.LXC_BASE", lxc), patch("kento.VM_BASE", vm):
        # Creating in container namespace, but name exists in vm namespace
        assert check_name_conflict("mybox", "container") is True


def test_check_name_conflict_none(tmp_path):
    """check_name_conflict returns False when no conflict."""
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()

    with patch("kento.LXC_BASE", lxc), patch("kento.VM_BASE", vm):
        assert check_name_conflict("newbox", "container") is False


def test_check_name_conflict_same_namespace_ignored(tmp_path):
    """check_name_conflict ignores names in the target (same) namespace."""
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    # Name exists in container namespace
    d = lxc / "mybox"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")

    with patch("kento.LXC_BASE", lxc), patch("kento.VM_BASE", vm):
        # Creating in container namespace — same-namespace existence is not a conflict
        assert check_name_conflict("mybox", "container") is False


# --- detect_bridge ---


class TestDetectBridge:
    @patch("kento._bridge_exists", side_effect=lambda n: n == "vmbr0")
    def test_finds_vmbr0(self, mock_exists):
        assert detect_bridge() == "vmbr0"

    @patch("kento._bridge_exists", side_effect=lambda n: n == "lxcbr0")
    def test_finds_lxcbr0(self, mock_exists):
        assert detect_bridge() == "lxcbr0"

    @patch("kento._bridge_exists", return_value=False)
    def test_no_bridge(self, mock_exists):
        assert detect_bridge() is None

    @patch("kento._bridge_exists", side_effect=lambda n: True)
    def test_prefers_vmbr0(self, mock_exists):
        assert detect_bridge() == "vmbr0"


# --- resolve_network ---


class TestResolveNetwork:
    @patch("kento.detect_bridge", return_value="vmbr0")
    def test_auto_detect_bridge(self, mock_detect):
        result = resolve_network(None, None, "lxc")
        assert result == {"type": "bridge", "bridge": "vmbr0", "port": None}

    @patch("kento.detect_bridge", return_value=None)
    def test_auto_detect_vm_usermode(self, mock_detect):
        result = resolve_network(None, None, "vm")
        assert result == {"type": "usermode", "bridge": None, "port": None}

    @patch("kento.detect_bridge", return_value="lxcbr0")
    def test_auto_detect_vm_prefers_usermode_over_bridge(self, mock_detect):
        """Plain VM mode with no --network must default to usermode even when
        a bridge exists on the host. start_vm has no bridge/tap support, so
        auto-detecting bridge here would produce a VM with no network."""
        result = resolve_network(None, None, "vm")
        assert result == {"type": "usermode", "bridge": None, "port": None}

    @patch("kento.detect_bridge", return_value="vmbr0")
    def test_auto_detect_pve_vm_bridge(self, mock_detect):
        """PVE-VM auto-detects bridge (qm generates proper bridge network)."""
        result = resolve_network(None, None, "pve-vm")
        assert result == {"type": "bridge", "bridge": "vmbr0", "port": None}

    @patch("kento.detect_bridge", return_value=None)
    def test_auto_detect_lxc_none(self, mock_detect):
        result = resolve_network(None, None, "lxc")
        assert result == {"type": "none", "bridge": None, "port": None}

    def test_explicit_bridge_with_name(self):
        result = resolve_network("bridge", "vmbr1", "lxc")
        assert result == {"type": "bridge", "bridge": "vmbr1", "port": None}

    @patch("kento.detect_bridge", return_value="lxcbr0")
    def test_bridge_auto_detect_name(self, mock_detect):
        result = resolve_network("bridge", None, "lxc")
        assert result == {"type": "bridge", "bridge": "lxcbr0", "port": None}

    @patch("kento.detect_bridge", return_value=None)
    def test_bridge_no_interface_errors(self, mock_detect):
        with pytest.raises(SystemExit):
            resolve_network("bridge", None, "lxc")

    def test_port_implies_usermode_for_vm(self):
        """Port implies usermode for VM mode."""
        result = resolve_network(None, None, "vm", port="10022:22")
        assert result == {"type": "usermode", "bridge": None, "port": "10022:22"}

    @patch("kento.detect_bridge", return_value="lxcbr0")
    def test_port_does_not_imply_usermode_for_lxc(self, mock_detect):
        """Port does NOT imply usermode for LXC — auto-detects bridge instead."""
        result = resolve_network(None, None, "lxc", port="10022:22")
        assert result["type"] == "bridge"
        assert result["port"] == "10022:22"

    @patch("kento.detect_bridge", return_value=None)
    def test_port_lxc_no_bridge_falls_to_none(self, mock_detect):
        """Port for LXC with no bridge found falls to 'none' (validation elsewhere)."""
        result = resolve_network(None, None, "lxc", port="10022:22")
        assert result["type"] == "none"
        assert result["port"] == "10022:22"

    def test_port_implies_usermode_for_pve_vm(self):
        """Port implies usermode for PVE-VM mode."""
        result = resolve_network(None, None, "pve-vm", port="10022:22")
        assert result == {"type": "usermode", "bridge": None, "port": "10022:22"}

    def test_explicit_none(self):
        result = resolve_network("none", None, "lxc")
        assert result == {"type": "none", "bridge": None, "port": None}

    def test_explicit_host(self):
        result = resolve_network("host", None, "lxc")
        assert result == {"type": "host", "bridge": None, "port": None}

    def test_explicit_usermode(self):
        result = resolve_network("usermode", None, "vm")
        assert result == {"type": "usermode", "bridge": None, "port": None}


# --- is_running ---


class TestIsRunningPveVm:
    @patch("subprocess.run")
    def test_running(self, mock_run, tmp_path):
        d = tmp_path / "test"
        d.mkdir()
        (d / "kento-vmid").write_text("100\n")
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "status: running"
        assert is_running(d, "pve-vm") is True
        mock_run.assert_called_once_with(
            ["qm", "status", "100"],
            capture_output=True, text=True,
        )

    @patch("subprocess.run")
    def test_stopped(self, mock_run, tmp_path):
        d = tmp_path / "test"
        d.mkdir()
        (d / "kento-vmid").write_text("100\n")
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "status: stopped"
        assert is_running(d, "pve-vm") is False

    def test_no_vmid_file(self, tmp_path):
        d = tmp_path / "test"
        d.mkdir()
        assert is_running(d, "pve-vm") is False

    @patch("subprocess.run")
    def test_qm_failure(self, mock_run, tmp_path):
        d = tmp_path / "test"
        d.mkdir()
        (d / "kento-vmid").write_text("100\n")
        mock_run.return_value.returncode = 1
        mock_run.return_value.stdout = ""
        assert is_running(d, "pve-vm") is False
