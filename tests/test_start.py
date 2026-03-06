"""Tests for container start."""

from unittest.mock import patch

import pytest

from kento.start import start


@patch("kento.start.subprocess.run")
@patch("kento.start.require_root")
def test_start_lxc(mock_root, mock_run, tmp_path):
    d = tmp_path / "mybox"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    (d / "kento-mode").write_text("lxc\n")

    with patch("kento.start.resolve_container", return_value=d):
        start("mybox")

    mock_run.assert_called_once_with(
        ["lxc-start", "-n", "mybox"], check=True,
    )


@patch("kento.start.subprocess.run")
@patch("kento.start.require_root")
def test_start_pve(mock_root, mock_run, tmp_path):
    d = tmp_path / "100"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    (d / "kento-mode").write_text("pve\n")

    with patch("kento.start.resolve_container", return_value=d):
        start("mybox")

    mock_run.assert_called_once_with(
        ["pct", "start", "100"], check=True,
    )


@patch("kento.start.subprocess.run")
@patch("kento.start.require_root")
def test_start_defaults_to_lxc(mock_root, mock_run, tmp_path):
    d = tmp_path / "mybox"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    # No kento-mode file

    with patch("kento.start.resolve_container", return_value=d):
        start("mybox")

    mock_run.assert_called_once_with(
        ["lxc-start", "-n", "mybox"], check=True,
    )


@patch("kento.start.require_root")
def test_start_vm(mock_root, tmp_path):
    d = tmp_path / "testvm"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    (d / "kento-mode").write_text("vm\n")

    with patch("kento.start.resolve_container", return_value=d), \
         patch("kento.vm.start_vm") as mock_start_vm:
        start("testvm")

    mock_start_vm.assert_called_once_with(d, "testvm")
