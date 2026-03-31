"""Tests for kento.defaults — centralized default values and config loading."""

from pathlib import Path
from unittest.mock import patch

import pytest

from kento.defaults import (
    load_config,
    get_vm_defaults,
    get_lxc_defaults,
    ensure_config_files,
    VM_MEMORY, VM_CORES, VM_KVM, VM_MACHINE, VM_SERIAL, VM_DISPLAY,
    LXC_TTY, LXC_MOUNT_AUTO, LXC_MOUNT_AUTO_NESTING, LXC_NESTING,
)


# --- TestLoadConfig ---


class TestLoadConfig:

    def test_parses_key_value_pairs(self, tmp_path):
        f = tmp_path / "test.conf"
        f.write_text("memory = 1024\ncores = 4\n")
        result = load_config(f)
        assert result == {"memory": "1024", "cores": "4"}

    def test_skips_comments_and_blanks(self, tmp_path):
        f = tmp_path / "test.conf"
        f.write_text("# a comment\n\nmemory = 512\n\n# another\ncores = 2\n")
        result = load_config(f)
        assert result == {"memory": "512", "cores": "2"}

    def test_strips_whitespace(self, tmp_path):
        f = tmp_path / "test.conf"
        f.write_text("  memory  =  1024  \n  cores=2\n")
        result = load_config(f)
        assert result == {"memory": "1024", "cores": "2"}

    def test_returns_empty_for_missing_file(self, tmp_path):
        result = load_config(tmp_path / "nonexistent.conf")
        assert result == {}

    def test_handles_equals_in_value(self, tmp_path):
        f = tmp_path / "test.conf"
        f.write_text("env = FOO=bar\n")
        result = load_config(f)
        assert result == {"env": "FOO=bar"}


# --- TestGetVmDefaults ---


class TestGetVmDefaults:

    def test_returns_hardcoded_defaults(self):
        with patch("kento.defaults.VM_CONFIG_FILE", Path("/nonexistent/vm.conf")):
            result = get_vm_defaults()
        assert result == {
            "memory": VM_MEMORY,
            "cores": VM_CORES,
            "kvm": VM_KVM,
            "machine": VM_MACHINE,
            "serial": VM_SERIAL,
            "display": VM_DISPLAY,
        }

    def test_overrides_from_config_file(self, tmp_path):
        f = tmp_path / "vm.conf"
        f.write_text("memory = 2048\ncores = 4\nkvm = false\nmachine = pc\nserial = ttyS1\ndisplay = true\n")
        with patch("kento.defaults.VM_CONFIG_FILE", f):
            result = get_vm_defaults()
        assert result == {
            "memory": 2048,
            "cores": 4,
            "kvm": False,
            "machine": "pc",
            "serial": "ttyS1",
            "display": True,
        }

    def test_partial_override(self, tmp_path):
        f = tmp_path / "vm.conf"
        f.write_text("memory = 4096\n")
        with patch("kento.defaults.VM_CONFIG_FILE", f):
            result = get_vm_defaults()
        assert result["memory"] == 4096
        assert result["cores"] == VM_CORES
        assert result["kvm"] == VM_KVM
        assert result["machine"] == VM_MACHINE
        assert result["serial"] == VM_SERIAL
        assert result["display"] == VM_DISPLAY


# --- TestGetLxcDefaults ---


class TestGetLxcDefaults:

    def test_returns_hardcoded_defaults(self):
        with patch("kento.defaults.LXC_CONFIG_FILE", Path("/nonexistent/lxc.conf")):
            result = get_lxc_defaults()
        assert result == {
            "tty": LXC_TTY,
            "mount_auto": LXC_MOUNT_AUTO,
            "mount_auto_nesting": LXC_MOUNT_AUTO_NESTING,
            "nesting": LXC_NESTING,
        }

    def test_overrides_from_config_file(self, tmp_path):
        f = tmp_path / "lxc.conf"
        f.write_text("tty = 4\nmount_auto = proc:rw sys:rw\nmount_auto_nesting = proc:rw\nnesting = false\n")
        with patch("kento.defaults.LXC_CONFIG_FILE", f):
            result = get_lxc_defaults()
        assert result == {
            "tty": 4,
            "mount_auto": "proc:rw sys:rw",
            "mount_auto_nesting": "proc:rw",
            "nesting": False,
        }


# --- TestEnsureConfigFiles ---


class TestEnsureConfigFiles:

    def test_creates_config_dir_and_files(self, tmp_path):
        config_dir = tmp_path / "kento"
        lxc_file = config_dir / "lxc.conf"
        vm_file = config_dir / "vm.conf"

        with patch("kento.defaults.CONFIG_DIR", config_dir), \
             patch("kento.defaults.LXC_CONFIG_FILE", lxc_file), \
             patch("kento.defaults.VM_CONFIG_FILE", vm_file):
            ensure_config_files()

        assert config_dir.is_dir()
        assert lxc_file.is_file()
        assert vm_file.is_file()

    def test_does_not_overwrite_existing(self, tmp_path):
        config_dir = tmp_path / "kento"
        config_dir.mkdir()
        lxc_file = config_dir / "lxc.conf"
        vm_file = config_dir / "vm.conf"
        lxc_file.write_text("custom content\n")
        vm_file.write_text("custom vm content\n")

        with patch("kento.defaults.CONFIG_DIR", config_dir), \
             patch("kento.defaults.LXC_CONFIG_FILE", lxc_file), \
             patch("kento.defaults.VM_CONFIG_FILE", vm_file):
            ensure_config_files()

        assert lxc_file.read_text() == "custom content\n"
        assert vm_file.read_text() == "custom vm content\n"

    def test_file_contains_commented_defaults(self, tmp_path):
        config_dir = tmp_path / "kento"
        lxc_file = config_dir / "lxc.conf"
        vm_file = config_dir / "vm.conf"

        with patch("kento.defaults.CONFIG_DIR", config_dir), \
             patch("kento.defaults.LXC_CONFIG_FILE", lxc_file), \
             patch("kento.defaults.VM_CONFIG_FILE", vm_file):
            ensure_config_files()

        lxc_text = lxc_file.read_text()
        assert "# tty = 2" in lxc_text
        assert "# nesting = True" in lxc_text
        assert "# mount_auto = proc:mixed sys:mixed cgroup:mixed" in lxc_text

        vm_text = vm_file.read_text()
        assert "# memory = 512" in vm_text
        assert "# cores = 1" in vm_text
        assert "# kvm = True" in vm_text
        assert "# machine = q35" in vm_text
        assert "# serial = ttyS0" in vm_text
        assert "# display = False" in vm_text
