"""Tests for container stop."""

from unittest.mock import patch

import pytest

from kento.stop import stop


@patch("kento.stop.subprocess.run")
@patch("kento.stop.require_root")
def test_stop_lxc(mock_root, mock_run, tmp_path):
    d = tmp_path / "mybox"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    (d / "kento-mode").write_text("lxc\n")

    with patch("kento.stop.resolve_container", return_value=d):
        stop("mybox")

    mock_run.assert_called_once_with(
        ["lxc-stop", "-n", "mybox"], check=True,
    )


@patch("kento.stop.subprocess.run")
@patch("kento.stop.require_root")
def test_stop_pve(mock_root, mock_run, tmp_path):
    d = tmp_path / "100"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    (d / "kento-mode").write_text("pve\n")

    with patch("kento.stop.resolve_container", return_value=d):
        stop("mybox")

    mock_run.assert_called_once_with(
        ["pct", "stop", "100"], check=True,
    )


@patch("kento.stop.subprocess.run")
@patch("kento.stop.require_root")
def test_stop_defaults_to_lxc(mock_root, mock_run, tmp_path):
    d = tmp_path / "mybox"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")

    with patch("kento.stop.resolve_container", return_value=d):
        stop("mybox")

    mock_run.assert_called_once_with(
        ["lxc-stop", "-n", "mybox"], check=True,
    )


@patch("kento.stop.require_root")
def test_stop_vm(mock_root, tmp_path):
    d = tmp_path / "testvm"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    (d / "kento-mode").write_text("vm\n")

    with patch("kento.stop.resolve_container", return_value=d), \
         patch("kento.vm.stop_vm") as mock_stop_vm:
        stop("testvm")

    mock_stop_vm.assert_called_once_with(d)
