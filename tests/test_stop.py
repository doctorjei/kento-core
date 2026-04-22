"""Tests for container shutdown/stop."""

from unittest.mock import patch

import pytest

from kento.stop import shutdown, stop


# -- Graceful shutdown (default) --

@patch("kento.stop.subprocess.run")
@patch("kento.stop.require_root")
def test_shutdown_lxc_graceful(mock_root, mock_run, tmp_path):
    d = tmp_path / "mybox"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    (d / "kento-mode").write_text("lxc\n")

    with patch("kento.stop.resolve_container", return_value=d):
        shutdown("mybox")

    mock_run.assert_called_once_with(
        ["lxc-stop", "-n", "mybox"], check=True,
    )


@patch("kento.stop.subprocess.run")
@patch("kento.stop.require_root")
def test_shutdown_pve_graceful(mock_root, mock_run, tmp_path):
    d = tmp_path / "100"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    (d / "kento-mode").write_text("pve\n")

    with patch("kento.stop.resolve_container", return_value=d):
        shutdown("mybox")

    mock_run.assert_called_once_with(
        ["pct", "shutdown", "100"], check=True,
    )


# -- Force stop --

@patch("kento.stop.subprocess.run")
@patch("kento.stop.require_root")
def test_shutdown_lxc_force(mock_root, mock_run, tmp_path):
    d = tmp_path / "mybox"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    (d / "kento-mode").write_text("lxc\n")

    with patch("kento.stop.resolve_container", return_value=d):
        shutdown("mybox", force=True)

    mock_run.assert_called_once_with(
        ["lxc-stop", "-n", "mybox", "-k"], check=True,
    )


@patch("kento.stop.subprocess.run")
@patch("kento.stop.require_root")
def test_shutdown_pve_force(mock_root, mock_run, tmp_path):
    d = tmp_path / "100"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    (d / "kento-mode").write_text("pve\n")

    with patch("kento.stop.resolve_container", return_value=d):
        shutdown("mybox", force=True)

    mock_run.assert_called_once_with(
        ["pct", "stop", "100"], check=True,
    )


# -- Defaults and aliases --

@patch("kento.stop.subprocess.run")
@patch("kento.stop.require_root")
def test_shutdown_defaults_to_lxc(mock_root, mock_run, tmp_path):
    d = tmp_path / "mybox"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")

    with patch("kento.stop.resolve_container", return_value=d):
        shutdown("mybox")

    mock_run.assert_called_once_with(
        ["lxc-stop", "-n", "mybox"], check=True,
    )


@patch("kento.stop.require_root")
def test_shutdown_vm(mock_root, tmp_path):
    d = tmp_path / "testvm"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    (d / "kento-mode").write_text("vm\n")

    with patch("kento.stop.resolve_container", return_value=d), \
         patch("kento.vm.stop_vm") as mock_stop_vm:
        shutdown("testvm")

    mock_stop_vm.assert_called_once_with(d, force=False)


def test_stop_is_alias_for_shutdown():
    assert stop is shutdown


# --- PVE-VM mode tests ---


class TestShutdownPveVm:
    @patch("kento.stop.subprocess.run")
    @patch("kento.stop.require_root")
    def test_graceful_calls_qm_shutdown(self, mock_root, mock_run, tmp_path):
        d = tmp_path / "test"
        d.mkdir()
        (d / "kento-image").write_text("myimage\n")
        (d / "kento-mode").write_text("pve-vm\n")
        (d / "kento-name").write_text("test\n")
        (d / "kento-vmid").write_text("100\n")

        with patch("kento.stop.resolve_container", return_value=d):
            shutdown("test")

        mock_run.assert_called_once_with(
            ["qm", "shutdown", "100", "--timeout", "60", "--forceStop", "1"],
            check=True,
        )

    @patch("kento.stop.subprocess.run")
    @patch("kento.stop.require_root")
    def test_force_calls_qm_stop(self, mock_root, mock_run, tmp_path):
        d = tmp_path / "test"
        d.mkdir()
        (d / "kento-image").write_text("myimage\n")
        (d / "kento-mode").write_text("pve-vm\n")
        (d / "kento-name").write_text("test\n")
        (d / "kento-vmid").write_text("100\n")

        with patch("kento.stop.resolve_container", return_value=d):
            shutdown("test", force=True)

        mock_run.assert_called_once_with(["qm", "stop", "100"], check=True)
