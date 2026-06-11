"""Tests for the exec command."""

import subprocess
from unittest.mock import patch

import pytest

from kento.exec_cmd import exec_cmd


def _ok(*args, **kwargs):
    return subprocess.CompletedProcess(args[0] if args else [], 0)


# -- Per-mode dispatch (mocked subprocess) --


@patch("kento.exec_cmd.subprocess.run", side_effect=_ok)
@patch("kento.exec_cmd.require_root")
def test_exec_lxc_calls_lxc_attach(mock_root, mock_run, tmp_path):
    d = tmp_path / "mybox"
    d.mkdir()

    with patch("kento.exec_cmd.resolve_any", return_value=(d, "lxc")):
        rc = exec_cmd("mybox", ["ls", "-la"])

    assert rc == 0
    mock_run.assert_called_once()
    assert list(mock_run.call_args[0][0]) == [
        "lxc-attach", "-n", "mybox", "--", "ls", "-la"]


@patch("kento.exec_cmd.subprocess.run", side_effect=_ok)
@patch("kento.exec_cmd.require_root")
def test_exec_pve_lxc_calls_pct_exec(mock_root, mock_run, tmp_path):
    d = tmp_path / "100"
    d.mkdir()

    with patch("kento.exec_cmd.resolve_any", return_value=(d, "pve")):
        rc = exec_cmd("mybox", ["ls", "-la"])

    assert rc == 0
    mock_run.assert_called_once()
    assert list(mock_run.call_args[0][0]) == [
        "pct", "exec", "100", "--", "ls", "-la"]


@patch("kento.exec_cmd.subprocess.run", side_effect=_ok)
@patch("kento.exec_cmd.require_root")
def test_exec_vm_errors_without_running(mock_root, mock_run, tmp_path, capsys):
    d = tmp_path / "myvm"
    d.mkdir()

    with patch("kento.exec_cmd.resolve_any", return_value=(d, "vm")):
        rc = exec_cmd("myvm", ["ls"])

    assert rc != 0
    mock_run.assert_not_called()
    captured = capsys.readouterr()
    assert "not supported for VM instances" in captured.err
    assert "SSH" in captured.err


@patch("kento.exec_cmd.subprocess.run", side_effect=_ok)
@patch("kento.exec_cmd.require_root")
def test_exec_pve_vm_errors(mock_root, mock_run, tmp_path, capsys):
    d = tmp_path / "pvevm"
    d.mkdir()

    with patch("kento.exec_cmd.resolve_any", return_value=(d, "pve-vm")):
        rc = exec_cmd("pvevm", ["ls"])

    assert rc != 0
    mock_run.assert_not_called()
    captured = capsys.readouterr()
    assert "not supported for VM instances" in captured.err


@patch("kento.exec_cmd.subprocess.run", side_effect=_ok)
@patch("kento.exec_cmd.require_root")
def test_exec_empty_command_errors_rc2(mock_root, mock_run, tmp_path, capsys):
    # Empty command must error before resolving / running anything.
    with patch("kento.exec_cmd.resolve_any") as mock_resolve:
        rc = exec_cmd("mybox", [])

    assert rc == 2
    mock_run.assert_not_called()
    mock_resolve.assert_not_called()
    captured = capsys.readouterr()
    assert "requires a command" in captured.err


@patch("kento.exec_cmd.subprocess.run")
@patch("kento.exec_cmd.require_root")
def test_exec_propagates_returncode(mock_root, mock_run, tmp_path):
    d = tmp_path / "mybox"
    d.mkdir()
    mock_run.side_effect = lambda *a, **k: subprocess.CompletedProcess(a[0], 7)

    with patch("kento.exec_cmd.resolve_any", return_value=(d, "lxc")):
        rc = exec_cmd("mybox", ["false"])

    assert rc == 7


# -- Namespace scope is forwarded to resolve_any (FIX 1) --


@patch("kento.exec_cmd.subprocess.run", side_effect=_ok)
@patch("kento.exec_cmd.require_root")
def test_exec_forwards_namespace_to_resolve_any(mock_root, mock_run, tmp_path):
    """exec_cmd must pass its namespace through so a duplicate name created via
    `create --force` resolves in the requested namespace instead of aborting."""
    d = tmp_path / "dup"
    d.mkdir()

    with patch("kento.exec_cmd.resolve_any",
               return_value=(d, "lxc")) as mock_resolve:
        exec_cmd("dup", ["ls"], namespace="lxc")

    mock_resolve.assert_called_once_with("dup", "lxc")


@patch("kento.exec_cmd.subprocess.run", side_effect=_ok)
@patch("kento.exec_cmd.require_root")
def test_exec_default_namespace_is_none(mock_root, mock_run, tmp_path):
    d = tmp_path / "mybox"
    d.mkdir()

    with patch("kento.exec_cmd.resolve_any",
               return_value=(d, "lxc")) as mock_resolve:
        exec_cmd("mybox", ["ls"])

    mock_resolve.assert_called_once_with("mybox", None)


# -- CLI routing --


class TestCliRouting:
    @patch("kento.exec_cmd.exec_cmd", return_value=0)
    def test_bare_exec_routes_with_dashdash(self, mock_exec):
        from kento.cli import main
        with pytest.raises(SystemExit) as exc:
            main(["exec", "foo", "--", "ls", "-la"])
        assert exc.value.code == 0
        mock_exec.assert_called_once_with("foo", ["ls", "-la"], namespace=None)

    @patch("kento.exec_cmd.exec_cmd", return_value=0)
    def test_bare_exec_routes_without_dashdash(self, mock_exec):
        from kento.cli import main
        with pytest.raises(SystemExit) as exc:
            main(["exec", "foo", "ls", "-la"])
        assert exc.value.code == 0
        mock_exec.assert_called_once_with("foo", ["ls", "-la"], namespace=None)

    @patch("kento.exec_cmd.exec_cmd", return_value=0)
    def test_lxc_exec_routes(self, mock_exec):
        from kento.cli import main
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "exec", "foo", "--", "ls"])
        assert exc.value.code == 0
        mock_exec.assert_called_once_with("foo", ["ls"], namespace="lxc")

    @patch("kento.exec_cmd.exec_cmd", return_value=0)
    def test_vm_exec_routes(self, mock_exec):
        from kento.cli import main
        with pytest.raises(SystemExit) as exc:
            main(["vm", "exec", "foo", "--", "ls"])
        assert exc.value.code == 0
        mock_exec.assert_called_once_with("foo", ["ls"], namespace="vm")

    @patch("kento.exec_cmd.exec_cmd", return_value=0)
    def test_exec_remainder_captures_flags(self, mock_exec):
        # Flags after the command name must be captured, not parsed by argparse.
        from kento.cli import main
        with pytest.raises(SystemExit):
            main(["exec", "foo", "--", "journalctl", "-f", "-n", "50"])
        mock_exec.assert_called_once_with(
            "foo", ["journalctl", "-f", "-n", "50"], namespace=None)

    @patch("kento.exec_cmd.exec_cmd", return_value=3)
    def test_cli_propagates_nonzero_exit(self, mock_exec):
        from kento.cli import main
        with pytest.raises(SystemExit) as exc:
            main(["exec", "foo", "--", "ls"])
        assert exc.value.code == 3
