"""Tests for container destruction."""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from kento.destroy import destroy


def _make_container(tmp_path, name="test", state_dir=None, mode="lxc"):
    """Create a minimal container directory for testing."""
    lxc_dir = tmp_path / name
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-mode").write_text(mode + "\n")
    (lxc_dir / "kento-name").write_text(name + "\n")
    (lxc_dir / "rootfs").mkdir()
    sd = state_dir or lxc_dir
    (lxc_dir / "kento-state").write_text(str(sd) + "\n")
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "upper").mkdir(exist_ok=True)
    (sd / "work").mkdir(exist_ok=True)
    return lxc_dir


def _mock_run_stopped(args, **kwargs):
    result = subprocess.CompletedProcess(args, 0)
    if "lxc-info" in args:
        result.stdout = "STOPPED"
    elif "mountpoint" in args:
        result.returncode = 1
    return result


def _mock_run_running(args, **kwargs):
    result = subprocess.CompletedProcess(args, 0)
    if "lxc-info" in args:
        result.stdout = "RUNNING"
    elif "mountpoint" in args:
        result.returncode = 1
    return result


@patch("kento.destroy.subprocess.run", side_effect=_mock_run_stopped)
@patch("kento.destroy.require_root")
def test_destroy_removes_directory(mock_root, mock_run, tmp_path):
    lxc_dir = _make_container(tmp_path)

    with patch("kento.destroy.resolve_container", return_value=lxc_dir):
        destroy("test")

    assert not lxc_dir.exists()


@patch("kento.destroy.subprocess.run", side_effect=_mock_run_running)
@patch("kento.destroy.require_root")
def test_destroy_stops_running_container(mock_root, mock_run, tmp_path):
    _make_container(tmp_path)

    with patch("kento.destroy.resolve_container", return_value=tmp_path / "test"):
        destroy("test", force=True)

    stop_calls = [c for c in mock_run.call_args_list if "lxc-stop" in c[0][0]]
    assert len(stop_calls) == 1


@patch("kento.destroy.subprocess.run", side_effect=_mock_run_stopped)
@patch("kento.destroy.require_root")
def test_destroy_removes_separate_state_dir(mock_root, mock_run, tmp_path):
    state = tmp_path / "user-state" / "test"
    lxc_dir = _make_container(tmp_path, state_dir=state)

    with patch("kento.destroy.resolve_container", return_value=lxc_dir):
        destroy("test")

    assert not lxc_dir.exists()
    assert not state.exists()


@patch("kento.destroy.require_root")
def test_destroy_nonexistent(mock_root, tmp_path):
    with patch("kento.destroy.resolve_container",
               side_effect=SystemExit(1)):
        with pytest.raises(SystemExit):
            destroy("nonexistent")


# --- PVE mode tests ---


def _mock_pve_run_stopped(args, **kwargs):
    result = subprocess.CompletedProcess(args, 0)
    if "pct" in args and "status" in args:
        result.stdout = "status: stopped"
    elif "mountpoint" in args:
        result.returncode = 1
    return result


def _mock_pve_run_running(args, **kwargs):
    result = subprocess.CompletedProcess(args, 0)
    if "pct" in args and "status" in args:
        result.stdout = "status: running"
    elif "mountpoint" in args:
        result.returncode = 1
    return result


@patch("kento.destroy.subprocess.run", side_effect=_mock_pve_run_stopped)
@patch("kento.destroy.require_root")
def test_destroy_pve_removes_directory(mock_root, mock_run, tmp_path):
    lxc_dir = _make_container(tmp_path, name="100", mode="pve")

    with patch("kento.destroy.resolve_container", return_value=lxc_dir), \
         patch("kento.pve.PVE_DIR", tmp_path / "pve"), \
         patch("kento.pve.socket.gethostname", return_value="node1"):
        destroy("mybox")

    assert not lxc_dir.exists()


@patch("kento.destroy.subprocess.run", side_effect=_mock_pve_run_running)
@patch("kento.destroy.require_root")
def test_destroy_pve_stops_running_container(mock_root, mock_run, tmp_path):
    _make_container(tmp_path, name="100", mode="pve")

    with patch("kento.destroy.resolve_container", return_value=tmp_path / "100"), \
         patch("kento.pve.PVE_DIR", tmp_path / "pve"), \
         patch("kento.pve.socket.gethostname", return_value="node1"):
        destroy("mybox", force=True)

    stop_calls = [c for c in mock_run.call_args_list
                  if "pct" in c[0][0] and "stop" in c[0][0]]
    assert len(stop_calls) == 1


@patch("kento.destroy.subprocess.run", side_effect=_mock_pve_run_stopped)
@patch("kento.destroy.require_root")
def test_destroy_pve_deletes_pve_config(mock_root, mock_run, tmp_path):
    _make_container(tmp_path, name="100", mode="pve")
    # Create a fake PVE config
    pve = tmp_path / "pve"
    conf_dir = pve / "nodes" / "node1" / "lxc"
    conf_dir.mkdir(parents=True)
    conf = conf_dir / "100.conf"
    conf.write_text("arch: amd64\n")

    with patch("kento.destroy.resolve_container", return_value=tmp_path / "100"), \
         patch("kento.pve.PVE_DIR", pve), \
         patch("kento.pve.socket.gethostname", return_value="node1"):
        destroy("mybox")

    assert not conf.exists()


# --- VM mode tests ---


def _mock_vm_run_not_mounted(args, **kwargs):
    result = subprocess.CompletedProcess(args, 0)
    if "mountpoint" in args:
        result.returncode = 1
    return result


@patch("kento.destroy.subprocess.run", side_effect=_mock_vm_run_not_mounted)
@patch("kento.destroy.require_root")
def test_destroy_vm_removes_directory(mock_root, mock_run, tmp_path):
    lxc_dir = _make_container(tmp_path, name="testvm", mode="vm")

    with patch("kento.destroy.resolve_container", return_value=lxc_dir), \
         patch("kento.vm.is_vm_running", return_value=False):
        destroy("testvm")

    assert not lxc_dir.exists()


@patch("kento.destroy.subprocess.run", side_effect=_mock_vm_run_not_mounted)
@patch("kento.destroy.require_root")
def test_destroy_vm_stops_running(mock_root, mock_run, tmp_path):
    lxc_dir = _make_container(tmp_path, name="testvm", mode="vm")

    with patch("kento.destroy.resolve_container", return_value=lxc_dir), \
         patch("kento.vm.is_vm_running", return_value=True), \
         patch("kento.vm.stop_vm") as mock_stop:
        destroy("testvm", force=True)

    mock_stop.assert_called_once_with(lxc_dir)
    assert not lxc_dir.exists()


@patch("kento.destroy.subprocess.run", side_effect=_mock_vm_run_not_mounted)
@patch("kento.destroy.require_root")
def test_destroy_vm_no_podman_unmount(mock_root, mock_run, tmp_path):
    lxc_dir = _make_container(tmp_path, name="testvm", mode="vm")

    with patch("kento.destroy.resolve_container", return_value=lxc_dir), \
         patch("kento.vm.is_vm_running", return_value=False):
        destroy("testvm")

    # No podman image unmount calls for VM mode
    podman_calls = [c for c in mock_run.call_args_list
                    if c[0][0][0] in ("podman", "runuser")]
    assert len(podman_calls) == 0


# --- PVE-VM mode tests ---


def _mock_pvevm_run_not_mounted(args, **kwargs):
    result = subprocess.CompletedProcess(args, 0)
    if "mountpoint" in args:
        result.returncode = 1
    return result


class TestDestroyPveVm:
    @patch("kento.destroy.subprocess.run", side_effect=_mock_pvevm_run_not_mounted)
    @patch("kento.destroy.is_running", return_value=False)
    @patch("kento.destroy.require_root")
    def test_destroy_cleans_up_qm_config(self, mock_root, mock_running, mock_run, tmp_path):
        d = tmp_path / "test"
        d.mkdir()
        (d / "rootfs").mkdir()
        (d / "kento-image").write_text("myimage\n")
        (d / "kento-mode").write_text("pve-vm\n")
        (d / "kento-name").write_text("test\n")
        (d / "kento-state").write_text(str(d) + "\n")
        (d / "kento-vmid").write_text("100\n")

        with patch("kento.destroy.resolve_container", return_value=d), \
             patch("kento.pve.delete_qm_config") as mock_delete_qm, \
             patch("kento.vm_hook.delete_snippets_wrapper") as mock_delete_snippets:
            destroy("test")

        mock_delete_qm.assert_called_once_with(100)
        mock_delete_snippets.assert_called_once_with(100)
        assert not d.exists()

    @patch("kento.destroy.subprocess.run", side_effect=_mock_pvevm_run_not_mounted)
    @patch("kento.destroy.is_running", return_value=True)
    @patch("kento.destroy.require_root")
    def test_force_destroy_stops_via_qm(self, mock_root, mock_running, mock_run, tmp_path):
        d = tmp_path / "test"
        d.mkdir()
        (d / "rootfs").mkdir()
        (d / "kento-image").write_text("myimage\n")
        (d / "kento-mode").write_text("pve-vm\n")
        (d / "kento-name").write_text("test\n")
        (d / "kento-state").write_text(str(d) + "\n")
        (d / "kento-vmid").write_text("100\n")

        with patch("kento.destroy.resolve_container", return_value=d), \
             patch("kento.pve.delete_qm_config"), \
             patch("kento.vm_hook.delete_snippets_wrapper"):
            destroy("test", force=True)

        # Check qm stop was called
        calls = [c[0][0] for c in mock_run.call_args_list]
        assert ["qm", "stop", "100"] in calls

    @patch("kento.destroy.subprocess.run", side_effect=_mock_pvevm_run_not_mounted)
    @patch("kento.destroy.is_running", return_value=False)
    @patch("kento.destroy.require_root")
    def test_destroy_no_podman_unmount(self, mock_root, mock_running, mock_run, tmp_path):
        d = tmp_path / "test"
        d.mkdir()
        (d / "rootfs").mkdir()
        (d / "kento-image").write_text("myimage\n")
        (d / "kento-mode").write_text("pve-vm\n")
        (d / "kento-name").write_text("test\n")
        (d / "kento-state").write_text(str(d) + "\n")
        (d / "kento-vmid").write_text("100\n")

        with patch("kento.destroy.resolve_container", return_value=d), \
             patch("kento.pve.delete_qm_config"), \
             patch("kento.vm_hook.delete_snippets_wrapper"):
            destroy("test")

        # No podman image unmount calls for pve-vm mode
        podman_calls = [c for c in mock_run.call_args_list
                        if c[0][0][0] in ("podman", "runuser")]
        assert len(podman_calls) == 0
