"""Tests for VM mode support."""

import logging
import signal
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from kento.errors import StateError, SubprocessError
from kento.vm import (
    VM_BASE, _PORT_MIN, _PORT_MAX, _port_is_free,
    allocate_port, is_vm_running, mount_rootfs, unmount_rootfs, start_vm, stop_vm,
    generate_mac, is_valid_mac, MAC_PREFIX,
)


@pytest.fixture(autouse=True)
def _stub_find_qemu():
    """Stub the qemu binary lookup so start_vm tests run on hosts without QEMU.

    start_vm resolves qemu-system-x86_64 up-front (sys.exit on miss); the test
    host has no QEMU installed. Tests that need to drive the binary-absent path
    patch _find_qemu themselves, overriding this default.
    """
    with patch("kento.vm._find_qemu", return_value="/usr/bin/qemu-system-x86_64"):
        yield


# --- allocate_port ---


class TestAllocatePort:
    @patch("kento.vm._port_is_free", return_value=True)
    def test_empty_dir(self, mock_free, tmp_path):
        vm_base = tmp_path / "vm"
        vm_base.mkdir()
        lxc_base = tmp_path / "lxc"
        lxc_base.mkdir()
        with patch("kento.vm.VM_BASE", vm_base), \
             patch("kento.LXC_BASE", lxc_base):
            assert allocate_port() == 10022

    @patch("kento.vm._port_is_free", return_value=True)
    def test_skips_used_ports(self, mock_free, tmp_path):
        vm_base = tmp_path / "vm"
        vm_base.mkdir()
        lxc_base = tmp_path / "lxc"
        lxc_base.mkdir()
        d = vm_base / "vm1"
        d.mkdir()
        (d / "kento-port").write_text("10022:22\n")
        with patch("kento.vm.VM_BASE", vm_base), \
             patch("kento.LXC_BASE", lxc_base):
            assert allocate_port() == 10023

    @patch("kento.vm._port_is_free", return_value=True)
    def test_fills_gaps(self, mock_free, tmp_path):
        vm_base = tmp_path / "vm"
        vm_base.mkdir()
        lxc_base = tmp_path / "lxc"
        lxc_base.mkdir()
        d1 = vm_base / "vm1"
        d1.mkdir()
        (d1 / "kento-port").write_text("10022:22\n")
        d2 = vm_base / "vm2"
        d2.mkdir()
        (d2 / "kento-port").write_text("10024:22\n")
        with patch("kento.vm.VM_BASE", vm_base), \
             patch("kento.LXC_BASE", lxc_base):
            assert allocate_port() == 10023

    @patch("kento.vm._port_is_free", return_value=True)
    def test_multiple_consecutive(self, mock_free, tmp_path):
        vm_base = tmp_path / "vm"
        vm_base.mkdir()
        lxc_base = tmp_path / "lxc"
        lxc_base.mkdir()
        for i in range(3):
            d = vm_base / f"vm{i}"
            d.mkdir()
            (d / "kento-port").write_text(f"{10022 + i}:22\n")
        with patch("kento.vm.VM_BASE", vm_base), \
             patch("kento.LXC_BASE", lxc_base):
            assert allocate_port() == 10025

    @patch("kento.vm._port_is_free", return_value=True)
    def test_nonexistent_dir(self, mock_free, tmp_path):
        vm_base = tmp_path / "vm-nope"
        lxc_base = tmp_path / "lxc-nope"
        with patch("kento.vm.VM_BASE", vm_base), \
             patch("kento.LXC_BASE", lxc_base):
            assert allocate_port() == 10022

    @patch("kento.vm._port_is_free", return_value=True)
    def test_ignores_malformed_port(self, mock_free, tmp_path):
        vm_base = tmp_path / "vm"
        vm_base.mkdir()
        lxc_base = tmp_path / "lxc"
        lxc_base.mkdir()
        d = vm_base / "vm1"
        d.mkdir()
        (d / "kento-port").write_text("bad\n")
        with patch("kento.vm.VM_BASE", vm_base), \
             patch("kento.LXC_BASE", lxc_base):
            assert allocate_port() == 10022

    @patch("kento.vm._port_is_free", return_value=True)
    def test_custom_guest_port(self, mock_free, tmp_path):
        vm_base = tmp_path / "vm"
        vm_base.mkdir()
        lxc_base = tmp_path / "lxc"
        lxc_base.mkdir()
        d = vm_base / "vm1"
        d.mkdir()
        (d / "kento-port").write_text("10022:2222\n")
        with patch("kento.vm.VM_BASE", vm_base), \
             patch("kento.LXC_BASE", lxc_base):
            assert allocate_port() == 10023

    @patch("kento.vm._port_is_free", side_effect=lambda p: p != 10022)
    def test_skips_port_in_use_on_host(self, mock_free, tmp_path):
        """Port 10022 not in kento-port files but bound on host — skip it."""
        vm_base = tmp_path / "vm"
        vm_base.mkdir()
        lxc_base = tmp_path / "lxc"
        lxc_base.mkdir()
        with patch("kento.vm.VM_BASE", vm_base), \
             patch("kento.LXC_BASE", lxc_base):
            assert allocate_port() == 10023

    @patch("kento.vm._port_is_free", return_value=False)
    def test_all_ports_exhausted(self, mock_free, tmp_path):
        """Every port in range is busy — should raise StateError."""
        vm_base = tmp_path / "vm"
        vm_base.mkdir()
        lxc_base = tmp_path / "lxc"
        lxc_base.mkdir()
        with patch("kento.vm.VM_BASE", vm_base), \
             patch("kento.LXC_BASE", lxc_base):
            with pytest.raises(StateError, match="no free port in range"):
                allocate_port()

    @patch("kento.vm._port_is_free", return_value=True)
    def test_scans_both_vm_and_lxc_bases(self, mock_free, tmp_path):
        """allocate_port scans both VM_BASE and LXC_BASE for used ports."""
        vm_base = tmp_path / "vm"
        vm_base.mkdir()
        lxc_base = tmp_path / "lxc"
        lxc_base.mkdir()
        # Port in VM namespace
        d_vm = vm_base / "vm1"
        d_vm.mkdir()
        (d_vm / "kento-port").write_text("10022:22\n")
        # Port in LXC namespace
        d_lxc = lxc_base / "lxc1"
        d_lxc.mkdir()
        (d_lxc / "kento-port").write_text("10023:22\n")
        with patch("kento.vm.VM_BASE", vm_base), \
             patch("kento.LXC_BASE", lxc_base):
            assert allocate_port() == 10024

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
    @patch("kento.subprocess_util.subprocess.run",
           return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    def test_mount_command(self, mock_run, mock_mp, tmp_path):
        lxc_dir = tmp_path / "vm1"
        lxc_dir.mkdir()
        (lxc_dir / "rootfs").mkdir()
        state_dir = lxc_dir

        mount_rootfs(lxc_dir, "/a:/b", state_dir)

        mock_run.assert_called_once()
        args = list(mock_run.call_args[0][0])
        assert args == [
            "mount", "-t", "overlay", "overlay", "-o",
            f"lowerdir=/a:/b,upperdir={state_dir}/upper,workdir={state_dir}/work",
            str(lxc_dir / "rootfs"),
        ]
        env = mock_run.call_args[1]["env"]
        assert env["LIBMOUNT_FORCE_MOUNT2"] == "always"
        # run_or_die captures stderr internally; check is not used.
        assert mock_run.call_args[1].get("capture_output") is True

    @patch("kento.vm._is_mountpoint", return_value=True)
    def test_mount_rejects_already_mounted(self, mock_mp, tmp_path):
        lxc_dir = tmp_path / "vm1"
        lxc_dir.mkdir()
        (lxc_dir / "rootfs").mkdir()
        with pytest.raises(StateError, match="rootfs already mounted"):
            mount_rootfs(lxc_dir, "/a:/b", lxc_dir)

    @patch("kento.vm._is_mountpoint", return_value=False)
    def test_mount_failure_raises_clean_error(self, mock_mp, tmp_path, caplog):
        """A failed mount raises SubprocessError with a kento message, not a traceback."""
        lxc_dir = tmp_path / "vm1"
        lxc_dir.mkdir()
        (lxc_dir / "rootfs").mkdir()

        def _fail(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 32, stdout="",
                                               stderr="mount: overlay: bad superblock")

        with caplog.at_level(logging.INFO, logger="kento"):
            with patch("kento.subprocess_util.subprocess.run", side_effect=_fail):
                with pytest.raises(SubprocessError, match="failed to mount overlayfs"):
                    mount_rootfs(lxc_dir, "/a:/b", lxc_dir)
        assert any("hint:" in r.message for r in caplog.records)


# --- unmount_rootfs ---


class TestUnmountRootfs:
    @patch("kento.subprocess_util.subprocess.run",
           return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    def test_unmount_command(self, mock_run, tmp_path):
        unmount_rootfs(tmp_path)
        mock_run.assert_called_once()
        assert list(mock_run.call_args[0][0]) == ["umount", str(tmp_path / "rootfs")]

    def test_unmount_failure_raises_clean_error(self, tmp_path, caplog):
        """A failed umount raises SubprocessError with a kento message, not a traceback."""
        def _fail(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="",
                                               stderr="umount: target busy")
        with caplog.at_level(logging.INFO, logger="kento"):
            with patch("kento.subprocess_util.subprocess.run", side_effect=_fail):
                with pytest.raises(SubprocessError, match="failed to unmount rootfs"):
                    unmount_rootfs(tmp_path)
        assert any("hint:" in r.message for r in caplog.records)


# --- start_vm ---


class TestStartVm:
    @patch("kento.subprocess_util.subprocess.run",
           return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    @patch("kento.vm._find_virtiofsd", return_value="/usr/libexec/virtiofsd")
    @patch("kento.vm.is_vm_running", return_value=False)
    @patch("kento.vm.subprocess.Popen")
    @patch("kento.vm.mount_rootfs")
    def test_start_lifecycle(self, mock_mount, mock_popen, mock_running, mock_find, mock_run, tmp_path):
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
        (lxc_dir / "kento-inject.sh").write_text("#!/bin/sh\n")
        # Pre-create socket so wait loop exits immediately
        (lxc_dir / "virtiofsd.sock").write_text("")

        mock_vfs = MagicMock()
        mock_vfs.pid = 1001
        mock_vfs.poll.return_value = None
        mock_qemu = MagicMock()
        mock_qemu.pid = 1002
        mock_popen.side_effect = [mock_vfs, mock_qemu]

        start_vm(lxc_dir, "testvm")

        mock_mount.assert_called_once()
        assert mock_popen.call_count == 2
        assert (lxc_dir / "kento-virtiofsd-pid").read_text().strip() == "1001"
        assert (lxc_dir / "kento-qemu-pid").read_text().strip() == "1002"

    @patch("kento.subprocess_util.subprocess.run",
           return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    @patch("kento.vm._find_virtiofsd", return_value="/usr/libexec/virtiofsd")
    @patch("kento.vm.is_vm_running", return_value=False)
    @patch("kento.vm.subprocess.Popen")
    @patch("kento.vm.mount_rootfs")
    def test_start_detaches_daemons(self, mock_mount, mock_popen, mock_running, mock_find, mock_run, tmp_path):
        # Both long-lived daemons (virtiofsd, qemu) must fully detach from the
        # caller: redirect stdin to /dev/null and run in their own session.
        # Otherwise qemu holds the inherited stdin and a non-interactive
        # `kento start` (e.g. over ssh-exec) hangs until the VM exits. (Serial
        # now goes to a unix socket, not stdio, but detaching stdin is still
        # the correct invariant.)
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
        (lxc_dir / "kento-inject.sh").write_text("#!/bin/sh\n")
        (lxc_dir / "virtiofsd.sock").write_text("")

        mock_vfs = MagicMock()
        mock_vfs.pid = 1001
        mock_vfs.poll.return_value = None
        mock_qemu = MagicMock()
        mock_qemu.pid = 1002
        mock_popen.side_effect = [mock_vfs, mock_qemu]

        start_vm(lxc_dir, "testvm")

        assert mock_popen.call_count == 2
        # virtiofsd is the first Popen call, qemu the second.
        for call in mock_popen.call_args_list:
            kwargs = call[1]
            assert kwargs["stdin"] == subprocess.DEVNULL
            assert kwargs["start_new_session"] is True

    @patch("kento.subprocess_util.subprocess.run",
           return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    @patch("kento.vm._find_virtiofsd", return_value="/usr/libexec/virtiofsd")
    @patch("kento.vm.is_vm_running", return_value=False)
    @patch("kento.vm.subprocess.Popen")
    @patch("kento.vm.mount_rootfs")
    def test_start_passes_port(self, mock_mount, mock_popen, mock_running, mock_find, mock_run, tmp_path):
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
        (lxc_dir / "kento-inject.sh").write_text("#!/bin/sh\n")
        (lxc_dir / "virtiofsd.sock").write_text("")

        mock_vfs = MagicMock()
        mock_vfs.pid = 100
        mock_vfs.poll.return_value = None
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

        with pytest.raises(StateError, match="kernel not found"):
            start_vm(lxc_dir, "testvm")
        mock_unmount.assert_called_once()

    @patch("kento.subprocess_util.subprocess.run",
           return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    @patch("kento.vm._find_virtiofsd", return_value="/usr/libexec/virtiofsd")
    @patch("kento.vm.is_vm_running", return_value=False)
    @patch("kento.vm.subprocess.Popen")
    @patch("kento.vm.mount_rootfs")
    def test_start_virtiofsd_args(self, mock_mount, mock_popen, mock_running, mock_find, mock_run, tmp_path):
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
        (lxc_dir / "kento-inject.sh").write_text("#!/bin/sh\n")
        (lxc_dir / "virtiofsd.sock").write_text("")

        mock_vfs = MagicMock()
        mock_vfs.pid = 1001
        mock_vfs.poll.return_value = None
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
    @patch("kento.vm.VM_MEMORY", 1024)
    @patch("kento.subprocess_util.subprocess.run",
           return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    @patch("kento.vm._find_virtiofsd", return_value="/usr/libexec/virtiofsd")
    @patch("kento.vm.is_vm_running", return_value=False)
    @patch("kento.vm.subprocess.Popen")
    @patch("kento.vm.mount_rootfs")
    def test_start_qemu_args(self, mock_mount, mock_popen, mock_running, mock_find, mock_run, tmp_path):
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
        (lxc_dir / "kento-inject.sh").write_text("#!/bin/sh\n")
        (lxc_dir / "virtiofsd.sock").write_text("")

        mock_vfs = MagicMock()
        mock_vfs.pid = 1001
        mock_vfs.poll.return_value = None
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
        assert qemu_args[m_idx + 1] == "1024"

        # Machine type
        machine_idx = qemu_args.index("-machine")
        assert qemu_args[machine_idx + 1] == "q35"

        # KVM enabled. No kento-nesting file → nesting off → vmx/svm masked.
        assert "-enable-kvm" in qemu_args
        cpu_idx = qemu_args.index("-cpu")
        assert qemu_args[cpu_idx + 1] == "host,vmx=off,svm=off"

        # Display: -display none (not -nographic). -nographic would alias the
        # guest serial to QEMU's stdio; we attach serial to a socket instead.
        assert "-nographic" not in qemu_args
        display_idx = qemu_args.index("-display")
        assert qemu_args[display_idx + 1] == "none"

        # Serial + QMP unix sockets (VM-interactive wiring).
        serial_idx = qemu_args.index("-serial")
        assert qemu_args[serial_idx + 1] == (
            f"unix:{lxc_dir / 'serial.sock'},server=on,wait=off"
        )
        qmp_idx = qemu_args.index("-qmp")
        assert qemu_args[qmp_idx + 1] == (
            f"unix:{lxc_dir / 'qmp.sock'},server=on,wait=off"
        )

        # virtiofs chardev and device
        socket_path = str(lxc_dir / "virtiofsd.sock")
        chardev_idx = qemu_args.index("-chardev")
        assert qemu_args[chardev_idx + 1] == f"socket,id=vfs,path={socket_path}"
        device_idx = qemu_args.index("-device")
        assert qemu_args[device_idx + 1] == "vhost-user-fs-pci,chardev=vfs,tag=rootfs"

        # Memory backend for virtiofs (memfd with share=on)
        obj_idx = qemu_args.index("-object")
        assert qemu_args[obj_idx + 1] == "memory-backend-memfd,id=mem,size=1024M,share=on"

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
    @patch("kento.subprocess_util.subprocess.run",
           return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    @patch("kento.vm._find_virtiofsd", return_value="/usr/libexec/virtiofsd")
    @patch("kento.vm.is_vm_running", return_value=False)
    @patch("kento.vm.subprocess.Popen")
    @patch("kento.vm.mount_rootfs")
    def test_start_qemu_no_kvm(self, mock_mount, mock_popen, mock_running, mock_find, mock_run, tmp_path):
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
        (lxc_dir / "kento-inject.sh").write_text("#!/bin/sh\n")
        (lxc_dir / "virtiofsd.sock").write_text("")
        # No kento-port file — no network args

        mock_vfs = MagicMock()
        mock_vfs.pid = 1001
        mock_vfs.poll.return_value = None
        mock_qemu = MagicMock()
        mock_qemu.pid = 1002
        mock_popen.side_effect = [mock_vfs, mock_qemu]

        start_vm(lxc_dir, "testvm")

        qemu_args = mock_popen.call_args_list[1][0][0]
        assert "-enable-kvm" not in qemu_args
        assert "-cpu" not in qemu_args
        # Without kento-port, no network args
        assert "-netdev" not in qemu_args

    @patch("kento.vm.VM_KVM", True)
    @patch("kento.vm.VM_MACHINE", "q35")
    @patch("kento.vm.VM_MEMORY", 1024)
    @patch("kento.subprocess_util.subprocess.run",
           return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    @patch("kento.vm._find_virtiofsd", return_value="/usr/libexec/virtiofsd")
    @patch("kento.vm.is_vm_running", return_value=False)
    @patch("kento.vm.subprocess.Popen")
    @patch("kento.vm.mount_rootfs")
    def _run_start_with_nesting(self, mock_mount, mock_popen, mock_running,
                                mock_find, mock_run, tmp_path, nesting):
        """Helper: start a VM with the given kento-nesting content (or None)
        and return the QEMU argv. nesting is "1", "0", or None (absent)."""
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
        (lxc_dir / "kento-inject.sh").write_text("#!/bin/sh\n")
        (lxc_dir / "virtiofsd.sock").write_text("")
        if nesting is not None:
            (lxc_dir / "kento-nesting").write_text(nesting + "\n")

        mock_vfs = MagicMock()
        mock_vfs.pid = 1001
        mock_vfs.poll.return_value = None
        mock_qemu = MagicMock()
        mock_qemu.pid = 1002
        mock_popen.side_effect = [mock_vfs, mock_qemu]

        start_vm(lxc_dir, "testvm")
        return mock_popen.call_args_list[1][0][0]

    def test_start_qemu_nesting_on(self, tmp_path):
        """kento-nesting=="1" → -cpu host (vmx/svm exposed)."""
        qemu_args = self._run_start_with_nesting(tmp_path=tmp_path, nesting="1")
        cpu_idx = qemu_args.index("-cpu")
        assert qemu_args[cpu_idx + 1] == "host"
        assert "host,vmx=off,svm=off" not in qemu_args

    def test_start_qemu_nesting_off_explicit(self, tmp_path):
        """kento-nesting=="0" → -cpu host,vmx=off,svm=off (masked)."""
        qemu_args = self._run_start_with_nesting(tmp_path=tmp_path, nesting="0")
        cpu_idx = qemu_args.index("-cpu")
        assert qemu_args[cpu_idx + 1] == "host,vmx=off,svm=off"

    def test_start_qemu_nesting_absent(self, tmp_path):
        """No kento-nesting file → treated as off → masked."""
        qemu_args = self._run_start_with_nesting(tmp_path=tmp_path, nesting=None)
        cpu_idx = qemu_args.index("-cpu")
        assert qemu_args[cpu_idx + 1] == "host,vmx=off,svm=off"

    @patch("kento.vm.is_vm_running", return_value=True)
    def test_start_already_running_is_idempotent(self, mock_running, tmp_path, caplog):
        """F15: start_vm on an already-running VM is a no-op, not an error."""
        lxc_dir = tmp_path / "testvm"
        lxc_dir.mkdir()
        with caplog.at_level(logging.INFO, logger="kento"):
            # Does not raise; returns cleanly.
            start_vm(lxc_dir, "testvm")
        assert any("Already running: testvm" in r.message for r in caplog.records)

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

        with pytest.raises(StateError, match="initramfs not found"):
            start_vm(lxc_dir, "testvm")
        mock_unmount.assert_called_once()

    @patch("kento.subprocess_util.subprocess.run",
           return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    @patch("kento.vm._find_virtiofsd", return_value="/usr/libexec/virtiofsd")
    @patch("kento.vm.is_vm_running", return_value=False)
    @patch("kento.vm.subprocess.Popen")
    @patch("kento.vm.mount_rootfs")
    def test_start_invokes_inject_before_virtiofsd(
            self, mock_mount, mock_popen, mock_running, mock_find, mock_run, tmp_path):
        """inject.sh must run after mount_rootfs and before virtiofsd launches."""
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
        (lxc_dir / "kento-inject.sh").write_text("#!/bin/sh\n")
        (lxc_dir / "virtiofsd.sock").write_text("")

        # Track call ordering across mock_mount, mock_run, mock_popen.
        order: list[str] = []
        mock_mount.side_effect = lambda *a, **kw: order.append("mount")

        def run_side_effect(*a, **kw):
            order.append("inject")
            return subprocess.CompletedProcess(a[0] if a else [], 0, stdout="", stderr="")
        mock_run.side_effect = run_side_effect

        def popen_side_effect(*a, **kw):
            order.append("popen")
            m = MagicMock()
            m.pid = len(order)
            m.poll.return_value = None  # virtiofsd alive → no abort
            return m
        mock_popen.side_effect = popen_side_effect

        start_vm(lxc_dir, "testvm")

        # inject must happen after mount and before any Popen (virtiofsd/qemu).
        assert order[0] == "mount"
        assert order[1] == "inject"
        assert order[2] == "popen"

        # Verify the inject invocation shape.
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        cmd = list(args[0])
        assert cmd[0] == "sh"
        assert cmd[1] == str(lxc_dir / "kento-inject.sh")
        assert cmd[2] == str(rootfs)
        assert cmd[3] == str(lxc_dir)
        # run_or_die captures stderr itself; no check= kwarg.
        assert kwargs.get("capture_output") is True

    @patch("kento.subprocess_util.subprocess.run",
           return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="inject failed"))
    @patch("kento.vm._find_virtiofsd", return_value="/usr/libexec/virtiofsd")
    @patch("kento.vm.is_vm_running", return_value=False)
    @patch("kento.vm.subprocess.Popen")
    @patch("kento.vm.mount_rootfs")
    def test_start_inject_failure_propagates(
            self, mock_mount, mock_popen, mock_running, mock_find, mock_run, tmp_path):
        """A failing inject.sh must abort start — virtiofsd/qemu never launch.

        run_or_die converts the non-zero exit into a SubprocessError with a
        kento-branded message, not a CalledProcessError traceback.
        """
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
        (lxc_dir / "kento-inject.sh").write_text("#!/bin/sh\nexit 1\n")

        with pytest.raises(SubprocessError, match="failed to inject guest config testvm"):
            start_vm(lxc_dir, "testvm")

        # Neither virtiofsd nor qemu should have launched.
        mock_popen.assert_not_called()

    @patch("kento.vm._find_virtiofsd", return_value="/usr/libexec/virtiofsd")
    @patch("kento.vm.is_vm_running", return_value=False)
    @patch("kento.vm.unmount_rootfs")
    @patch("kento.vm.mount_rootfs")
    def test_start_missing_inject_script(
            self, mock_mount, mock_unmount, mock_running, mock_find, tmp_path):
        """Missing kento-inject.sh aborts start with an unmount + StateError."""
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
        # No kento-inject.sh

        with pytest.raises(StateError, match="inject script not found"):
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

    @patch("kento.vm._is_mountpoint", return_value=False)
    @patch("kento.vm._kill_and_wait")
    def test_stop_unlinks_serial_and_qmp_sockets(self, mock_kw, mock_mp, tmp_path):
        """stop_vm cleans up serial.sock and qmp.sock alongside virtiofsd.sock."""
        (tmp_path / "virtiofsd.sock").write_text("")
        (tmp_path / "serial.sock").write_text("")
        (tmp_path / "qmp.sock").write_text("")

        stop_vm(tmp_path)

        assert not (tmp_path / "virtiofsd.sock").exists()
        assert not (tmp_path / "serial.sock").exists()
        assert not (tmp_path / "qmp.sock").exists()

    @patch("kento.vm._is_mountpoint", return_value=False)
    @patch("kento.vm._kill_and_wait")
    def test_stop_socket_cleanup_tolerates_absent(self, mock_kw, mock_mp, tmp_path):
        """Missing serial/qmp sockets (older instances) don't break stop."""
        stop_vm(tmp_path)  # should not raise

    @patch("kento.vm.subprocess.run", return_value=subprocess.CompletedProcess([], 0))
    @patch("kento.vm._is_mountpoint", return_value=True)
    @patch("kento.vm._kill_and_wait")
    def test_stop_unmounts_if_mounted(self, mock_kw, mock_mp, mock_run, tmp_path):
        (tmp_path / "rootfs").mkdir()

        stop_vm(tmp_path)

        mock_run.assert_called_once_with(
            ["umount", str(tmp_path / "rootfs")], capture_output=True, text=True
        )

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

    @patch("kento.vm.Path")
    @patch("kento.vm.os.kill")
    def test_default_escalates_to_sigkill_when_stubborn(self, mock_kill,
                                                        mock_path_cls, tmp_path):
        # Default (no_kill=False): a process still alive at the deadline gets
        # SIGKILLed (today's behavior — unchanged).
        from kento.vm import _kill_and_wait
        pid_file = tmp_path / "pid"
        pid_file.write_text("1234\n")
        proc_path = MagicMock()
        proc_path.is_dir.return_value = True  # never exits
        original_path = Path
        mock_path_cls.side_effect = lambda p: (
            proc_path if p == "/proc/1234" else original_path(p))

        with patch("kento.vm.time.sleep"), patch("kento.vm.time.monotonic",
                                                 side_effect=[0.0, 0.0, 99.0]):
            _kill_and_wait(pid_file, timeout=5.0)

        # SIGTERM up front, then SIGKILL at the deadline.
        assert mock_kill.call_args_list[0] == ((1234, signal.SIGTERM),)
        assert mock_kill.call_args_list[-1] == ((1234, signal.SIGKILL),)
        assert not pid_file.exists()

    @patch("kento.vm.Path")
    @patch("kento.vm.os.kill")
    def test_no_kill_does_not_sigkill_stubborn(self, mock_kill, mock_path_cls,
                                               tmp_path):
        # no_kill=True (M6 graceful): a process still alive at the deadline is
        # LEFT running — SIGTERM only, never SIGKILL, pid file preserved.
        from kento.vm import _kill_and_wait
        pid_file = tmp_path / "pid"
        pid_file.write_text("1234\n")
        proc_path = MagicMock()
        proc_path.is_dir.return_value = True  # never exits
        original_path = Path
        mock_path_cls.side_effect = lambda p: (
            proc_path if p == "/proc/1234" else original_path(p))

        with patch("kento.vm.time.sleep"), patch("kento.vm.time.monotonic",
                                                 side_effect=[0.0, 0.0, 99.0]):
            _kill_and_wait(pid_file, timeout=5.0, no_kill=True)

        # Only SIGTERM — no SIGKILL escalation.
        mock_kill.assert_called_once_with(1234, signal.SIGTERM)
        # pid file preserved (process still alive, left for the typed re-probe).
        assert pid_file.exists()


class TestGenerateMac:
    """Tests for generate_mac() — deterministic MAC address generation."""

    def test_prefix_is_qemu_block(self):
        mac = generate_mac("anything")
        assert mac.startswith("52:54:00:")

    def test_mac_prefix_constant(self):
        assert MAC_PREFIX == "52:54:00"

    def test_format_is_six_pairs(self):
        mac = generate_mac("foo")
        parts = mac.split(":")
        assert len(parts) == 6
        for p in parts:
            assert len(p) == 2
            int(p, 16)  # valid hex

    def test_deterministic_same_input(self):
        assert generate_mac("foo-0") == generate_mac("foo-0")

    def test_different_inputs_different_outputs(self):
        assert generate_mac("foo-0") != generate_mac("foo-1")

    def test_accepts_vmid_string(self):
        mac = generate_mac("100")
        assert mac.startswith("52:54:00:")
        assert is_valid_mac(mac)


class TestIsValidMac:
    def test_valid_mac(self):
        assert is_valid_mac("52:54:00:ab:cd:ef")

    def test_valid_uppercase(self):
        assert is_valid_mac("AA:BB:CC:DD:EE:FF")

    def test_valid_mixed_case(self):
        assert is_valid_mac("aA:Bb:00:11:22:33")

    def test_too_few_pairs(self):
        assert not is_valid_mac("52:54:00:ab:cd")

    def test_too_many_pairs(self):
        assert not is_valid_mac("52:54:00:ab:cd:ef:01")

    def test_missing_colons(self):
        assert not is_valid_mac("525400abcdef")

    def test_non_hex_chars(self):
        assert not is_valid_mac("52:54:00:ab:cd:gg")

    def test_empty_string(self):
        assert not is_valid_mac("")


class TestStartVmMac:
    """Tests that start_vm includes mac= on the -device line when kento-mac exists."""

    @patch("kento.subprocess_util.subprocess.run",
           return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    @patch("kento.vm._find_virtiofsd", return_value="/usr/libexec/virtiofsd")
    @patch("kento.vm.is_vm_running", return_value=False)
    @patch("kento.vm.subprocess.Popen")
    @patch("kento.vm.mount_rootfs")
    def test_start_qemu_includes_mac(self, mock_mount, mock_popen, mock_running, mock_find, mock_run, tmp_path):
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
        (lxc_dir / "kento-inject.sh").write_text("#!/bin/sh\n")
        (lxc_dir / "kento-mac").write_text("52:54:00:de:ad:be\n")
        (lxc_dir / "virtiofsd.sock").write_text("")

        mock_vfs = MagicMock()
        mock_vfs.pid = 1001
        mock_vfs.poll.return_value = None
        mock_qemu = MagicMock()
        mock_qemu.pid = 1002
        mock_popen.side_effect = [mock_vfs, mock_qemu]

        start_vm(lxc_dir, "testvm")

        qemu_args = mock_popen.call_args_list[1][0][0]
        # Find the -device that follows -netdev
        joined = " ".join(qemu_args)
        assert "virtio-net-pci,netdev=net0,mac=52:54:00:de:ad:be" in joined

    @patch("kento.subprocess_util.subprocess.run",
           return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    @patch("kento.vm._find_virtiofsd", return_value="/usr/libexec/virtiofsd")
    @patch("kento.vm.is_vm_running", return_value=False)
    @patch("kento.vm.subprocess.Popen")
    @patch("kento.vm.mount_rootfs")
    def test_start_qemu_no_mac_file(self, mock_mount, mock_popen, mock_running, mock_find, mock_run, tmp_path):
        """Without kento-mac (older containers), the -device line has no mac=."""
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
        (lxc_dir / "kento-inject.sh").write_text("#!/bin/sh\n")
        (lxc_dir / "virtiofsd.sock").write_text("")
        # No kento-mac

        mock_vfs = MagicMock()
        mock_vfs.pid = 1001
        mock_vfs.poll.return_value = None
        mock_qemu = MagicMock()
        mock_qemu.pid = 1002
        mock_popen.side_effect = [mock_vfs, mock_qemu]

        start_vm(lxc_dir, "testvm")

        qemu_args = mock_popen.call_args_list[1][0][0]
        joined = " ".join(qemu_args)
        assert "virtio-net-pci,netdev=net0" in joined
        assert "mac=" not in joined


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


class TestStartVmQemuArgsPassthrough:
    """B2: start_vm appends kento-qemu-args lines to the QEMU argv."""

    def _setup(self, tmp_path):
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
        (lxc_dir / "kento-inject.sh").write_text("#!/bin/sh\n")
        (lxc_dir / "virtiofsd.sock").write_text("")
        return lxc_dir

    @patch("kento.subprocess_util.subprocess.run",
           return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    @patch("kento.vm._find_virtiofsd", return_value="/usr/libexec/virtiofsd")
    @patch("kento.vm.is_vm_running", return_value=False)
    @patch("kento.vm.subprocess.Popen")
    @patch("kento.vm.mount_rootfs")
    def test_no_passthrough_file_argv_unchanged(
            self, mock_mount, mock_popen, mock_running, mock_find, mock_run, tmp_path):
        """Baseline: no kento-qemu-args file -> argv does not grow past kento's own flags."""
        lxc_dir = self._setup(tmp_path)
        mock_vfs = MagicMock(); mock_vfs.pid = 1; mock_vfs.poll.return_value = None
        mock_qemu = MagicMock(); mock_qemu.pid = 2
        mock_popen.side_effect = [mock_vfs, mock_qemu]

        start_vm(lxc_dir, "testvm")

        qemu_args = mock_popen.call_args_list[1][0][0]
        # Last kento-managed element is the -append value.
        assert qemu_args[-2] == "-append"
        assert qemu_args[-1] == "console=ttyS0 rootfstype=virtiofs root=rootfs"

    @patch("kento.subprocess_util.subprocess.run",
           return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    @patch("kento.vm._find_virtiofsd", return_value="/usr/libexec/virtiofsd")
    @patch("kento.vm.is_vm_running", return_value=False)
    @patch("kento.vm.subprocess.Popen")
    @patch("kento.vm.mount_rootfs")
    def test_single_line_appended(
            self, mock_mount, mock_popen, mock_running, mock_find, mock_run, tmp_path):
        lxc_dir = self._setup(tmp_path)
        (lxc_dir / "kento-qemu-args").write_text("-device=virtio-rng-pci\n")
        mock_vfs = MagicMock(); mock_vfs.pid = 1; mock_vfs.poll.return_value = None
        mock_qemu = MagicMock(); mock_qemu.pid = 2
        mock_popen.side_effect = [mock_vfs, mock_qemu]

        start_vm(lxc_dir, "testvm")

        qemu_args = mock_popen.call_args_list[1][0][0]
        # Appended at the end (after kento's -append ...).
        assert qemu_args[-1] == "-device=virtio-rng-pci"
        # Exactly one added element.
        assert qemu_args.count("-device=virtio-rng-pci") == 1

    @patch("kento.subprocess_util.subprocess.run",
           return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    @patch("kento.vm._find_virtiofsd", return_value="/usr/libexec/virtiofsd")
    @patch("kento.vm.is_vm_running", return_value=False)
    @patch("kento.vm.subprocess.Popen")
    @patch("kento.vm.mount_rootfs")
    def test_multi_line_appended_in_order(
            self, mock_mount, mock_popen, mock_running, mock_find, mock_run, tmp_path):
        lxc_dir = self._setup(tmp_path)
        (lxc_dir / "kento-qemu-args").write_text(
            "-device\nvirtio-rng-pci\n-m\n2048\n"
        )
        mock_vfs = MagicMock(); mock_vfs.pid = 1; mock_vfs.poll.return_value = None
        mock_qemu = MagicMock(); mock_qemu.pid = 2
        mock_popen.side_effect = [mock_vfs, mock_qemu]

        start_vm(lxc_dir, "testvm")

        qemu_args = mock_popen.call_args_list[1][0][0]
        # All four entries appear in order at the tail.
        assert qemu_args[-4:] == ["-device", "virtio-rng-pci", "-m", "2048"]
        # The kento-emitted -m 1024 still precedes the pass-through -m 2048;
        # QEMU's last-occurrence semantics lets the user override.
        first_m = qemu_args.index("-m")
        last_m = len(qemu_args) - 1 - qemu_args[::-1].index("-m")
        assert first_m < last_m
        assert qemu_args[first_m + 1] == "1024"
        assert qemu_args[last_m + 1] == "2048"

    @patch("kento.subprocess_util.subprocess.run",
           return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    @patch("kento.vm._find_virtiofsd", return_value="/usr/libexec/virtiofsd")
    @patch("kento.vm.is_vm_running", return_value=False)
    @patch("kento.vm.subprocess.Popen")
    @patch("kento.vm.mount_rootfs")
    def test_serial_qmp_precede_passthrough(
            self, mock_mount, mock_popen, mock_running, mock_find, mock_run, tmp_path):
        """kento's -serial/-qmp must appear BEFORE any pass-through args so a
        user --qemu-arg can still override them (QEMU last-occurrence wins)."""
        lxc_dir = self._setup(tmp_path)
        (lxc_dir / "kento-qemu-args").write_text("-device=virtio-rng-pci\n")
        mock_vfs = MagicMock(); mock_vfs.pid = 1; mock_vfs.poll.return_value = None
        mock_qemu = MagicMock(); mock_qemu.pid = 2
        mock_popen.side_effect = [mock_vfs, mock_qemu]

        start_vm(lxc_dir, "testvm")

        qemu_args = mock_popen.call_args_list[1][0][0]
        passthrough_idx = qemu_args.index("-device=virtio-rng-pci")
        assert qemu_args.index("-serial") < passthrough_idx
        assert qemu_args.index("-qmp") < passthrough_idx
        assert qemu_args.index("-display") < passthrough_idx

    @patch("kento.subprocess_util.subprocess.run",
           return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    @patch("kento.vm._find_virtiofsd", return_value="/usr/libexec/virtiofsd")
    @patch("kento.vm.is_vm_running", return_value=False)
    @patch("kento.vm.subprocess.Popen")
    @patch("kento.vm.mount_rootfs")
    def test_empty_lines_ignored(
            self, mock_mount, mock_popen, mock_running, mock_find, mock_run, tmp_path):
        """Blank lines in kento-qemu-args should not become empty argv elements."""
        lxc_dir = self._setup(tmp_path)
        (lxc_dir / "kento-qemu-args").write_text("\n-device=virtio-rng-pci\n\n")
        mock_vfs = MagicMock(); mock_vfs.pid = 1; mock_vfs.poll.return_value = None
        mock_qemu = MagicMock(); mock_qemu.pid = 2
        mock_popen.side_effect = [mock_vfs, mock_qemu]

        start_vm(lxc_dir, "testvm")

        qemu_args = mock_popen.call_args_list[1][0][0]
        assert "" not in qemu_args
        assert qemu_args[-1] == "-device=virtio-rng-pci"

    @patch("kento.subprocess_util.subprocess.run",
           return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    @patch("kento.vm._find_virtiofsd", return_value="/usr/libexec/virtiofsd")
    @patch("kento.vm.is_vm_running", return_value=False)
    @patch("kento.vm.subprocess.Popen")
    @patch("kento.vm.mount_rootfs")
    def test_whitespace_only_lines_ignored(
            self, mock_mount, mock_popen, mock_running, mock_find, mock_run, tmp_path):
        """A whitespace-only kento-qemu-args line must NOT become a lone argv
        token: that would be an empty/positional arg QEMU rejects at boot.

        ``if line:`` is truthy for "   ", so the loop must strip first."""
        lxc_dir = self._setup(tmp_path)
        (lxc_dir / "kento-qemu-args").write_text(
            "   \n-device=virtio-rng-pci\n\t\n  -m\n"
        )
        mock_vfs = MagicMock(); mock_vfs.pid = 1; mock_vfs.poll.return_value = None
        mock_qemu = MagicMock(); mock_qemu.pid = 2
        mock_popen.side_effect = [mock_vfs, mock_qemu]

        start_vm(lxc_dir, "testvm")

        qemu_args = mock_popen.call_args_list[1][0][0]
        # No empty or whitespace-only tokens reached QEMU.
        assert "" not in qemu_args
        assert not any(tok.strip() == "" for tok in qemu_args)
        # Only the two real (stripped) flags were appended, in order.
        assert qemu_args[-2:] == ["-device=virtio-rng-pci", "-m"]


class TestStartVmCleanupOnFailure:
    """F: start_vm must be self-cleaning on its own failure paths (start.py
    calls it with NO surrounding rollback), tearing down the virtiofsd process
    + the leaked overlay mount + the pid files."""

    def _setup(self, tmp_path):
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
        (lxc_dir / "kento-inject.sh").write_text("#!/bin/sh\n")
        return lxc_dir

    @patch("kento.subprocess_util.subprocess.run",
           return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    @patch("kento.vm._find_virtiofsd", return_value="/usr/libexec/virtiofsd")
    @patch("kento.vm.is_vm_running", return_value=False)
    @patch("kento.vm._is_mountpoint", return_value=True)
    @patch("kento.vm._umount_with_retry", return_value=True)
    @patch("kento.vm.subprocess.Popen")
    @patch("kento.vm.mount_rootfs")
    def test_socket_never_appears_cleans_up(
            self, mock_mount, mock_popen, mock_umount, mock_mp, mock_running,
            mock_find, mock_run, tmp_path):
        """virtiofsd starts but its socket never appears -> terminate virtiofsd,
        unmount, drop pid files, raise StateError. QEMU must NOT launch."""
        lxc_dir = self._setup(tmp_path)
        # Do NOT create virtiofsd.sock -> wait loop times out.
        mock_vfs = MagicMock(); mock_vfs.pid = 1; mock_vfs.poll.return_value = None
        mock_popen.return_value = mock_vfs

        with patch("kento.vm.time.sleep"):  # don't actually wait 5s
            with pytest.raises(StateError, match="virtiofsd socket did not appear"):
                start_vm(lxc_dir, "testvm")

        # Only virtiofsd was spawned; QEMU never launched.
        assert mock_popen.call_count == 1
        # virtiofsd terminated, mount torn down. (The socket-abort path runs
        # cleanup once before sys.exit and the SystemExit handler is idempotent,
        # so terminate may be invoked more than once — only that it ran matters.)
        assert mock_vfs.terminate.called
        assert mock_umount.called
        # Pid files cleaned up.
        assert not (lxc_dir / "kento-virtiofsd-pid").exists()
        assert not (lxc_dir / "kento-qemu-pid").exists()

    @patch("kento.subprocess_util.subprocess.run",
           return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    @patch("kento.vm._find_virtiofsd", return_value="/usr/libexec/virtiofsd")
    @patch("kento.vm.is_vm_running", return_value=False)
    @patch("kento.vm._is_mountpoint", return_value=True)
    @patch("kento.vm._umount_with_retry", return_value=True)
    @patch("kento.vm.subprocess.Popen")
    @patch("kento.vm.mount_rootfs")
    def test_virtiofsd_dies_cleans_up(
            self, mock_mount, mock_popen, mock_umount, mock_mp, mock_running,
            mock_find, mock_run, tmp_path):
        """virtiofsd dies during the wait (poll() returns non-None) -> abort,
        terminate, unmount, raise StateError even though the socket exists."""
        lxc_dir = self._setup(tmp_path)
        (lxc_dir / "virtiofsd.sock").write_text("")  # socket present...
        mock_vfs = MagicMock(); mock_vfs.pid = 1
        mock_vfs.poll.return_value = 1  # ...but virtiofsd has exited.
        mock_popen.return_value = mock_vfs

        with patch("kento.vm.time.sleep"):
            with pytest.raises(StateError, match="virtiofsd socket did not appear"):
                start_vm(lxc_dir, "testvm")

        assert mock_popen.call_count == 1  # QEMU never launched
        assert mock_vfs.terminate.called
        assert mock_umount.called
        assert not (lxc_dir / "kento-virtiofsd-pid").exists()

    @patch("kento.subprocess_util.subprocess.run",
           return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    @patch("kento.vm._find_virtiofsd", return_value="/usr/libexec/virtiofsd")
    @patch("kento.vm.is_vm_running", return_value=False)
    @patch("kento.vm._is_mountpoint", return_value=True)
    @patch("kento.vm._umount_with_retry", return_value=True)
    @patch("kento.vm.subprocess.Popen")
    @patch("kento.vm.mount_rootfs")
    def test_qemu_launch_failure_cleans_up(
            self, mock_mount, mock_popen, mock_umount, mock_mp, mock_running,
            mock_find, mock_run, tmp_path):
        """QEMU Popen raises (e.g. FileNotFoundError) -> virtiofsd + mount +
        pid files torn down and the original error re-raised."""
        lxc_dir = self._setup(tmp_path)
        (lxc_dir / "virtiofsd.sock").write_text("")  # socket appears
        mock_vfs = MagicMock(); mock_vfs.pid = 1; mock_vfs.poll.return_value = None
        # First Popen = virtiofsd (ok); second = QEMU (raises).
        mock_popen.side_effect = [mock_vfs, FileNotFoundError("qemu-system-x86_64")]

        with patch("kento.vm.time.sleep"):
            with pytest.raises(FileNotFoundError):
                start_vm(lxc_dir, "testvm")

        mock_vfs.terminate.assert_called_once()
        mock_umount.assert_called_once()
        assert not (lxc_dir / "kento-virtiofsd-pid").exists()
        assert not (lxc_dir / "kento-qemu-pid").exists()
