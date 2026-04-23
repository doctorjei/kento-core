"""Tests for pass-through flags --qemu-arg / --pve-arg (v1.2.0 Phase B1).

Covers the argparse surface (cli.py), mode-specific rejection, denylist
validation, and on-disk persistence (kento-qemu-args / kento-pve-args).
Consumers (vm.py, pve.py, info.py) land in B2-B4 and have their own tests.
"""

import json
from unittest.mock import patch, MagicMock

import pytest

from kento.cli import main
from kento.create import create


# ---------- Argparse / CLI-level tests ----------


class TestQemuArgCli:
    """--qemu-arg: exposed on VM scope only, rejected on LXC scope."""

    def test_qemu_arg_in_vm_create_help(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["vm", "create", "--help"])
        assert exc.value.code == 0
        assert "--qemu-arg" in capsys.readouterr().out

    def test_qemu_arg_passes_through_on_vm(self):
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["vm", "create",
                  "--qemu-arg", "-device virtio-rng-pci",
                  "--qemu-arg", "-smbios type=1,serial=abc",
                  "debian:12"])
        mock_create.assert_called_once()
        assert mock_create.call_args[1]["qemu_args"] == [
            "-device virtio-rng-pci",
            "-smbios type=1,serial=abc",
        ]

    def test_qemu_arg_default_none_on_vm(self):
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["vm", "create", "debian:12"])
        assert mock_create.call_args[1]["qemu_args"] is None

    def test_qemu_arg_rejected_on_lxc_create(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "create", "--qemu-arg", "-device foo", "debian:12"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "--qemu-arg is not supported for LXC" in err
        assert "--lxc-config" in err  # pointer to future flag

    def test_qemu_arg_rejected_on_lxc_run(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "run", "--qemu-arg", "-device foo", "debian:12"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "--qemu-arg is not supported for LXC" in err


class TestPveArgCli:
    """--pve-arg: requires a PVE mode (explicit --pve or auto-detected)."""

    def test_pve_arg_in_lxc_create_help(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "create", "--help"])
        assert exc.value.code == 0
        assert "--pve-arg" in capsys.readouterr().out

    def test_pve_arg_in_vm_create_help(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["vm", "create", "--help"])
        assert exc.value.code == 0
        assert "--pve-arg" in capsys.readouterr().out

    def test_pve_arg_on_pve_lxc_passes_through(self):
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create), \
             patch("kento.pve.is_pve", return_value=True):
            main(["lxc", "create",
                  "--pve", "--pve-arg", "tags: kento-test",
                  "--pve-arg", "onboot: 1",
                  "debian:12"])
        assert mock_create.call_args[1]["pve_args"] == [
            "tags: kento-test", "onboot: 1"]

    def test_pve_arg_on_pve_vm_passes_through(self):
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create), \
             patch("kento.pve.is_pve", return_value=True):
            main(["vm", "create", "--pve",
                  "--pve-arg", "tags: kento-test", "debian:12"])
        assert mock_create.call_args[1]["pve_args"] == ["tags: kento-test"]

    def test_pve_arg_on_plain_lxc_rejected(self, capsys):
        """--pve-arg on plain LXC (is_pve() False, auto-detect) errors with
        a pointer to the future --lxc-config flag."""
        with pytest.raises(SystemExit) as exc, \
                patch("kento.pve.is_pve", return_value=False):
            main(["lxc", "create",
                  "--pve-arg", "tags: foo", "debian:12"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "--pve-arg is not supported for plain LXC" in err
        assert "--lxc-config" in err

    def test_pve_arg_on_plain_vm_rejected(self, capsys):
        """--pve-arg on plain VM (is_pve() False, auto-detect) errors."""
        with pytest.raises(SystemExit) as exc, \
                patch("kento.pve.is_pve", return_value=False):
            main(["vm", "create",
                  "--pve-arg", "tags: foo", "debian:12"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "--pve-arg is not supported for plain VM" in err

    def test_pve_arg_explicit_no_pve_rejected(self, capsys):
        """Even on a PVE host, --no-pve + --pve-arg is a user error (the
        explicit opt-out branch, separate error message)."""
        with pytest.raises(SystemExit) as exc, \
                patch("kento.pve.is_pve", return_value=True):
            main(["lxc", "create", "--no-pve",
                  "--pve-arg", "tags: foo", "debian:12"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "--pve-arg requires PVE mode" in err


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
