"""Tests for container reset."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from kento.reset import reset


def _mock_run_stopped(args, **kwargs):
    result = subprocess.CompletedProcess(args, 0)
    if "lxc-info" in args:
        result.stdout = "STOPPED"
    elif "mountpoint" in args:
        result.returncode = 1
    return result


def _mock_run_running(args, **kwargs):
    result = subprocess.CompletedProcess(args, 0)
    if "lxc-info" in args:
        result.stdout = "RUNNING"
    return result


@patch("kento.reset.subprocess.run", side_effect=_mock_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_clears_upper_and_work(mock_root, mock_layers, mock_run,
                                      tmp_path):
    lxc_dir = tmp_path / "test"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    upper = lxc_dir / "upper"
    upper.mkdir()
    (upper / "somefile").write_text("data")
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir):
        reset("test")

    assert upper.is_dir()
    assert not (upper / "somefile").exists()
    assert (lxc_dir / "kento-layers").read_text().strip() == "/new/upper:/new/lower"
    assert (lxc_dir / "kento-hook").exists()


@patch("kento.reset.subprocess.run", side_effect=_mock_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_with_separate_state_dir(mock_root, mock_layers, mock_run,
                                        tmp_path):
    lxc_dir = tmp_path / "test"
    lxc_dir.mkdir()
    state = tmp_path / "user-state" / "test"
    state.mkdir(parents=True)
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-state").write_text(str(state) + "\n")
    (lxc_dir / "rootfs").mkdir()
    upper = state / "upper"
    upper.mkdir()
    (upper / "somefile").write_text("data")
    (state / "work").mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir):
        reset("test")

    assert upper.is_dir()
    assert not (upper / "somefile").exists()
    hook = (lxc_dir / "kento-hook").read_text()
    assert str(state) in hook


@patch("kento.reset.subprocess.run", side_effect=_mock_run_running)
@patch("kento.reset.require_root")
def test_reset_refuses_running(mock_root, mock_run, tmp_path):
    lxc_dir = tmp_path / "test"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")

    with patch("kento.reset.resolve_container", return_value=lxc_dir):
        with pytest.raises(SystemExit):
            reset("test")


@patch("kento.reset.require_root")
def test_reset_nonexistent(mock_root, tmp_path):
    with patch("kento.reset.resolve_container",
               side_effect=SystemExit(1)):
        with pytest.raises(SystemExit):
            reset("nonexistent")


# --- PVE mode tests ---


def _mock_pve_run_stopped(args, **kwargs):
    result = subprocess.CompletedProcess(args, 0)
    if "pct" in args and "status" in args:
        result.stdout = "status: stopped"
    elif "mountpoint" in args:
        result.returncode = 1
    return result


def _mock_pve_run_running(args, **kwargs):
    result = subprocess.CompletedProcess(args, 0)
    if "pct" in args and "status" in args:
        result.stdout = "status: running"
    return result


@patch("kento.reset.subprocess.run", side_effect=_mock_pve_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_pve_clears_upper_and_work(mock_root, mock_layers, mock_run,
                                          tmp_path):
    lxc_dir = tmp_path / "100"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-mode").write_text("pve\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    upper = lxc_dir / "upper"
    upper.mkdir()
    (upper / "somefile").write_text("data")
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir):
        reset("mybox")

    assert upper.is_dir()
    assert not (upper / "somefile").exists()
    assert (lxc_dir / "kento-layers").read_text().strip() == "/new/upper:/new/lower"


@patch("kento.reset.subprocess.run", side_effect=_mock_pve_run_running)
@patch("kento.reset.require_root")
def test_reset_pve_refuses_running(mock_root, mock_run, tmp_path):
    lxc_dir = tmp_path / "100"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-mode").write_text("pve\n")

    with patch("kento.reset.resolve_container", return_value=lxc_dir):
        with pytest.raises(SystemExit):
            reset("mybox")


# --- VM mode tests ---


def _mock_vm_run_stopped(args, **kwargs):
    result = subprocess.CompletedProcess(args, 0)
    if "mountpoint" in args:
        result.returncode = 1
    return result


@patch("kento.reset.subprocess.run", side_effect=_mock_vm_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_vm_clears_upper_and_work(mock_root, mock_layers, mock_run,
                                         tmp_path):
    lxc_dir = tmp_path / "testvm"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-mode").write_text("vm\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    upper = lxc_dir / "upper"
    upper.mkdir()
    (upper / "somefile").write_text("data")
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir), \
         patch("kento.vm.is_vm_running", return_value=False):
        reset("testvm")

    assert upper.is_dir()
    assert not (upper / "somefile").exists()
    assert (lxc_dir / "kento-layers").read_text().strip() == "/new/upper:/new/lower"


@patch("kento.reset.subprocess.run", side_effect=_mock_vm_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_vm_no_hook_regenerated(mock_root, mock_layers, mock_run,
                                       tmp_path):
    lxc_dir = tmp_path / "testvm"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-mode").write_text("vm\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir), \
         patch("kento.vm.is_vm_running", return_value=False):
        reset("testvm")

    assert not (lxc_dir / "kento-hook").exists()


@patch("kento.reset.subprocess.run", side_effect=_mock_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_pve_vm_regenerates_vm_hook(mock_root, mock_layers, mock_run,
                                           tmp_path):
    """pve-vm scrub must regenerate the VM hookscript, not the LXC hook.

    Before this fix scrub called `write_hook()` for any mode != "vm",
    which overwrote the VM hookscript with the LXC shell hook. `qm start`
    then failed in pre-start with `3: parameter not set` because the LXC
    hook expects a 3rd arg (hook-type) but qm only passes VMID and PHASE.
    """
    lxc_dir = tmp_path / "testpvevm"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-mode").write_text("pve-vm\n")
    (lxc_dir / "kento-vmid").write_text("100\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir), \
         patch("kento.reset.is_running", return_value=False):
        reset("testpvevm")

    hook = lxc_dir / "kento-hook"
    assert hook.exists()
    content = hook.read_text()
    # VM hook shape: uses $1/$2 positional args, has a "pre-start" case
    assert 'VMID="$1"' in content
    assert 'PHASE="$2"' in content
    # LXC hook shape uses $3 for hook type — must NOT be present
    assert 'LXC_HOOK_TYPE' not in content


@patch("kento.reset.subprocess.run", side_effect=_mock_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_reinjects_static_ip(mock_root, mock_layers, mock_run,
                                    tmp_path):
    lxc_dir = tmp_path / "test"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "kento-net").write_text("ip=192.168.0.160/22\ngateway=192.168.0.1\ndns=8.8.8.8\n")
    upper = lxc_dir / "upper"
    upper.mkdir()
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir):
        reset("test")

    unit = (lxc_dir / "upper" / "etc" / "systemd" / "network" /
            "10-static.network").read_text()
    assert "Address=192.168.0.160/22" in unit
    assert "Gateway=192.168.0.1" in unit
    assert "DNS=8.8.8.8" in unit


@patch("kento.reset.subprocess.run", side_effect=_mock_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_reinjects_hostname(mock_root, mock_layers, mock_run, tmp_path):
    lxc_dir = tmp_path / "test"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "kento-name").write_text("myhost\n")
    (lxc_dir / "upper").mkdir()
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir):
        reset("myhost")

    hostname = (lxc_dir / "upper" / "etc" / "hostname").read_text()
    assert hostname.strip() == "myhost"


@patch("kento.reset.subprocess.run", side_effect=_mock_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_reinjects_timezone(mock_root, mock_layers, mock_run, tmp_path):
    lxc_dir = tmp_path / "test"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "kento-tz").write_text("Asia/Tokyo\n")
    (lxc_dir / "upper").mkdir()
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir):
        reset("test")

    assert (lxc_dir / "upper" / "etc" / "timezone").read_text().strip() == "Asia/Tokyo"
    localtime = lxc_dir / "upper" / "etc" / "localtime"
    assert localtime.is_symlink()


@patch("kento.reset.subprocess.run", side_effect=_mock_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_reinjects_env(mock_root, mock_layers, mock_run, tmp_path):
    lxc_dir = tmp_path / "test"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "kento-env").write_text("FOO=bar\nBAZ=qux\n")
    (lxc_dir / "upper").mkdir()
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir):
        reset("test")

    env = (lxc_dir / "upper" / "etc" / "environment").read_text()
    assert "FOO=bar" in env
    assert "BAZ=qux" in env


@patch("kento.reset.subprocess.run", side_effect=_mock_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_no_net_file_skips_injection(mock_root, mock_layers, mock_run,
                                            tmp_path):
    lxc_dir = tmp_path / "test"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir):
        reset("test")

    assert not (lxc_dir / "upper" / "etc").exists()


@patch("kento.reset.require_root")
def test_reset_vm_refuses_running(mock_root, tmp_path):
    lxc_dir = tmp_path / "testvm"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-mode").write_text("vm\n")

    with patch("kento.reset.resolve_container", return_value=lxc_dir), \
         patch("kento.vm.is_vm_running", return_value=True):
        with pytest.raises(SystemExit):
            reset("testvm")


# --- Port forwarding state cleanup (Phase 3) ---


@patch("kento.reset.subprocess.run", side_effect=_mock_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_removes_portfwd_active(mock_root, mock_layers, mock_run,
                                       tmp_path):
    """scrub removes stale kento-portfwd-active file."""
    lxc_dir = tmp_path / "test"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "kento-portfwd-active").write_text("10022:22:10.0.0.5\n")
    (lxc_dir / "upper").mkdir()
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir):
        reset("test")

    assert not (lxc_dir / "kento-portfwd-active").exists()
