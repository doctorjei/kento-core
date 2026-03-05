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

    with patch("kento.reset.LXC_BASE", tmp_path):
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

    with patch("kento.reset.LXC_BASE", tmp_path):
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

    with patch("kento.reset.LXC_BASE", tmp_path):
        with pytest.raises(SystemExit):
            reset("test")


@patch("kento.reset.require_root")
def test_reset_nonexistent(mock_root, tmp_path):
    with patch("kento.reset.LXC_BASE", tmp_path):
        with pytest.raises(SystemExit):
            reset("nonexistent")
