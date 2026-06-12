"""Tests for container shutdown/stop."""

import logging
import subprocess
from unittest.mock import patch

import pytest

from kento.errors import SubprocessError, ValidationError
from kento.stop import shutdown, stop


def _ok(*args, **kwargs):
    return subprocess.CompletedProcess(args[0] if args else [], 0, stdout="", stderr="")


# -- Graceful shutdown (default) --

@patch("kento.stop.is_running", return_value=True)
@patch("kento.subprocess_util.subprocess.run", side_effect=_ok)
@patch("kento.stop.require_root")
def test_shutdown_lxc_graceful(mock_root, mock_run, mock_running, tmp_path):
    d = tmp_path / "mybox"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    (d / "kento-mode").write_text("lxc\n")

    with patch("kento.stop.resolve_container", return_value=d):
        shutdown("mybox")

    mock_run.assert_called_once()
    assert list(mock_run.call_args[0][0]) == ["lxc-stop", "-n", "mybox"]


@patch("kento.stop.is_running", return_value=True)
@patch("kento.subprocess_util.subprocess.run", side_effect=_ok)
@patch("kento.stop.require_root")
def test_shutdown_pve_graceful(mock_root, mock_run, mock_running, tmp_path):
    d = tmp_path / "100"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    (d / "kento-mode").write_text("pve\n")

    with patch("kento.stop.resolve_container", return_value=d):
        shutdown("mybox")

    mock_run.assert_called_once()
    assert list(mock_run.call_args[0][0]) == ["pct", "shutdown", "100"]


# -- Force stop --

@patch("kento.stop.is_running", return_value=True)
@patch("kento.subprocess_util.subprocess.run", side_effect=_ok)
@patch("kento.stop.require_root")
def test_shutdown_lxc_force(mock_root, mock_run, mock_running, tmp_path):
    d = tmp_path / "mybox"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    (d / "kento-mode").write_text("lxc\n")

    with patch("kento.stop.resolve_container", return_value=d):
        shutdown("mybox", force=True)

    mock_run.assert_called_once()
    assert list(mock_run.call_args[0][0]) == ["lxc-stop", "-n", "mybox", "-k"]


@patch("kento.stop.is_running", return_value=True)
@patch("kento.subprocess_util.subprocess.run", side_effect=_ok)
@patch("kento.stop.require_root")
def test_shutdown_pve_force(mock_root, mock_run, mock_running, tmp_path):
    d = tmp_path / "100"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    (d / "kento-mode").write_text("pve\n")

    with patch("kento.stop.resolve_container", return_value=d):
        shutdown("mybox", force=True)

    mock_run.assert_called_once()
    assert list(mock_run.call_args[0][0]) == ["pct", "stop", "100"]


# -- Defaults and aliases --

@patch("kento.stop.is_running", return_value=True)
@patch("kento.subprocess_util.subprocess.run", side_effect=_ok)
@patch("kento.stop.require_root")
def test_shutdown_defaults_to_lxc(mock_root, mock_run, mock_running, tmp_path):
    d = tmp_path / "mybox"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")

    with patch("kento.stop.resolve_container", return_value=d):
        shutdown("mybox")

    mock_run.assert_called_once()
    assert list(mock_run.call_args[0][0]) == ["lxc-stop", "-n", "mybox"]


@patch("kento.stop.is_running", return_value=True)
@patch("kento.stop.require_root")
def test_shutdown_vm(mock_root, mock_running, tmp_path):
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
    @patch("kento.stop.is_running", return_value=True)
    @patch("kento.subprocess_util.subprocess.run", side_effect=_ok)
    @patch("kento.stop.require_root")
    def test_graceful_calls_qm_shutdown(self, mock_root, mock_run, mock_running, tmp_path):
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
            "qm", "shutdown", "100", "--timeout", "30", "--forceStop"
        ]

    @patch("kento.stop.is_running", return_value=True)
    @patch("kento.subprocess_util.subprocess.run", side_effect=_ok)
    @patch("kento.stop.require_root")
    def test_timeout_overrides_default(self, mock_root, mock_run, mock_running, tmp_path):
        d = tmp_path / "test"
        d.mkdir()
        (d / "kento-mode").write_text("pve-vm\n")
        (d / "kento-vmid").write_text("100\n")

        with patch("kento.stop.resolve_container", return_value=d):
            shutdown("test", timeout=60)

        mock_run.assert_called_once()
        assert list(mock_run.call_args[0][0]) == [
            "qm", "shutdown", "100", "--timeout", "60", "--forceStop"
        ]

    @patch("kento.stop.is_running", return_value=True)
    @patch("kento.subprocess_util.subprocess.run", side_effect=_ok)
    @patch("kento.stop.require_root")
    def test_graceful_only_drops_timeout_and_forcestop(self, mock_root, mock_run, mock_running, tmp_path):
        d = tmp_path / "test"
        d.mkdir()
        (d / "kento-mode").write_text("pve-vm\n")
        (d / "kento-vmid").write_text("100\n")

        with patch("kento.stop.resolve_container", return_value=d):
            shutdown("test", graceful_only=True)

        mock_run.assert_called_once()
        assert list(mock_run.call_args[0][0]) == ["qm", "shutdown", "100"]

    @patch("kento.stop.is_running", return_value=True)
    @patch("kento.subprocess_util.subprocess.run", side_effect=_ok)
    @patch("kento.stop.require_root")
    def test_force_calls_qm_stop(self, mock_root, mock_run, mock_running, tmp_path):
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

    @patch("kento.stop.is_running", return_value=True)
    @patch("kento.stop.require_root")
    def test_warning_fires_when_qm_reports_fallback(self, mock_root, mock_running,
                                                     tmp_path, caplog):
        d = tmp_path / "test"
        d.mkdir()
        (d / "kento-mode").write_text("pve-vm\n")
        (d / "kento-vmid").write_text("100\n")

        def _fallback(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd, 0, stdout="VM still running - terminating now with SIGTERM\n",
                stderr="",
            )

        with caplog.at_level(logging.WARNING, logger="kento"), \
             patch("kento.subprocess_util.subprocess.run", side_effect=_fallback), \
             patch("kento.stop.resolve_container", return_value=d):
            shutdown("test")

        assert "did not honor ACPI shutdown within 30s" in caplog.text
        assert "hard-stopped" in caplog.text

    @patch("kento.stop.is_running", return_value=True)
    @patch("kento.stop.require_root")
    def test_no_warning_when_graceful_succeeds(self, mock_root, mock_running,
                                                tmp_path, caplog):
        d = tmp_path / "test"
        d.mkdir()
        (d / "kento-mode").write_text("pve-vm\n")
        (d / "kento-vmid").write_text("100\n")

        def _clean(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with caplog.at_level(logging.WARNING, logger="kento"), \
             patch("kento.subprocess_util.subprocess.run", side_effect=_clean), \
             patch("kento.stop.resolve_container", return_value=d):
            shutdown("test")

        assert "did not honor ACPI" not in caplog.text
        assert "hard-stopped" not in caplog.text


class TestShutdownFlagMutex:
    """The three new pve-vm shutdown flags (--timeout, --graceful-only,
    --force) have meaningful pairwise conflicts; the API rejects them
    rather than silently picking a winner."""

    @patch("kento.stop.is_running", return_value=True)
    @patch("kento.stop.require_root")
    def test_graceful_only_and_force_conflict(self, mock_root, mock_running,
                                                tmp_path):
        d = tmp_path / "test"
        d.mkdir()
        (d / "kento-mode").write_text("pve-vm\n")
        (d / "kento-vmid").write_text("100\n")

        with patch("kento.stop.resolve_container", return_value=d):
            with pytest.raises(ValidationError) as exc:
                shutdown("test", force=True, graceful_only=True)
        assert "--graceful-only" in str(exc.value)
        assert "--force" in str(exc.value)
        assert "mutually exclusive" in str(exc.value)

    @patch("kento.stop.is_running", return_value=True)
    @patch("kento.stop.require_root")
    def test_graceful_only_and_timeout_conflict(self, mock_root, mock_running,
                                                  tmp_path):
        d = tmp_path / "test"
        d.mkdir()
        (d / "kento-mode").write_text("pve-vm\n")
        (d / "kento-vmid").write_text("100\n")

        with patch("kento.stop.resolve_container", return_value=d):
            with pytest.raises(ValidationError) as exc:
                shutdown("test", graceful_only=True, timeout=45)
        assert "--timeout" in str(exc.value)
        assert "--graceful-only" in str(exc.value)

    @patch("kento.stop.is_running", return_value=True)
    @patch("kento.stop.require_root")
    def test_force_and_timeout_conflict(self, mock_root, mock_running,
                                          tmp_path):
        d = tmp_path / "test"
        d.mkdir()
        (d / "kento-mode").write_text("pve-vm\n")
        (d / "kento-vmid").write_text("100\n")

        with patch("kento.stop.resolve_container", return_value=d):
            with pytest.raises(ValidationError) as exc:
                shutdown("test", force=True, timeout=45)
        assert "--timeout" in str(exc.value)
        assert "--force" in str(exc.value)


# --- run_or_die error-path tests (F8) ---


class TestShutdownFailurePaths:
    """Failures must raise SubprocessError with a clean message,
    never a CalledProcessError traceback."""

    @patch("kento.stop.is_running", return_value=True)
    @patch("kento.stop.require_root")
    def test_lxc_stop_failure_prints_clean_error(self, mock_root, mock_running, tmp_path):
        d = tmp_path / "mybox"
        d.mkdir()
        (d / "kento-mode").write_text("lxc\n")

        def _fail(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="lxc-stop error")

        with patch("kento.subprocess_util.subprocess.run", side_effect=_fail), \
             patch("kento.stop.resolve_container", return_value=d):
            with pytest.raises(SubprocessError) as exc:
                shutdown("mybox")
        assert exc.value.returncode == 1
        assert exc.value.cmd == ["lxc-stop", "-n", "mybox"]
        assert "stop LXC container mybox" in str(exc.value)
        assert "lxc-stop error" in str(exc.value)

    @patch("kento.stop.is_running", return_value=True)
    @patch("kento.stop.require_root")
    def test_pve_shutdown_failure_prints_clean_error(self, mock_root, mock_running, tmp_path):
        d = tmp_path / "100"
        d.mkdir()
        (d / "kento-mode").write_text("pve\n")

        def _fail(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="pct refused")

        with patch("kento.subprocess_util.subprocess.run", side_effect=_fail), \
             patch("kento.stop.resolve_container", return_value=d):
            with pytest.raises(SubprocessError) as exc:
                shutdown("mybox")
        assert exc.value.returncode == 1
        assert "shut down PVE container mybox" in str(exc.value)
        assert "pct refused" in str(exc.value)

    @patch("kento.stop.is_running", return_value=True)
    @patch("kento.stop.require_root")
    def test_pve_vm_stop_failure_prints_clean_error(self, mock_root, mock_running, tmp_path):
        d = tmp_path / "test"
        d.mkdir()
        (d / "kento-mode").write_text("pve-vm\n")
        (d / "kento-vmid").write_text("100\n")

        def _fail(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="qm refused")

        with patch("kento.subprocess_util.subprocess.run", side_effect=_fail), \
             patch("kento.stop.resolve_container", return_value=d):
            with pytest.raises(SubprocessError) as exc:
                shutdown("test", force=True)
        assert exc.value.returncode == 1
        assert "stop PVE VM test" in str(exc.value)
        assert "qm refused" in str(exc.value)


# --- F15: idempotency ---


class TestShutdownIdempotent:
    @patch("kento.stop.is_running", return_value=False)
    @patch("kento.subprocess_util.subprocess.run", side_effect=_ok)
    @patch("kento.stop.require_root")
    def test_lxc_already_stopped_is_no_op(self, mock_root, mock_run, mock_running,
                                            tmp_path, caplog):
        d = tmp_path / "mybox"
        d.mkdir()
        (d / "kento-image").write_text("debian:12\n")
        (d / "kento-mode").write_text("lxc\n")

        with caplog.at_level(logging.INFO, logger="kento"), \
             patch("kento.stop.resolve_container", return_value=d):
            shutdown("mybox")

        mock_run.assert_not_called()
        assert "Already stopped: mybox" in caplog.text

    @patch("kento.stop.is_running", return_value=False)
    @patch("kento.subprocess_util.subprocess.run", side_effect=_ok)
    @patch("kento.stop.require_root")
    def test_pve_already_stopped_is_no_op(self, mock_root, mock_run, mock_running,
                                            tmp_path, caplog):
        d = tmp_path / "100"
        d.mkdir()
        (d / "kento-image").write_text("debian:12\n")
        (d / "kento-mode").write_text("pve\n")

        with caplog.at_level(logging.INFO, logger="kento"), \
             patch("kento.stop.resolve_container", return_value=d):
            shutdown("mybox")

        mock_run.assert_not_called()
        assert "Already stopped: mybox" in caplog.text

    @patch("kento.stop.is_running", return_value=False)
    @patch("kento.subprocess_util.subprocess.run", side_effect=_ok)
    @patch("kento.stop.require_root")
    def test_pve_vm_already_stopped_is_no_op(self, mock_root, mock_run, mock_running,
                                               tmp_path, caplog):
        d = tmp_path / "test"
        d.mkdir()
        (d / "kento-image").write_text("myimage\n")
        (d / "kento-mode").write_text("pve-vm\n")
        (d / "kento-vmid").write_text("100\n")

        with caplog.at_level(logging.INFO, logger="kento"), \
             patch("kento.stop.resolve_container", return_value=d):
            shutdown("test")

        mock_run.assert_not_called()
        assert "Already stopped: test" in caplog.text

    @patch("kento.stop.is_running", return_value=False)
    @patch("kento.stop.require_root")
    def test_vm_already_stopped_skips_stop_vm(self, mock_root, mock_running,
                                                tmp_path, caplog):
        d = tmp_path / "testvm"
        d.mkdir()
        (d / "kento-image").write_text("debian:12\n")
        (d / "kento-mode").write_text("vm\n")

        with caplog.at_level(logging.INFO, logger="kento"), \
             patch("kento.stop.resolve_container", return_value=d), \
             patch("kento.vm.stop_vm") as mock_stop_vm:
            shutdown("testvm")

        mock_stop_vm.assert_not_called()
        assert "Already stopped: testvm" in caplog.text


# --- Fix 2: PVE status-query "assume running" race vs. an actually-stopped
# instance must NOT hard-exit. is_running() returns True on a status timeout/
# non-zero, so shutdown() proceeds to pct/qm shutdown, which then reports
# "not running"; that must be reported as "Already stopped", not SystemExit(1).


class TestShutdownPveAssumeRunningRace:
    @patch("kento.stop.require_root")
    def test_pve_lxc_shutdown_tolerates_already_stopped(self, mock_root,
                                                         tmp_path, caplog):
        d = tmp_path / "100"
        d.mkdir()
        (d / "kento-mode").write_text("pve\n")

        # is_running first ASSUMES RUNNING (the status query timed out), then
        # the re-query inside _pve_shutdown_or_die reports actually-stopped.
        running_calls = iter([True, False])

        def _running(*a, **k):
            return next(running_calls)

        def _not_running(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd, 2, stdout="", stderr="Container 100 is not running\n")

        with caplog.at_level(logging.INFO, logger="kento"), \
             patch("kento.stop.is_running", side_effect=_running), \
             patch("kento.subprocess_util.subprocess.run",
                   side_effect=_not_running), \
             patch("kento.stop.resolve_container", return_value=d):
            shutdown("mybox")  # must NOT raise

        assert "Already stopped: mybox" in caplog.text

    @patch("kento.stop.require_root")
    def test_pve_vm_shutdown_tolerates_already_stopped(self, mock_root,
                                                       tmp_path, caplog):
        d = tmp_path / "test"
        d.mkdir()
        (d / "kento-mode").write_text("pve-vm\n")
        (d / "kento-vmid").write_text("100\n")

        running_calls = iter([True, False])

        def _running(*a, **k):
            return next(running_calls)

        def _not_running(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd, 255, stdout="", stderr="VM 100 not running\n")

        with caplog.at_level(logging.INFO, logger="kento"), \
             patch("kento.stop.is_running", side_effect=_running), \
             patch("kento.subprocess_util.subprocess.run",
                   side_effect=_not_running), \
             patch("kento.stop.resolve_container", return_value=d):
            shutdown("test")  # must NOT raise

        assert "Already stopped: test" in caplog.text

    @patch("kento.stop.require_root")
    def test_pve_vm_force_tolerates_already_stopped(self, mock_root,
                                                    tmp_path, caplog):
        d = tmp_path / "test"
        d.mkdir()
        (d / "kento-mode").write_text("pve-vm\n")
        (d / "kento-vmid").write_text("100\n")

        running_calls = iter([True, False])

        def _running(*a, **k):
            return next(running_calls)

        def _not_running(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="VM 100 not running\n")

        with caplog.at_level(logging.INFO, logger="kento"), \
             patch("kento.stop.is_running", side_effect=_running), \
             patch("kento.subprocess_util.subprocess.run",
                   side_effect=_not_running), \
             patch("kento.stop.resolve_container", return_value=d):
            shutdown("test", force=True)  # must NOT raise

        assert "Already stopped: test" in caplog.text

    @patch("kento.stop.require_root")
    def test_pve_lxc_genuinely_running_still_stops(self, mock_root,
                                                   tmp_path, caplog):
        """A normally-running instance must still stop cleanly (single
        shutdown call, no false 'Already stopped')."""
        d = tmp_path / "100"
        d.mkdir()
        (d / "kento-mode").write_text("pve\n")

        with caplog.at_level(logging.INFO, logger="kento"), \
             patch("kento.stop.is_running", return_value=True), \
             patch("kento.subprocess_util.subprocess.run",
                   side_effect=_ok) as mock_run, \
             patch("kento.stop.resolve_container", return_value=d):
            shutdown("mybox")

        mock_run.assert_called_once()
        assert list(mock_run.call_args[0][0]) == ["pct", "shutdown", "100"]
        assert "Shut down: mybox" in caplog.text
        assert "Already stopped" not in caplog.text

    @patch("kento.stop.require_root")
    def test_pve_lxc_genuine_failure_still_hard_errors(self, mock_root,
                                                       tmp_path):
        """A real failure (instance still up, tool refuses) must still raise
        SubprocessError, not be swallowed as already-stopped."""
        d = tmp_path / "100"
        d.mkdir()
        (d / "kento-mode").write_text("pve\n")

        def _fail(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="pct refused (lock busy)")

        with patch("kento.stop.is_running", return_value=True), \
             patch("kento.subprocess_util.subprocess.run", side_effect=_fail), \
             patch("kento.stop.resolve_container", return_value=d):
            with pytest.raises(SubprocessError) as exc:
                shutdown("mybox")
        assert exc.value.returncode == 1
        assert "shut down PVE container mybox" in str(exc.value)
        assert "pct refused" in str(exc.value)
