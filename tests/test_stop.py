"""Tests for container shutdown/stop."""

import subprocess
from unittest.mock import patch

import pytest

from kento.stop import shutdown, stop


def _ok(*args, **kwargs):
    return subprocess.CompletedProcess(args[0] if args else [], 0, stdout="", stderr="")


# -- Graceful shutdown (default) --

@patch("kento.subprocess_util.subprocess.run", side_effect=_ok)
@patch("kento.stop.require_root")
def test_shutdown_lxc_graceful(mock_root, mock_run, tmp_path):
    d = tmp_path / "mybox"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    (d / "kento-mode").write_text("lxc\n")

    with patch("kento.stop.resolve_container", return_value=d):
        shutdown("mybox")

    mock_run.assert_called_once()
    assert list(mock_run.call_args[0][0]) == ["lxc-stop", "-n", "mybox"]


@patch("kento.subprocess_util.subprocess.run", side_effect=_ok)
@patch("kento.stop.require_root")
def test_shutdown_pve_graceful(mock_root, mock_run, tmp_path):
    d = tmp_path / "100"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    (d / "kento-mode").write_text("pve\n")

    with patch("kento.stop.resolve_container", return_value=d):
        shutdown("mybox")

    mock_run.assert_called_once()
    assert list(mock_run.call_args[0][0]) == ["pct", "shutdown", "100"]


# -- Force stop --

@patch("kento.subprocess_util.subprocess.run", side_effect=_ok)
@patch("kento.stop.require_root")
def test_shutdown_lxc_force(mock_root, mock_run, tmp_path):
    d = tmp_path / "mybox"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    (d / "kento-mode").write_text("lxc\n")

    with patch("kento.stop.resolve_container", return_value=d):
        shutdown("mybox", force=True)

    mock_run.assert_called_once()
    assert list(mock_run.call_args[0][0]) == ["lxc-stop", "-n", "mybox", "-k"]


@patch("kento.subprocess_util.subprocess.run", side_effect=_ok)
@patch("kento.stop.require_root")
def test_shutdown_pve_force(mock_root, mock_run, tmp_path):
    d = tmp_path / "100"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    (d / "kento-mode").write_text("pve\n")

    with patch("kento.stop.resolve_container", return_value=d):
        shutdown("mybox", force=True)

    mock_run.assert_called_once()
    assert list(mock_run.call_args[0][0]) == ["pct", "stop", "100"]


# -- Defaults and aliases --

@patch("kento.subprocess_util.subprocess.run", side_effect=_ok)
@patch("kento.stop.require_root")
def test_shutdown_defaults_to_lxc(mock_root, mock_run, tmp_path):
    d = tmp_path / "mybox"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")

    with patch("kento.stop.resolve_container", return_value=d):
        shutdown("mybox")

    mock_run.assert_called_once()
    assert list(mock_run.call_args[0][0]) == ["lxc-stop", "-n", "mybox"]


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
    @patch("kento.subprocess_util.subprocess.run", side_effect=_ok)
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

        mock_run.assert_called_once()
        assert list(mock_run.call_args[0][0]) == [
            "qm", "shutdown", "100", "--timeout", "60", "--forceStop", "1"
        ]

    @patch("kento.subprocess_util.subprocess.run", side_effect=_ok)
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

        mock_run.assert_called_once()
        assert list(mock_run.call_args[0][0]) == ["qm", "stop", "100"]


# --- run_or_die error-path tests (F8) ---


class TestShutdownFailurePaths:
    """Failures must print a clean error + hint and SystemExit(1),
    never a CalledProcessError traceback."""

    @patch("kento.stop.require_root")
    def test_lxc_stop_failure_prints_clean_error(self, mock_root, tmp_path, capsys):
        d = tmp_path / "mybox"
        d.mkdir()
        (d / "kento-mode").write_text("lxc\n")

        def _fail(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="lxc-stop error")

        with patch("kento.subprocess_util.subprocess.run", side_effect=_fail), \
             patch("kento.stop.resolve_container", return_value=d):
            with pytest.raises(SystemExit) as exc:
                shutdown("mybox")
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "Error: failed to stop LXC container mybox" in captured.err
        assert "lxc-stop error" in captured.err
        assert "hint:" in captured.err
        assert "Traceback" not in captured.err

    @patch("kento.stop.require_root")
    def test_pve_shutdown_failure_prints_clean_error(self, mock_root, tmp_path, capsys):
        d = tmp_path / "100"
        d.mkdir()
        (d / "kento-mode").write_text("pve\n")

        def _fail(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="pct refused")

        with patch("kento.subprocess_util.subprocess.run", side_effect=_fail), \
             patch("kento.stop.resolve_container", return_value=d):
            with pytest.raises(SystemExit) as exc:
                shutdown("mybox")
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "Error: failed to shut down PVE container mybox" in captured.err
        assert "pct refused" in captured.err

    @patch("kento.stop.require_root")
    def test_pve_vm_stop_failure_prints_clean_error(self, mock_root, tmp_path, capsys):
        d = tmp_path / "test"
        d.mkdir()
        (d / "kento-mode").write_text("pve-vm\n")
        (d / "kento-vmid").write_text("100\n")

        def _fail(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="qm refused")

        with patch("kento.subprocess_util.subprocess.run", side_effect=_fail), \
             patch("kento.stop.resolve_container", return_value=d):
            with pytest.raises(SystemExit) as exc:
                shutdown("test", force=True)
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "Error: failed to stop PVE VM test" in captured.err
        assert "qm refused" in captured.err
