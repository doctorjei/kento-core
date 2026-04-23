"""Tests for container start."""

import subprocess
from unittest.mock import patch, MagicMock

import pytest

from kento.start import start


def _ok(*args, **kwargs):
    return subprocess.CompletedProcess(args[0] if args else [], 0, stdout="", stderr="")


@patch("kento.subprocess_util.subprocess.run", side_effect=_ok)
@patch("kento.start.require_root")
def test_start_lxc(mock_root, mock_run, tmp_path):
    d = tmp_path / "mybox"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    (d / "kento-mode").write_text("lxc\n")

    with patch("kento.start.resolve_container", return_value=d):
        start("mybox")

    mock_run.assert_called_once()
    assert list(mock_run.call_args[0][0]) == ["lxc-start", "-n", "mybox"]


@patch("kento.subprocess_util.subprocess.run", side_effect=_ok)
@patch("kento.start.require_root")
def test_start_pve(mock_root, mock_run, tmp_path):
    d = tmp_path / "100"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    (d / "kento-mode").write_text("pve\n")

    with patch("kento.start.resolve_container", return_value=d):
        start("mybox")

    mock_run.assert_called_once()
    assert list(mock_run.call_args[0][0]) == ["pct", "start", "100"]


@patch("kento.subprocess_util.subprocess.run", side_effect=_ok)
@patch("kento.start.require_root")
def test_start_defaults_to_lxc(mock_root, mock_run, tmp_path):
    d = tmp_path / "mybox"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    # No kento-mode file

    with patch("kento.start.resolve_container", return_value=d):
        start("mybox")

    mock_run.assert_called_once()
    assert list(mock_run.call_args[0][0]) == ["lxc-start", "-n", "mybox"]


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


# --- PVE-VM mode tests ---


class TestStartPveVm:
    @patch("kento.subprocess_util.subprocess.run", side_effect=_ok)
    @patch("kento.start.require_root")
    def test_start_calls_qm(self, mock_root, mock_run, tmp_path):
        d = tmp_path / "test"
        d.mkdir()
        (d / "kento-image").write_text("myimage\n")
        (d / "kento-mode").write_text("pve-vm\n")
        (d / "kento-name").write_text("test\n")
        (d / "kento-vmid").write_text("100\n")

        with patch("kento.start.resolve_container", return_value=d):
            start("test")

        mock_run.assert_called_once()
        assert list(mock_run.call_args[0][0]) == ["qm", "start", "100"]


# --- run_or_die error-path tests (F8) ---


class TestStartFailurePaths:
    """Failures must print a clean error + hint and SystemExit(1),
    never a CalledProcessError traceback."""

    @patch("kento.start.require_root")
    def test_lxc_start_failure_prints_clean_error(self, mock_root, tmp_path, capsys):
        d = tmp_path / "mybox"
        d.mkdir()
        (d / "kento-image").write_text("debian:12\n")
        (d / "kento-mode").write_text("lxc\n")

        def _fail(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boot failed")

        with patch("kento.subprocess_util.subprocess.run", side_effect=_fail), \
             patch("kento.start.resolve_container", return_value=d):
            with pytest.raises(SystemExit) as exc:
                start("mybox")
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "Error: failed to start LXC container mybox" in captured.err
        assert "boot failed" in captured.err
        assert "hint:" in captured.err
        assert "Traceback" not in captured.err

    @patch("kento.start.require_root")
    def test_pve_start_failure_prints_clean_error(self, mock_root, tmp_path, capsys):
        d = tmp_path / "100"
        d.mkdir()
        (d / "kento-image").write_text("debian:12\n")
        (d / "kento-mode").write_text("pve\n")

        def _fail(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="pct refused")

        with patch("kento.subprocess_util.subprocess.run", side_effect=_fail), \
             patch("kento.start.resolve_container", return_value=d):
            with pytest.raises(SystemExit) as exc:
                start("mybox")
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "Error: failed to start PVE container mybox" in captured.err
        assert "pct refused" in captured.err

    @patch("kento.start.require_root")
    def test_pve_vm_start_failure_prints_clean_error(self, mock_root, tmp_path, capsys):
        d = tmp_path / "test"
        d.mkdir()
        (d / "kento-mode").write_text("pve-vm\n")
        (d / "kento-vmid").write_text("100\n")

        def _fail(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="qm refused")

        with patch("kento.subprocess_util.subprocess.run", side_effect=_fail), \
             patch("kento.start.resolve_container", return_value=d):
            with pytest.raises(SystemExit) as exc:
                start("test")
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "Error: failed to start PVE VM test" in captured.err
        assert "qm refused" in captured.err
