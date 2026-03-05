"""Tests for container reset."""

import subprocess
from unittest.mock import patch

import pytest

from kento.reset import reset


@patch("kento.reset.subprocess.run")
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_clears_upper_and_work(mock_root, mock_layers, mock_run,
                                      tmp_path):
    lxc_dir = tmp_path / "test"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    upper = lxc_dir / "upper"
    upper.mkdir()
    (upper / "somefile").write_text("data")
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()

    def side_effect(args, **kwargs):
        result = subprocess.CompletedProcess(args, 0)
        if "lxc-info" in args:
            result.stdout = "STOPPED"
        elif "mountpoint" in args:
            result.returncode = 1
        return result

    mock_run.side_effect = side_effect

    with patch("kento.reset.LXC_BASE", tmp_path):
        reset("test")

    assert upper.is_dir()
    assert not (upper / "somefile").exists()
    assert (lxc_dir / "kento-layers").read_text().strip() == "/new/upper:/new/lower"
    assert (lxc_dir / "kento-hook").exists()


@patch("kento.reset.subprocess.run")
@patch("kento.reset.require_root")
def test_reset_refuses_running(mock_root, mock_run, tmp_path):
    lxc_dir = tmp_path / "test"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")

    def side_effect(args, **kwargs):
        result = subprocess.CompletedProcess(args, 0)
        if "lxc-info" in args:
            result.stdout = "RUNNING"
        return result

    mock_run.side_effect = side_effect

    with patch("kento.reset.LXC_BASE", tmp_path):
        with pytest.raises(SystemExit):
            reset("test")


@patch("kento.reset.require_root")
def test_reset_nonexistent(mock_root, tmp_path):
    with patch("kento.reset.LXC_BASE", tmp_path):
        with pytest.raises(SystemExit):
            reset("nonexistent")
