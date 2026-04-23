"""Integration tests for F8: CalledProcessError no longer escapes as a traceback.

These tests exercise the failure paths of the 11 previously-raw-check=True
sites. For each, a non-zero subprocess result must produce a kento-branded
"Error:" message on stderr and (where run_or_die is used) exit(1) — never a
CalledProcessError Python traceback.

See the edge-case audit (F8) and the error-message audit (Grade D) for the
motivating findings.
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kento.create import create
from kento.destroy import destroy


# --- destroy -f with stop failure: must continue to cleanup ---


def _make_destroy_container(tmp_path, name="test", mode="lxc"):
    lxc_dir = tmp_path / name
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-mode").write_text(mode + "\n")
    (lxc_dir / "kento-name").write_text(name + "\n")
    (lxc_dir / "rootfs").mkdir()
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    return lxc_dir


class TestDestroyStopFailureContinuesCleanup:
    """F8: when destroy(-f) hits a failing stop it must WARN and keep going
    so a wedged instance can still be removed. No SystemExit."""

    @patch("kento.destroy.require_root")
    @patch("kento.destroy.is_running", return_value=True)
    def test_lxc_stop_failure_warns_and_continues(
            self, mock_running, mock_root, tmp_path, capsys):
        lxc_dir = _make_destroy_container(tmp_path, name="wedged", mode="lxc")

        def fake_run(cmd, *args, **kwargs):
            if cmd[0] == "lxc-stop":
                raise subprocess.CalledProcessError(
                    1, cmd, stderr=b"lxc-stop: container stuck"
                )
            if cmd[0] == "mountpoint":
                return subprocess.CompletedProcess(cmd, 1)
            # podman image unmount, etc.
            return subprocess.CompletedProcess(cmd, 0)

        with patch("kento.destroy.resolve_container", return_value=lxc_dir), \
             patch("kento.destroy.subprocess.run", side_effect=fake_run), \
             patch("kento.layers.remove_image_hold"):
            destroy("wedged", force=True)

        # Directory removed even though stop failed.
        assert not lxc_dir.exists()
        captured = capsys.readouterr()
        assert "Warning: stop failed" in captured.err
        assert "proceeding with cleanup" in captured.err
        assert "Traceback" not in captured.err

    @patch("kento.destroy.require_root")
    @patch("kento.destroy.is_running", return_value=True)
    def test_pve_stop_failure_warns_and_continues(
            self, mock_running, mock_root, tmp_path, capsys):
        lxc_dir = _make_destroy_container(tmp_path, name="100", mode="pve")

        def fake_run(cmd, *args, **kwargs):
            if cmd[0] == "pct" and cmd[1] == "stop":
                raise subprocess.CalledProcessError(
                    2, cmd, stderr=b"pct: container stuck"
                )
            if cmd[0] == "mountpoint":
                return subprocess.CompletedProcess(cmd, 1)
            return subprocess.CompletedProcess(cmd, 0)

        with patch("kento.destroy.resolve_container", return_value=lxc_dir), \
             patch("kento.destroy.subprocess.run", side_effect=fake_run), \
             patch("kento.layers.remove_image_hold"), \
             patch("kento.pve.delete_pve_config"), \
             patch("kento.lxc_hook.delete_lxc_snippets_wrapper"):
            destroy("test", force=True)

        assert not lxc_dir.exists()
        captured = capsys.readouterr()
        assert "Warning: stop failed" in captured.err
        assert "Traceback" not in captured.err

    @patch("kento.destroy.require_root")
    @patch("kento.destroy.is_running", return_value=True)
    def test_pve_vm_stop_failure_warns_and_continues(
            self, mock_running, mock_root, tmp_path, capsys):
        lxc_dir = _make_destroy_container(tmp_path, name="test", mode="pve-vm")
        (lxc_dir / "kento-vmid").write_text("100\n")

        def fake_run(cmd, *args, **kwargs):
            if cmd[0] == "qm" and cmd[1] == "stop":
                raise subprocess.CalledProcessError(
                    1, cmd, stderr=b"qm: VM unresponsive"
                )
            if cmd[0] == "mountpoint":
                return subprocess.CompletedProcess(cmd, 1)
            return subprocess.CompletedProcess(cmd, 0)

        with patch("kento.destroy.resolve_container", return_value=lxc_dir), \
             patch("kento.destroy.subprocess.run", side_effect=fake_run), \
             patch("kento.layers.remove_image_hold"), \
             patch("kento.pve.delete_qm_config"), \
             patch("kento.vm_hook.delete_snippets_wrapper"):
            destroy("test", force=True)

        assert not lxc_dir.exists()
        captured = capsys.readouterr()
        assert "Warning: stop failed" in captured.err
        assert "Traceback" not in captured.err

    @patch("kento.destroy.require_root")
    @patch("kento.destroy.is_running", return_value=True)
    def test_stop_tool_not_found_warns_and_continues(
            self, mock_running, mock_root, tmp_path, capsys):
        lxc_dir = _make_destroy_container(tmp_path, name="missing", mode="lxc")

        def fake_run(cmd, *args, **kwargs):
            if cmd[0] == "lxc-stop":
                raise FileNotFoundError(2, "No such file", "lxc-stop")
            if cmd[0] == "mountpoint":
                return subprocess.CompletedProcess(cmd, 1)
            return subprocess.CompletedProcess(cmd, 0)

        with patch("kento.destroy.resolve_container", return_value=lxc_dir), \
             patch("kento.destroy.subprocess.run", side_effect=fake_run), \
             patch("kento.layers.remove_image_hold"):
            destroy("missing", force=True)

        assert not lxc_dir.exists()
        captured = capsys.readouterr()
        assert "Warning: stop tool not found" in captured.err
        assert "Traceback" not in captured.err


# --- create(--start) failure rollback path ---


class TestCreateStartRollback:
    """F8: when create(--start) hits a failing start command the caller
    sees a RuntimeError with a human-actionable message, rollback runs,
    and the container_dir is cleaned up."""

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_pve_vm_start_failure_rolls_back(self, mock_root, mock_layers,
                                              tmp_path):
        """qm start failure after --start must raise RuntimeError, rollback,
        and clean container state."""
        pve = tmp_path / "pve"
        pve.mkdir()
        import json as _json
        (pve / ".vmlist").write_text(_json.dumps({"ids": {}}))
        vm_base = tmp_path / "vm"
        vm_base.mkdir()
        state = tmp_path / "state"

        def fake_run(cmd, *args, **kwargs):
            if cmd[0] == "qm" and cmd[1] == "start":
                return subprocess.CompletedProcess(cmd, 1, stdout="",
                                                    stderr="qm: start failed")
            # stop (rollback) and podman: accept
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch("kento.create.VM_BASE", vm_base), \
             patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base",
                   side_effect=lambda n, b=None: state / n), \
             patch("kento.create.subprocess.run", side_effect=fake_run), \
             patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve.is_pve", return_value=True), \
             patch("kento.pve.write_qm_config",
                   return_value=Path("/etc/pve/qemu-server/100.conf")), \
             patch("kento.vm_hook.find_snippets_dir",
                   return_value=(tmp_path / "snippets", "local")), \
             patch("kento.vm_hook.write_snippets_wrapper",
                   return_value="local:snippets/kento-100.sh"), \
             patch("kento.vm_hook.delete_snippets_wrapper"), \
             patch("kento.pve.delete_qm_config"):
            (tmp_path / "snippets").mkdir()
            with pytest.raises(RuntimeError) as exc:
                create("myimage:latest", name="pvefail", mode="vm",
                       pve=True, vmid=100, start=True)
            msg = str(exc.value)
            assert "failed to start pvefail" in msg
            assert "kento vm start pvefail" in msg or "kento vm destroy pvefail" in msg

        # Container dir got rolled back.
        assert not (vm_base / "pvefail").exists()

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_pve_lxc_start_failure_rolls_back(self, mock_root, mock_layers,
                                               tmp_path):
        """pct start failure after --start must raise RuntimeError, rollback."""
        pve = tmp_path / "pve"
        pve.mkdir()
        import json as _json
        (pve / ".vmlist").write_text(_json.dumps({"ids": {}}))

        def fake_run(cmd, *args, **kwargs):
            if cmd[0] == "pct" and cmd[1] == "start":
                return subprocess.CompletedProcess(cmd, 1, stdout="",
                                                    stderr="pct: start failed")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "100"), \
             patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve.write_pve_config",
                   return_value=Path("/etc/pve/lxc/100.conf")), \
             patch("kento.create.subprocess.run", side_effect=fake_run):
            with pytest.raises(RuntimeError) as exc:
                create("myimage:latest", name="lxcfail", mode="pve",
                       vmid=100, start=True)
            msg = str(exc.value)
            assert "failed to start lxcfail" in msg
            assert "kento lxc start lxcfail" in msg or "kento lxc destroy lxcfail" in msg

        assert not (tmp_path / "100").exists()


# --- ssh-keygen failure path (F8, site 5 on create.py line 182) ---


class TestSshKeygenFailure:
    """ssh-keygen failing (e.g. bad -t type, write-permission, bad flags)
    must print a kento-branded error, not a CalledProcessError traceback."""

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_ssh_keygen_nonzero_exit(self, mock_root, mock_layers,
                                      tmp_path, capsys):
        def fake_run(cmd, *args, **kwargs):
            if cmd[0] == "ssh-keygen":
                raise subprocess.CalledProcessError(
                    1, cmd, stderr=b"ssh-keygen: unable to write key")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"), \
             patch("kento.create.subprocess.run", side_effect=fake_run):
            with pytest.raises(SystemExit) as exc:
                create("myimage:latest", name="test", mode="lxc",
                       unconfined=True, ssh_host_keys=True)
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "Error: ssh-keygen failed" in captured.err
        assert "unable to write key" in captured.err
        assert "Traceback" not in captured.err
