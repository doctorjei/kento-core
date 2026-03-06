"""Tests for VM mode support."""

import signal
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from kento.vm import VM_BASE, allocate_port, is_vm_running, mount_rootfs, unmount_rootfs, start_vm, stop_vm


# --- allocate_port ---


class TestAllocatePort:
    def test_empty_dir(self, tmp_path):
        assert allocate_port(tmp_path) == 10022

    def test_skips_used_ports(self, tmp_path):
        d = tmp_path / "vm1"
        d.mkdir()
        (d / "kento-port").write_text("10022:22\n")
        assert allocate_port(tmp_path) == 10023

    def test_fills_gaps(self, tmp_path):
        d1 = tmp_path / "vm1"
        d1.mkdir()
        (d1 / "kento-port").write_text("10022:22\n")
        d2 = tmp_path / "vm2"
        d2.mkdir()
        (d2 / "kento-port").write_text("10024:22\n")
        assert allocate_port(tmp_path) == 10023

    def test_multiple_consecutive(self, tmp_path):
        for i in range(3):
            d = tmp_path / f"vm{i}"
            d.mkdir()
            (d / "kento-port").write_text(f"{10022 + i}:22\n")
        assert allocate_port(tmp_path) == 10025

    def test_nonexistent_dir(self, tmp_path):
        assert allocate_port(tmp_path / "nope") == 10022

    def test_ignores_malformed_port(self, tmp_path):
        d = tmp_path / "vm1"
        d.mkdir()
        (d / "kento-port").write_text("bad\n")
        assert allocate_port(tmp_path) == 10022

    def test_custom_guest_port(self, tmp_path):
        d = tmp_path / "vm1"
        d.mkdir()
        (d / "kento-port").write_text("10022:2222\n")
        assert allocate_port(tmp_path) == 10023


# --- is_vm_running ---


class TestIsVmRunning:
    def test_no_pid_file(self, tmp_path):
        assert is_vm_running(tmp_path) is False

    def test_pid_file_with_running_process(self, tmp_path):
        (tmp_path / "kento-qemu-pid").write_text("1\n")  # PID 1 always exists
        assert is_vm_running(tmp_path) is True

    def test_pid_file_with_dead_process(self, tmp_path):
        (tmp_path / "kento-qemu-pid").write_text("999999999\n")
        assert is_vm_running(tmp_path) is False

    def test_malformed_pid_file(self, tmp_path):
        (tmp_path / "kento-qemu-pid").write_text("notapid\n")
        assert is_vm_running(tmp_path) is False


# --- mount_rootfs ---


class TestMountRootfs:
    @patch("kento.vm.subprocess.run")
    def test_mount_command(self, mock_run, tmp_path):
        lxc_dir = tmp_path / "vm1"
        lxc_dir.mkdir()
        (lxc_dir / "rootfs").mkdir()
        state_dir = lxc_dir

        mount_rootfs(lxc_dir, "/a:/b", state_dir)

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args == [
            "mount", "-t", "overlay", "overlay", "-o",
            f"lowerdir=/a:/b,upperdir={state_dir}/upper,workdir={state_dir}/work",
            str(lxc_dir / "rootfs"),
        ]
        env = mock_run.call_args[1]["env"]
        assert env["LIBMOUNT_FORCE_MOUNT2"] == "always"
        assert mock_run.call_args[1]["check"] is True


# --- unmount_rootfs ---


class TestUnmountRootfs:
    @patch("kento.vm.subprocess.run")
    def test_unmount_command(self, mock_run, tmp_path):
        unmount_rootfs(tmp_path)
        mock_run.assert_called_once_with(
            ["umount", str(tmp_path / "rootfs")], check=True,
        )


# --- start_vm ---


class TestStartVm:
    @patch("kento.vm.subprocess.Popen")
    @patch("kento.vm.mount_rootfs")
    def test_start_lifecycle(self, mock_mount, mock_popen, tmp_path):
        lxc_dir = tmp_path / "testvm"
        lxc_dir.mkdir()
        rootfs = lxc_dir / "rootfs"
        rootfs.mkdir()
        boot = rootfs / "boot"
        boot.mkdir()
        (boot / "vmlinuz").write_text("kernel")
        (boot / "initramfs.img").write_text("initramfs")
        (lxc_dir / "kento-layers").write_text("/a:/b\n")
        (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
        (lxc_dir / "kento-port").write_text("10022:22\n")
        # Pre-create socket so wait loop exits immediately
        (lxc_dir / "virtiofsd.sock").write_text("")

        mock_vfs = MagicMock()
        mock_vfs.pid = 1001
        mock_qemu = MagicMock()
        mock_qemu.pid = 1002
        mock_popen.side_effect = [mock_vfs, mock_qemu]

        start_vm(lxc_dir, "testvm")

        mock_mount.assert_called_once()
        assert mock_popen.call_count == 2
        assert (lxc_dir / "kento-virtiofsd-pid").read_text().strip() == "1001"
        assert (lxc_dir / "kento-qemu-pid").read_text().strip() == "1002"

    @patch("kento.vm.subprocess.Popen")
    @patch("kento.vm.mount_rootfs")
    def test_start_passes_port(self, mock_mount, mock_popen, tmp_path):
        lxc_dir = tmp_path / "testvm"
        lxc_dir.mkdir()
        rootfs = lxc_dir / "rootfs"
        rootfs.mkdir()
        boot = rootfs / "boot"
        boot.mkdir()
        (boot / "vmlinuz").write_text("kernel")
        (boot / "initramfs.img").write_text("initramfs")
        (lxc_dir / "kento-layers").write_text("/a:/b\n")
        (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
        (lxc_dir / "kento-port").write_text("12345:2222\n")
        (lxc_dir / "virtiofsd.sock").write_text("")

        mock_vfs = MagicMock()
        mock_vfs.pid = 100
        mock_qemu = MagicMock()
        mock_qemu.pid = 200
        mock_popen.side_effect = [mock_vfs, mock_qemu]

        start_vm(lxc_dir, "testvm")

        # QEMU is the second Popen call
        qemu_args = mock_popen.call_args_list[1][0][0]
        assert "hostfwd=tcp:127.0.0.1:12345-:2222" in " ".join(qemu_args)

    @patch("kento.vm.unmount_rootfs")
    @patch("kento.vm.mount_rootfs")
    def test_start_missing_kernel(self, mock_mount, mock_unmount, tmp_path):
        lxc_dir = tmp_path / "testvm"
        lxc_dir.mkdir()
        rootfs = lxc_dir / "rootfs"
        rootfs.mkdir()
        (rootfs / "boot").mkdir()
        # No vmlinuz
        (lxc_dir / "kento-layers").write_text("/a:/b\n")
        (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")

        with pytest.raises(SystemExit):
            start_vm(lxc_dir, "testvm")
        mock_unmount.assert_called_once()

    @patch("kento.vm.unmount_rootfs")
    @patch("kento.vm.mount_rootfs")
    def test_start_missing_initramfs(self, mock_mount, mock_unmount, tmp_path):
        lxc_dir = tmp_path / "testvm"
        lxc_dir.mkdir()
        rootfs = lxc_dir / "rootfs"
        rootfs.mkdir()
        boot = rootfs / "boot"
        boot.mkdir()
        (boot / "vmlinuz").write_text("kernel")
        # No initramfs.img
        (lxc_dir / "kento-layers").write_text("/a:/b\n")
        (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")

        with pytest.raises(SystemExit):
            start_vm(lxc_dir, "testvm")
        mock_unmount.assert_called_once()


# --- stop_vm ---


class TestStopVm:
    @patch("kento.vm.subprocess.run")
    def test_stop_kills_processes_and_cleans_up(self, mock_run, tmp_path):
        (tmp_path / "kento-qemu-pid").write_text("1001\n")
        (tmp_path / "kento-virtiofsd-pid").write_text("1002\n")
        (tmp_path / "virtiofsd.sock").write_text("")
        # mountpoint check returns 1 (not mounted)
        mock_run.return_value = subprocess.CompletedProcess([], 1)

        with patch("kento.vm.os.kill") as mock_kill:
            stop_vm(tmp_path)

        mock_kill.assert_any_call(1001, signal.SIGTERM)
        mock_kill.assert_any_call(1002, signal.SIGTERM)
        assert not (tmp_path / "kento-qemu-pid").exists()
        assert not (tmp_path / "kento-virtiofsd-pid").exists()
        assert not (tmp_path / "virtiofsd.sock").exists()

    @patch("kento.vm.subprocess.run")
    def test_stop_unmounts_if_mounted(self, mock_run, tmp_path):
        (tmp_path / "rootfs").mkdir()
        # mountpoint check returns 0 (mounted)
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="")

        with patch("kento.vm.os.kill"):
            stop_vm(tmp_path)

        # Should have called mountpoint and then umount
        umount_calls = [c for c in mock_run.call_args_list if "umount" in c[0][0]]
        assert len(umount_calls) == 1

    @patch("kento.vm.subprocess.run")
    def test_stop_handles_no_pid_files(self, mock_run, tmp_path):
        # No PID files, no socket — should not raise
        mock_run.return_value = subprocess.CompletedProcess([], 1)
        stop_vm(tmp_path)

    @patch("kento.vm.subprocess.run")
    def test_stop_handles_dead_process(self, mock_run, tmp_path):
        (tmp_path / "kento-qemu-pid").write_text("999999999\n")
        mock_run.return_value = subprocess.CompletedProcess([], 1)

        with patch("kento.vm.os.kill", side_effect=ProcessLookupError):
            stop_vm(tmp_path)

        assert not (tmp_path / "kento-qemu-pid").exists()
