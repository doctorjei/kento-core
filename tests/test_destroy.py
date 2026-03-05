"""Tests for container destruction."""

import subprocess
from pathlib import Path
from unittest.mock import patch, call

import pytest

from kento.destroy import destroy


@patch("kento.destroy.subprocess.run")
@patch("kento.destroy.require_root")
def test_destroy_removes_directory(mock_root, mock_run, tmp_path):
    lxc_dir = tmp_path / "test"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "rootfs").mkdir()

    def side_effect(args, **kwargs):
        result = subprocess.CompletedProcess(args, 0)
        if "lxc-info" in args:
            result.stdout = "STOPPED"
        elif "mountpoint" in args:
            result.returncode = 1
        return result

    mock_run.side_effect = side_effect

    with patch("kento.destroy.LXC_BASE", tmp_path):
        destroy("test")

    assert not lxc_dir.exists()


@patch("kento.destroy.subprocess.run")
@patch("kento.destroy.require_root")
def test_destroy_stops_running_container(mock_root, mock_run, tmp_path):
    lxc_dir = tmp_path / "test"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "rootfs").mkdir()

    def side_effect(args, **kwargs):
        result = subprocess.CompletedProcess(args, 0)
        if "lxc-info" in args:
            result.stdout = "RUNNING"
        elif "mountpoint" in args:
            result.returncode = 1
        return result

    mock_run.side_effect = side_effect

    with patch("kento.destroy.LXC_BASE", tmp_path):
        destroy("test")

    stop_calls = [c for c in mock_run.call_args_list if "lxc-stop" in c[0][0]]
    assert len(stop_calls) == 1


@patch("kento.destroy.require_root")
def test_destroy_nonexistent(mock_root, tmp_path):
    with patch("kento.destroy.LXC_BASE", tmp_path):
        with pytest.raises(SystemExit):
            destroy("nonexistent")


@patch("kento.destroy.require_root")
def test_destroy_non_kento_container(mock_root, tmp_path):
    lxc_dir = tmp_path / "test"
    lxc_dir.mkdir()
    with patch("kento.destroy.LXC_BASE", tmp_path):
        with pytest.raises(SystemExit):
            destroy("test")
