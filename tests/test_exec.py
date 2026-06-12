"""Tests for the exec command."""

import subprocess
from unittest.mock import patch

import pytest

from kento.errors import ModeError, ValidationError
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
def test_exec_vm_errors_without_running(mock_root, mock_run, tmp_path):
    d = tmp_path / "myvm"
    d.mkdir()

    with patch("kento.exec_cmd.resolve_any", return_value=(d, "vm")):
        with pytest.raises(ModeError, match="not supported for VM instances"):
            exec_cmd("myvm", ["ls"])

    mock_run.assert_not_called()


@patch("kento.exec_cmd.subprocess.run", side_effect=_ok)
@patch("kento.exec_cmd.require_root")
def test_exec_pve_vm_errors(mock_root, mock_run, tmp_path):
    d = tmp_path / "pvevm"
    d.mkdir()

    with patch("kento.exec_cmd.resolve_any", return_value=(d, "pve-vm")):
        with pytest.raises(ModeError, match="not supported for VM instances"):
            exec_cmd("pvevm", ["ls"])

    mock_run.assert_not_called()


@patch("kento.exec_cmd.subprocess.run", side_effect=_ok)
@patch("kento.exec_cmd.require_root")
def test_exec_empty_command_raises_validation_error(mock_root, mock_run, tmp_path):
    # Empty command must error before resolving / running anything.
    with patch("kento.exec_cmd.resolve_any") as mock_resolve:
        with pytest.raises(ValidationError, match="requires a command"):
            exec_cmd("mybox", [])

    mock_run.assert_not_called()
    mock_resolve.assert_not_called()


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
