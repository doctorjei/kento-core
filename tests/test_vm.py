"""Tests for VM mode support."""

import signal
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from kento.vm import (
    VM_BASE, _PORT_MIN, _PORT_MAX, _port_is_free,
    allocate_port, is_vm_running, mount_rootfs, unmount_rootfs, start_vm, stop_vm,
)


# --- allocate_port ---


class TestAllocatePort:
    @patch("kento.vm._port_is_free", return_value=True)
    def test_empty_dir(self, mock_free, tmp_path):
        assert allocate_port(tmp_path) == 10022

    @patch("kento.vm._port_is_free", return_value=True)
    def test_skips_used_ports(self, mock_free, tmp_path):
        d = tmp_path / "vm1"
        d.mkdir()
        (d / "kento-port").write_text("10022:22\n")
        assert allocate_port(tmp_path) == 10023

    @patch("kento.vm._port_is_free", return_value=True)
    def test_fills_gaps(self, mock_free, tmp_path):
        d1 = tmp_path / "vm1"
        d1.mkdir()
        (d1 / "kento-port").write_text("10022:22\n")
        d2 = tmp_path / "vm2"
        d2.mkdir()
        (d2 / "kento-port").write_text("10024:22\n")
        assert allocate_port(tmp_path) == 10023

    @patch("kento.vm._port_is_free", return_value=True)
    def test_multiple_consecutive(self, mock_free, tmp_path):
        for i in range(3):
            d = tmp_path / f"vm{i}"
            d.mkdir()
            (d / "kento-port").write_text(f"{10022 + i}:22\n")
        assert allocate_port(tmp_path) == 10025

    @patch("kento.vm._port_is_free", return_value=True)
    def test_nonexistent_dir(self, mock_free, tmp_path):
        assert allocate_port(tmp_path / "nope") == 10022

    @patch("kento.vm._port_is_free", return_value=True)
    def test_ignores_malformed_port(self, mock_free, tmp_path):
        d = tmp_path / "vm1"
        d.mkdir()
        (d / "kento-port").write_text("bad\n")
        assert allocate_port(tmp_path) == 10022

    @patch("kento.vm._port_is_free", return_value=True)
    def test_custom_guest_port(self, mock_free, tmp_path):
        d = tmp_path / "vm1"
        d.mkdir()
        (d / "kento-port").write_text("10022:2222\n")
        assert allocate_port(tmp_path) == 10023

    @patch("kento.vm._port_is_free", side_effect=lambda p: p != 10022)
    def test_skips_port_in_use_on_host(self, mock_free, tmp_path):
        """Port 10022 not in kento-port files but bound on host — skip it."""
        assert allocate_port(tmp_path) == 10023

    @patch("kento.vm._port_is_free", return_value=False)
    def test_all_ports_exhausted(self, mock_free, tmp_path):
        """Every port in range is busy — should exit with error."""
        with pytest.raises(SystemExit):
            allocate_port(tmp_path)

    def test_port_range_constants(self):
        assert _PORT_MIN == 10022
        assert _PORT_MAX == 10999


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
    @patch("kento.vm._is_mountpoint", return_value=False)
    @patch("kento.vm.subprocess.run")
    def test_mount_command(self, mock_run, mock_mp, tmp_path):
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

    @patch("kento.vm._is_mountpoint", return_value=True)
    def test_mount_rejects_already_mounted(self, mock_mp, tmp_path):
        lxc_dir = tmp_path / "vm1"
        lxc_dir.mkdir()
        (lxc_dir / "rootfs").mkdir()
        with pytest.raises(SystemExit):
            mount_rootfs(lxc_dir, "/a:/b", lxc_dir)


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
    @patch("kento.vm._find_virtiofsd", return_value="/usr/libexec/virtiofsd")
    @patch("kento.vm.is_vm_running", return_value=False)
    @patch("kento.vm.subprocess.Popen")
    @patch("kento.vm.mount_rootfs")
    def test_start_lifecycle(self, mock_mount, mock_popen, mock_running, mock_find, tmp_path):
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

    @patch("kento.vm._find_virtiofsd", return_value="/usr/libexec/virtiofsd")
    @patch("kento.vm.is_vm_running", return_value=False)
    @patch("kento.vm.subprocess.Popen")
    @patch("kento.vm.mount_rootfs")
    def test_start_passes_port(self, mock_mount, mock_popen, mock_running, mock_find, tmp_path):
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

    @patch("kento.vm.is_vm_running", return_value=False)
    @patch("kento.vm.unmount_rootfs")
    @patch("kento.vm.mount_rootfs")
    def test_start_missing_kernel(self, mock_mount, mock_unmount, mock_running, tmp_path):
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

    @patch("kento.vm._find_virtiofsd", return_value="/usr/libexec/virtiofsd")
    @patch("kento.vm.is_vm_running", return_value=False)
    @patch("kento.vm.subprocess.Popen")
    @patch("kento.vm.mount_rootfs")
    def test_start_virtiofsd_args(self, mock_mount, mock_popen, mock_running, mock_find, tmp_path):
        """Validate virtiofsd command-line arguments."""
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
        (lxc_dir / "virtiofsd.sock").write_text("")

        mock_vfs = MagicMock()
        mock_vfs.pid = 1001
        mock_qemu = MagicMock()
        mock_qemu.pid = 1002
        mock_popen.side_effect = [mock_vfs, mock_qemu]

        start_vm(lxc_dir, "testvm")

        vfs_call = mock_popen.call_args_list[0]
        vfs_args = vfs_call[0][0]
        assert vfs_args[0] == "/usr/libexec/virtiofsd"
        assert f"--socket-path={lxc_dir / 'virtiofsd.sock'}" in vfs_args
        assert f"--shared-dir={rootfs}" in vfs_args
        assert "--cache=auto" in vfs_args
        assert vfs_call[1]["stdout"] == subprocess.DEVNULL
        assert vfs_call[1]["stderr"] == subprocess.DEVNULL

    @patch("kento.vm.VM_KVM", True)
    @patch("kento.vm.VM_MACHINE", "q35")
    @patch("kento.vm.VM_MEMORY", 512)
    @patch("kento.vm._find_virtiofsd", return_value="/usr/libexec/virtiofsd")
    @patch("kento.vm.is_vm_running", return_value=False)
    @patch("kento.vm.subprocess.Popen")
    @patch("kento.vm.mount_rootfs")
    def test_start_qemu_args(self, mock_mount, mock_popen, mock_running, mock_find, tmp_path):
        """Validate QEMU command-line arguments that are critical for boot."""
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
        (lxc_dir / "virtiofsd.sock").write_text("")

        mock_vfs = MagicMock()
        mock_vfs.pid = 1001
        mock_qemu = MagicMock()
        mock_qemu.pid = 1002
        mock_popen.side_effect = [mock_vfs, mock_qemu]

        start_vm(lxc_dir, "testvm")

        qemu_call = mock_popen.call_args_list[1]
        qemu_args = qemu_call[0][0]

        # Executable
        assert qemu_args[0] == "qemu-system-x86_64"

        # Kernel and initrd paths
        kernel_idx = qemu_args.index("-kernel")
        assert qemu_args[kernel_idx + 1] == str(rootfs / "boot" / "vmlinuz")
        initrd_idx = qemu_args.index("-initrd")
        assert qemu_args[initrd_idx + 1] == str(rootfs / "boot" / "initramfs.img")

        # Memory
        m_idx = qemu_args.index("-m")
        assert qemu_args[m_idx + 1] == "512"

        # Machine type
        machine_idx = qemu_args.index("-machine")
        assert qemu_args[machine_idx + 1] == "q35"

        # KVM enabled
        assert "-enable-kvm" in qemu_args
        cpu_idx = qemu_args.index("-cpu")
        assert qemu_args[cpu_idx + 1] == "host"

        # Nographic
        assert "-nographic" in qemu_args

        # virtiofs chardev and device
        socket_path = str(lxc_dir / "virtiofsd.sock")
        chardev_idx = qemu_args.index("-chardev")
        assert qemu_args[chardev_idx + 1] == f"socket,id=vfs,path={socket_path}"
        device_idx = qemu_args.index("-device")
        assert qemu_args[device_idx + 1] == "vhost-user-fs-pci,chardev=vfs,tag=rootfs"

        # Memory backend for virtiofs (memfd with share=on)
        obj_idx = qemu_args.index("-object")
        assert qemu_args[obj_idx + 1] == "memory-backend-memfd,id=mem,size=512M,share=on"

        # NUMA node
        numa_idx = qemu_args.index("-numa")
        assert qemu_args[numa_idx + 1] == "node,memdev=mem"

        # Network device (port mapping present)
        netdev_idx = qemu_args.index("-netdev")
        assert qemu_args[netdev_idx + 1] == "user,id=net0,hostfwd=tcp:127.0.0.1:10022-:22"

        # Kernel command line
        append_idx = qemu_args.index("-append")
        assert qemu_args[append_idx + 1] == "console=ttyS0 rootfstype=virtiofs root=rootfs"

        # QEMU stdout/stderr suppressed
        assert qemu_call[1]["stdout"] == subprocess.DEVNULL
        assert qemu_call[1]["stderr"] == subprocess.DEVNULL

    @patch("kento.vm.VM_KVM", False)
    @patch("kento.vm._find_virtiofsd", return_value="/usr/libexec/virtiofsd")
    @patch("kento.vm.is_vm_running", return_value=False)
    @patch("kento.vm.subprocess.Popen")
    @patch("kento.vm.mount_rootfs")
    def test_start_qemu_no_kvm(self, mock_mount, mock_popen, mock_running, mock_find, tmp_path):
        """When VM_KVM is False, -enable-kvm and -cpu host must be absent."""
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
        (lxc_dir / "virtiofsd.sock").write_text("")
        # No kento-port file — no network args

        mock_vfs = MagicMock()
        mock_vfs.pid = 1001
        mock_qemu = MagicMock()
        mock_qemu.pid = 1002
        mock_popen.side_effect = [mock_vfs, mock_qemu]

        start_vm(lxc_dir, "testvm")

        qemu_args = mock_popen.call_args_list[1][0][0]
        assert "-enable-kvm" not in qemu_args
        assert "-cpu" not in qemu_args
        # Without kento-port, no network args
        assert "-netdev" not in qemu_args

    @patch("kento.vm.is_vm_running", return_value=True)
    def test_start_rejects_running(self, mock_running, tmp_path):
        lxc_dir = tmp_path / "testvm"
        lxc_dir.mkdir()
        with pytest.raises(SystemExit):
            start_vm(lxc_dir, "testvm")

    @patch("kento.vm.is_vm_running", return_value=False)
    @patch("kento.vm.unmount_rootfs")
    @patch("kento.vm.mount_rootfs")
    def test_start_missing_initramfs(self, mock_mount, mock_unmount, mock_running, tmp_path):
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
    @patch("kento.vm._is_mountpoint", return_value=False)
    @patch("kento.vm._kill_and_wait")
    def test_stop_kills_processes_and_cleans_up(self, mock_kw, mock_mp, tmp_path):
        (tmp_path / "virtiofsd.sock").write_text("")

        stop_vm(tmp_path)

        assert mock_kw.call_count == 2
        mock_kw.assert_any_call(tmp_path / "kento-qemu-pid", force=False)
        mock_kw.assert_any_call(tmp_path / "kento-virtiofsd-pid", force=False)
        assert not (tmp_path / "virtiofsd.sock").exists()

    @patch("kento.vm.subprocess.run", return_value=subprocess.CompletedProcess([], 0))
    @patch("kento.vm._is_mountpoint", return_value=True)
    @patch("kento.vm._kill_and_wait")
    def test_stop_unmounts_if_mounted(self, mock_kw, mock_mp, mock_run, tmp_path):
        (tmp_path / "rootfs").mkdir()

        stop_vm(tmp_path)

        mock_run.assert_called_once_with(["umount", str(tmp_path / "rootfs")])

    @patch("kento.vm._is_mountpoint", return_value=False)
    @patch("kento.vm._kill_and_wait")
    def test_stop_handles_no_pid_files(self, mock_kw, mock_mp, tmp_path):
        stop_vm(tmp_path)

    @patch("kento.vm._is_mountpoint", return_value=False)
    def test_stop_handles_dead_process(self, mock_mp, tmp_path):
        (tmp_path / "kento-qemu-pid").write_text("999999999\n")

        with patch("kento.vm.os.kill", side_effect=ProcessLookupError):
            stop_vm(tmp_path)

        assert not (tmp_path / "kento-qemu-pid").exists()


class TestKillAndWait:
    def test_no_pid_file(self, tmp_path):
        from kento.vm import _kill_and_wait
        _kill_and_wait(tmp_path / "nonexistent")  # should not raise

    def test_malformed_pid_file(self, tmp_path):
        from kento.vm import _kill_and_wait
        pid_file = tmp_path / "pid"
        pid_file.write_text("notanumber\n")
        _kill_and_wait(pid_file)
        assert not pid_file.exists()

    @patch("kento.vm.os.kill", side_effect=ProcessLookupError)
    def test_dead_process(self, mock_kill, tmp_path):
        from kento.vm import _kill_and_wait
        pid_file = tmp_path / "pid"
        pid_file.write_text("999999999\n")
        _kill_and_wait(pid_file)
        mock_kill.assert_called_once_with(999999999, signal.SIGTERM)
        assert not pid_file.exists()

    @patch("kento.vm.Path")
    @patch("kento.vm.os.kill")
    def test_waits_for_exit(self, mock_kill, mock_path_cls, tmp_path):
        from kento.vm import _kill_and_wait
        pid_file = tmp_path / "pid"
        pid_file.write_text("1234\n")

        # /proc/1234 exists on first check, gone on second
        proc_path = MagicMock()
        proc_path.is_dir.side_effect = [True, False]
        original_path = Path

        def path_factory(p):
            if p == "/proc/1234":
                return proc_path
            return original_path(p)

        mock_path_cls.side_effect = path_factory

        with patch("kento.vm.time.sleep"):
            _kill_and_wait(pid_file)

        mock_kill.assert_called_once_with(1234, signal.SIGTERM)


class TestIsMountpoint:
    @patch("kento.vm.subprocess.run")
    def test_returns_true(self, mock_run):
        from kento.vm import _is_mountpoint
        mock_run.return_value = subprocess.CompletedProcess([], 0)
        assert _is_mountpoint(Path("/some/path")) is True

    @patch("kento.vm.subprocess.run")
    def test_returns_false(self, mock_run):
        from kento.vm import _is_mountpoint
        mock_run.return_value = subprocess.CompletedProcess([], 1)
        assert _is_mountpoint(Path("/some/path")) is False
