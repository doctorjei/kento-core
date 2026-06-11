"""Tests for the logs command."""

import subprocess
from unittest.mock import patch

import pytest

from kento.logs import logs


def _ok(*args, **kwargs):
    return subprocess.CompletedProcess(args[0] if args else [], 0)


# -- Per-mode dispatch (mocked subprocess) --


@patch("kento.logs.subprocess.run", side_effect=_ok)
@patch("kento.logs.require_root")
def test_logs_lxc_calls_journalctl(mock_root, mock_run, tmp_path):
    d = tmp_path / "mybox"
    d.mkdir()

    with patch("kento.logs.resolve_any", return_value=(d, "lxc")):
        rc = logs("mybox", ["-f", "-n", "50"])

    assert rc == 0
    mock_run.assert_called_once()
    assert list(mock_run.call_args[0][0]) == [
        "lxc-attach", "-n", "mybox", "--", "journalctl", "-f", "-n", "50"]


@patch("kento.logs.subprocess.run", side_effect=_ok)
@patch("kento.logs.require_root")
def test_logs_pve_lxc_calls_pct_exec_journalctl(mock_root, mock_run, tmp_path):
    d = tmp_path / "100"
    d.mkdir()

    with patch("kento.logs.resolve_any", return_value=(d, "pve")):
        rc = logs("mybox", ["-n", "10"])

    assert rc == 0
    mock_run.assert_called_once()
    assert list(mock_run.call_args[0][0]) == [
        "pct", "exec", "100", "--", "journalctl", "-n", "10"]


@patch("kento.logs.subprocess.run", side_effect=_ok)
@patch("kento.logs.require_root")
def test_logs_empty_args_ok(mock_root, mock_run, tmp_path):
    d = tmp_path / "mybox"
    d.mkdir()

    with patch("kento.logs.resolve_any", return_value=(d, "lxc")):
        rc = logs("mybox", [])

    assert rc == 0
    assert list(mock_run.call_args[0][0]) == [
        "lxc-attach", "-n", "mybox", "--", "journalctl"]


@patch("kento.logs.subprocess.run", side_effect=_ok)
@patch("kento.logs.require_root")
def test_logs_vm_errors(mock_root, mock_run, tmp_path, capsys):
    d = tmp_path / "myvm"
    d.mkdir()

    with patch("kento.logs.resolve_any", return_value=(d, "vm")):
        rc = logs("myvm", ["-n", "10"])

    assert rc != 0
    mock_run.assert_not_called()
    captured = capsys.readouterr()
    assert "not supported for VM instances" in captured.err
    assert "attach" in captured.err


@patch("kento.logs.subprocess.run", side_effect=_ok)
@patch("kento.logs.require_root")
def test_logs_pve_vm_errors(mock_root, mock_run, tmp_path, capsys):
    d = tmp_path / "pvevm"
    d.mkdir()

    with patch("kento.logs.resolve_any", return_value=(d, "pve-vm")):
        rc = logs("pvevm", [])

    assert rc != 0
    mock_run.assert_not_called()
    captured = capsys.readouterr()
    assert "not supported for VM instances" in captured.err


@patch("kento.logs.subprocess.run")
@patch("kento.logs.require_root")
def test_logs_propagates_returncode(mock_root, mock_run, tmp_path):
    d = tmp_path / "mybox"
    d.mkdir()
    mock_run.side_effect = lambda *a, **k: subprocess.CompletedProcess(a[0], 5)

    with patch("kento.logs.resolve_any", return_value=(d, "lxc")):
        rc = logs("mybox", [])

    assert rc == 5


# -- CLI routing --


class TestCliRouting:
    @patch("kento.logs.logs", return_value=0)
    def test_bare_logs_routes(self, mock_logs):
        from kento.cli import main
        with pytest.raises(SystemExit) as exc:
            main(["logs", "foo"])
        assert exc.value.code == 0
        mock_logs.assert_called_once_with("foo", [], namespace=None)

    @patch("kento.logs.logs", return_value=0)
    def test_bare_logs_routes_with_flags(self, mock_logs):
        # -f / -n 50 must reach journalctl, not trip argparse.
        from kento.cli import main
        with pytest.raises(SystemExit) as exc:
            main(["logs", "foo", "-f", "-n", "50"])
        assert exc.value.code == 0
        mock_logs.assert_called_once_with("foo", ["-f", "-n", "50"], namespace=None)

    @patch("kento.logs.logs", return_value=0)
    def test_lxc_logs_routes(self, mock_logs):
        from kento.cli import main
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "logs", "foo", "-n", "10"])
        assert exc.value.code == 0
        mock_logs.assert_called_once_with("foo", ["-n", "10"], namespace="lxc")

    @patch("kento.logs.logs", return_value=0)
    def test_vm_logs_routes(self, mock_logs):
        from kento.cli import main
        with pytest.raises(SystemExit) as exc:
            main(["vm", "logs", "foo"])
        assert exc.value.code == 0
        mock_logs.assert_called_once_with("foo", [], namespace="vm")

    @patch("kento.logs.logs", return_value=4)
    def test_cli_propagates_nonzero_exit(self, mock_logs):
        from kento.cli import main
        with pytest.raises(SystemExit) as exc:
            main(["logs", "foo"])
        assert exc.value.code == 4
