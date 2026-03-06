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
