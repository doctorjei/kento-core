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


# --------------------------------------------------------------------------- #
# Block 13 — the authorized tty/user/env touch (M13). The DEFAULT path stays
# byte-identical (covered by the per-mode tests above); these cover the new
# command construction. Mocked subprocess; no live process runs.
# --------------------------------------------------------------------------- #


@patch("kento.exec_cmd.subprocess.run", side_effect=_ok)
@patch("kento.exec_cmd.require_root")
def test_exec_default_path_byte_identical_lxc(mock_root, mock_run, tmp_path):
    # Explicitly pin the default-path argv: tty=False, user=None, env=None must
    # produce EXACTLY the pre-touch command (no env/runuser wrapping).
    d = tmp_path / "mybox"
    d.mkdir()
    with patch("kento.exec_cmd.resolve_any", return_value=(d, "lxc")):
        exec_cmd("mybox", ["ls", "-la"], tty=False, user=None, env=None)
    assert list(mock_run.call_args[0][0]) == [
        "lxc-attach", "-n", "mybox", "--", "ls", "-la"]


@patch("kento.exec_cmd.subprocess.run", side_effect=_ok)
@patch("kento.exec_cmd.require_root")
def test_exec_env_prepends_in_guest_env(mock_root, mock_run, tmp_path):
    d = tmp_path / "mybox"
    d.mkdir()
    with patch("kento.exec_cmd.resolve_any", return_value=(d, "lxc")):
        exec_cmd("mybox", ["printenv", "FOO"], env={"FOO": "bar", "BAZ": "qux"})
    # env is set IN THE GUEST via an `env K=V …` prefix (NOT passed to the host
    # subprocess.run, which would set it on lxc-attach itself).
    assert list(mock_run.call_args[0][0]) == [
        "lxc-attach", "-n", "mybox", "--",
        "env", "FOO=bar", "BAZ=qux", "printenv", "FOO"]
    # The host subprocess.run is NOT handed an env= kwarg.
    assert "env" not in mock_run.call_args.kwargs


@patch("kento.exec_cmd.subprocess.run", side_effect=_ok)
@patch("kento.exec_cmd.require_root")
def test_exec_user_wraps_runuser(mock_root, mock_run, tmp_path):
    d = tmp_path / "mybox"
    d.mkdir()
    with patch("kento.exec_cmd.resolve_any", return_value=(d, "lxc")):
        exec_cmd("mybox", ["whoami"], user="alice")
    assert list(mock_run.call_args[0][0]) == [
        "lxc-attach", "-n", "mybox", "--",
        "runuser", "-u", "alice", "--", "whoami"]


@patch("kento.exec_cmd.subprocess.run", side_effect=_ok)
@patch("kento.exec_cmd.require_root")
def test_exec_user_and_env_compose_runuser_outside_env(
        mock_root, mock_run, tmp_path):
    # runuser wraps OUTSIDE env so `runuser` resolves on root's PATH and the env
    # assignments apply to the target command (env innermost, runuser outermost).
    d = tmp_path / "mybox"
    d.mkdir()
    with patch("kento.exec_cmd.resolve_any", return_value=(d, "lxc")):
        exec_cmd("mybox", ["printenv", "FOO"], user="alice", env={"FOO": "bar"})
    assert list(mock_run.call_args[0][0]) == [
        "lxc-attach", "-n", "mybox", "--",
        "runuser", "-u", "alice", "--", "env", "FOO=bar", "printenv", "FOO"]


@patch("kento.exec_cmd.subprocess.run", side_effect=_ok)
@patch("kento.exec_cmd.require_root")
def test_exec_pve_lxc_threads_env_user(mock_root, mock_run, tmp_path):
    # Same wrapping works on the pve-lxc backend (pct exec), inside the guest.
    d = tmp_path / "100"
    d.mkdir()
    with patch("kento.exec_cmd.resolve_any", return_value=(d, "pve")):
        exec_cmd("box", ["id"], user="root", env={"K": "V"})
    assert list(mock_run.call_args[0][0]) == [
        "pct", "exec", "100", "--",
        "runuser", "-u", "root", "--", "env", "K=V", "id"]


@patch("kento.exec_cmd.subprocess.run", side_effect=_ok)
@patch("kento.exec_cmd.require_root")
def test_exec_tty_true_does_not_alter_argv(mock_root, mock_run, tmp_path):
    # tty is honored only via inherited stdio (best-effort) and NEVER changes the
    # in-guest argv (the documented limit — brief JC1). tty=True == default argv.
    d = tmp_path / "mybox"
    d.mkdir()
    with patch("kento.exec_cmd.resolve_any", return_value=(d, "lxc")):
        exec_cmd("mybox", ["bash"], tty=True)
    assert list(mock_run.call_args[0][0]) == [
        "lxc-attach", "-n", "mybox", "--", "bash"]


@patch("kento.exec_cmd.subprocess.run")
@patch("kento.exec_cmd.require_root")
def test_exec_returns_nonzero_without_raising(mock_root, mock_run, tmp_path):
    # M13/§11.9: a non-zero exit is normal info — returned, NOT raised.
    mock_run.return_value = subprocess.CompletedProcess([], 7)
    d = tmp_path / "mybox"
    d.mkdir()
    with patch("kento.exec_cmd.resolve_any", return_value=(d, "lxc")):
        rc = exec_cmd("mybox", ["grep", "x", "/nope"])
    assert rc == 7
