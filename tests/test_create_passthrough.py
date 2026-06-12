"""Tests for pass-through flags --qemu-arg / --pve-arg (v1.2.0 Phase B1).

Covers denylist validation and on-disk persistence (kento-qemu-args /
kento-pve-args).  Consumers (vm.py, pve.py, info.py) land in B2-B4 and have
their own tests.  CLI-level argparse surface tests (TestQemuArgCli,
TestLxcArgCli, TestPveArgCli) removed in Task 5 — re-homed to kento-cli in
Plan 2.
"""

import json
from unittest.mock import patch

import pytest

from kento.create import create


# ---------- Persistence / storage tests ----------


class TestQemuArgStorage:
    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_qemu_args_written_to_instance_dir(self, mock_root, mock_layers,
                                                mock_run, tmp_path):
        """--qemu-arg values are written one-per-line to kento-qemu-args."""
        vm_base = tmp_path / "vm"
        vm_base.mkdir()
        with patch("kento.create.VM_BASE", vm_base), \
             patch("kento.create.upper_base",
                   side_effect=lambda n, b=None: (b or vm_base) / n):
            create("myimage:latest", name="test", mode="vm",
                   qemu_args=["-device virtio-rng-pci",
                              "-smbios type=1,serial=abc"])

        out = (vm_base / "test" / "kento-qemu-args").read_text()
        assert out == "-device virtio-rng-pci\n-smbios type=1,serial=abc\n"

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_no_qemu_args_no_file(self, mock_root, mock_layers,
                                    mock_run, tmp_path):
        """Without --qemu-arg, the file is not created."""
        vm_base = tmp_path / "vm"
        vm_base.mkdir()
        with patch("kento.create.VM_BASE", vm_base), \
             patch("kento.create.upper_base",
                   side_effect=lambda n, b=None: (b or vm_base) / n):
            create("myimage:latest", name="test", mode="vm")

        assert not (vm_base / "test" / "kento-qemu-args").exists()

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_denylisted_qemu_arg_kernel_rejected(self, mock_root, mock_layers,
                                                  mock_run, tmp_path, capsys):
        vm_base = tmp_path / "vm"
        vm_base.mkdir()
        with patch("kento.create.VM_BASE", vm_base), \
             patch("kento.create.upper_base",
                   side_effect=lambda n, b=None: (b or vm_base) / n):
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", mode="vm",
                       qemu_args=["-kernel /other/vmlinuz"])
        err = capsys.readouterr().err
        assert "kento manages '-kernel'" in err
        # nothing should have been written
        assert not (vm_base / "test").exists() or \
               not (vm_base / "test" / "kento-qemu-args").exists()

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_denylisted_qemu_arg_initrd_rejected(self, mock_root, mock_layers,
                                                  mock_run, tmp_path, capsys):
        vm_base = tmp_path / "vm"
        vm_base.mkdir()
        with patch("kento.create.VM_BASE", vm_base), \
             patch("kento.create.upper_base",
                   side_effect=lambda n, b=None: (b or vm_base) / n):
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", mode="vm",
                       qemu_args=["-initrd /other/initramfs"])
        err = capsys.readouterr().err
        assert "kento manages '-initrd'" in err

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_denylisted_qemu_arg_memfd_rejected(self, mock_root, mock_layers,
                                                 mock_run, tmp_path, capsys):
        """memory-backend-memfd is kento-managed (scrub resyncs size=)."""
        vm_base = tmp_path / "vm"
        vm_base.mkdir()
        with patch("kento.create.VM_BASE", vm_base), \
             patch("kento.create.upper_base",
                   side_effect=lambda n, b=None: (b or vm_base) / n):
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", mode="vm",
                       qemu_args=["-object memory-backend-memfd,id=mem2,size=1G"])
        err = capsys.readouterr().err
        assert "memory-backend-memfd" in err


class TestLxcArgStorage:
    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_lxc_args_written_and_in_config(self, mock_root, mock_layers,
                                            mock_run, tmp_path):
        """--lxc-arg values land in kento-lxc-args AND the generated config."""
        lxc_base = tmp_path / "lxc"
        lxc_base.mkdir()
        with patch("kento.create.LXC_BASE", lxc_base), \
             patch("kento.create.upper_base",
                   side_effect=lambda n, b=None: (b or lxc_base) / n), \
             patch("kento.pve.is_pve", return_value=False):
            create("myimage:latest", name="test", mode="lxc",
                   lxc_args=["lxc.cgroup2.devices.allow = c 10:200 rwm",
                             "lxc.cap.drop = sys_module"])

        d = lxc_base / "test"
        out = (d / "kento-lxc-args").read_text()
        assert out == ("lxc.cgroup2.devices.allow = c 10:200 rwm\n"
                       "lxc.cap.drop = sys_module\n")
        config = (d / "config").read_text()
        assert "lxc.cgroup2.devices.allow = c 10:200 rwm" in config
        assert "lxc.cap.drop = sys_module" in config

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_no_lxc_args_no_file(self, mock_root, mock_layers,
                                 mock_run, tmp_path):
        lxc_base = tmp_path / "lxc"
        lxc_base.mkdir()
        with patch("kento.create.LXC_BASE", lxc_base), \
             patch("kento.create.upper_base",
                   side_effect=lambda n, b=None: (b or lxc_base) / n), \
             patch("kento.pve.is_pve", return_value=False):
            create("myimage:latest", name="test", mode="lxc")
        assert not (lxc_base / "test" / "kento-lxc-args").exists()

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_lxc_arg_denied_key_rejected(self, mock_root, mock_layers,
                                         mock_run, tmp_path, capsys):
        """A denied structural key (lxc.rootfs.path) kills the create."""
        lxc_base = tmp_path / "lxc"
        lxc_base.mkdir()
        with patch("kento.create.LXC_BASE", lxc_base), \
             patch("kento.create.upper_base",
                   side_effect=lambda n, b=None: (b or lxc_base) / n), \
             patch("kento.pve.is_pve", return_value=False):
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", mode="lxc",
                       lxc_args=["lxc.rootfs.path = /evil"])
        err = capsys.readouterr().err
        assert "lxc.rootfs.path" in err
        assert "kento manages" in err

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_lxc_arg_denied_net_prefix_rejected(self, mock_root, mock_layers,
                                                mock_run, tmp_path, capsys):
        """The lxc.net. prefix denial catches lxc.net.0.* keys."""
        lxc_base = tmp_path / "lxc"
        lxc_base.mkdir()
        with patch("kento.create.LXC_BASE", lxc_base), \
             patch("kento.create.upper_base",
                   side_effect=lambda n, b=None: (b or lxc_base) / n), \
             patch("kento.pve.is_pve", return_value=False):
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", mode="lxc",
                       lxc_args=["lxc.net.0.type = phys"])
        assert "lxc.net." in capsys.readouterr().err

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_lxc_arg_rejected_on_vm_mode(self, mock_root, mock_layers,
                                         mock_run, tmp_path, capsys):
        vm_base = tmp_path / "vm"
        vm_base.mkdir()
        with patch("kento.create.VM_BASE", vm_base), \
             patch("kento.create.upper_base",
                   side_effect=lambda n, b=None: (b or vm_base) / n), \
             patch("kento.pve.is_pve", return_value=False):
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", mode="vm",
                       lxc_args=["lxc.cap.drop = sys_module"])
        assert "not applicable to VM modes" in capsys.readouterr().err

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_lxc_arg_rejected_on_pve_lxc_mode(self, mock_root, mock_layers,
                                              mock_run, tmp_path, capsys):
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text(json.dumps({"ids": {}}))
        lxc_base = tmp_path / "lxc"
        lxc_base.mkdir()
        with patch("kento.create.LXC_BASE", lxc_base), \
             patch("kento.create.upper_base",
                   side_effect=lambda n, b=None: (b or lxc_base) / n), \
             patch("kento.pve.PVE_DIR", pve):
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", mode="pve",
                       lxc_args=["lxc.cap.drop = sys_module"])
        err = capsys.readouterr().err
        assert "--lxc-arg is not supported on a PVE host" in err


class TestPveArgStorage:
    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_pve_args_written_on_pve_lxc(self, mock_root, mock_layers,
                                          mock_run, tmp_path):
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text(json.dumps({"ids": {}}))
        lxc_base = tmp_path / "lxc"
        lxc_base.mkdir()

        with patch("kento.create.LXC_BASE", lxc_base), \
             patch("kento.create.upper_base",
                   side_effect=lambda n, b=None: (b or lxc_base) / n), \
             patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve.write_pve_config",
                   side_effect=lambda vmid, content: pve / f"{vmid}.conf"):
            create("myimage:latest", name="test", mode="pve",
                   pve_args=["tags: kento-test", "onboot: 1"])

        # container_dir for pve-lxc is LXC_BASE/<VMID>
        dirs = [p for p in lxc_base.iterdir() if p.is_dir()]
        assert len(dirs) == 1
        out = (dirs[0] / "kento-pve-args").read_text()
        assert out == "tags: kento-test\nonboot: 1\n"

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_pve_args_denylist_rootfs_rejected(self, mock_root, mock_layers,
                                                mock_run, tmp_path, capsys):
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text(json.dumps({"ids": {}}))
        lxc_base = tmp_path / "lxc"
        lxc_base.mkdir()

        with patch("kento.create.LXC_BASE", lxc_base), \
             patch("kento.create.upper_base",
                   side_effect=lambda n, b=None: (b or lxc_base) / n), \
             patch("kento.pve.PVE_DIR", pve):
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", mode="pve",
                       pve_args=["rootfs: local-lvm:vm-100-disk-0"])
        err = capsys.readouterr().err
        assert "rootfs:" in err
        assert "kento manages" in err

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_no_pve_args_no_file(self, mock_root, mock_layers,
                                   mock_run, tmp_path):
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text(json.dumps({"ids": {}}))
        lxc_base = tmp_path / "lxc"
        lxc_base.mkdir()

        with patch("kento.create.LXC_BASE", lxc_base), \
             patch("kento.create.upper_base",
                   side_effect=lambda n, b=None: (b or lxc_base) / n), \
             patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve.write_pve_config",
                   side_effect=lambda vmid, content: pve / f"{vmid}.conf"):
            create("myimage:latest", name="test", mode="pve")

        dirs = [p for p in lxc_base.iterdir() if p.is_dir()]
        assert len(dirs) == 1
        assert not (dirs[0] / "kento-pve-args").exists()
