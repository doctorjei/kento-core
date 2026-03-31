"""Tests for VM hookscript generation."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from kento.vm_hook import (
    generate_vm_hook, generate_snippets_wrapper, write_vm_hook,
    find_snippets_dir, write_snippets_wrapper, delete_snippets_wrapper,
)


class TestGenerateVmHook:
    def test_contains_baked_paths(self):
        hook = generate_vm_hook(
            Path("/var/lib/kento/vm/test"),
            "/a:/b",
            "test",
            Path("/var/lib/kento/vm/test"),
        )
        assert 'NAME="test"' in hook
        assert 'CONTAINER_DIR="/var/lib/kento/vm/test"' in hook
        assert 'LAYERS="/a:/b"' in hook

    def test_contains_memory_validation(self):
        hook = generate_vm_hook(Path("/d"), "/a:/b", "x", Path("/d"))
        assert "memory mismatch" in hook
        assert "CONF_MEM" in hook

    def test_contains_overlayfs_mount(self):
        hook = generate_vm_hook(Path("/d"), "/a:/b", "x", Path("/d"))
        assert "LIBMOUNT_FORCE_MOUNT2=always" in hook
        assert "mount -t overlay" in hook

    def test_contains_virtiofsd_start(self):
        hook = generate_vm_hook(Path("/d"), "/a:/b", "x", Path("/d"))
        assert "virtiofsd" in hook.lower() or "VIRTIOFSD" in hook
        assert "--socket-path=" in hook
        assert "--shared-dir=" in hook

    def test_contains_post_stop_cleanup(self):
        hook = generate_vm_hook(Path("/d"), "/a:/b", "x", Path("/d"))
        assert "post-stop" in hook
        assert "umount" in hook
        assert "virtiofsd.sock" in hook

    def test_contains_layer_validation(self):
        hook = generate_vm_hook(Path("/d"), "/a:/b", "x", Path("/d"))
        assert "layer path missing" in hook

    def test_contains_kernel_validation(self):
        hook = generate_vm_hook(Path("/d"), "/a:/b", "x", Path("/d"))
        assert "vmlinuz" in hook
        assert "initramfs" in hook

    def test_is_executable_shell_script(self):
        hook = generate_vm_hook(Path("/d"), "/a:/b", "x", Path("/d"))
        assert hook.startswith("#!/bin/sh")

    def test_receives_vmid_and_phase(self):
        hook = generate_vm_hook(Path("/d"), "/a:/b", "x", Path("/d"))
        assert 'VMID="$1"' in hook
        assert 'PHASE="$2"' in hook


class TestGenerateSnippetsWrapper:
    def test_wrapper_execs_hook(self):
        wrapper = generate_snippets_wrapper("/var/lib/kento/vm/test/kento-hook")
        assert "exec" in wrapper
        assert "/var/lib/kento/vm/test/kento-hook" in wrapper

    def test_is_shell_script(self):
        wrapper = generate_snippets_wrapper("/path/to/hook")
        assert wrapper.startswith("#!/bin/sh")

    def test_passes_args(self):
        wrapper = generate_snippets_wrapper("/path/to/hook")
        assert '"$@"' in wrapper


class TestWriteVmHook:
    def test_writes_and_marks_executable(self, tmp_path):
        hook_path = write_vm_hook(tmp_path, "/a:/b", "test", tmp_path)
        assert hook_path == tmp_path / "kento-hook"
        assert hook_path.is_file()
        assert hook_path.stat().st_mode & 0o755 == 0o755
        content = hook_path.read_text()
        assert "test" in content


class TestFindSnippetsDir:
    def test_from_config_file(self, tmp_path):
        config = tmp_path / "vm.conf"
        config.write_text("snippets_storage = local\n")

        with patch("kento.vm_hook.VM_CONFIG_FILE", config), \
             patch("kento.vm_hook.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="/var/lib/vz/snippets/probe\n",
            )
            path, name = find_snippets_dir()

        assert name == "local"
        assert path == Path("/var/lib/vz/snippets")

    def test_from_storage_cfg(self, tmp_path):
        config = tmp_path / "vm.conf"
        config.write_text("")

        storage_cfg = tmp_path / "storage.cfg"
        storage_cfg.write_text(
            "dir: local\n"
            "\tpath /var/lib/vz\n"
            "\tcontent images,snippets,iso\n"
        )

        with patch("kento.vm_hook.VM_CONFIG_FILE", config), \
             patch("kento.vm_hook._STORAGE_CFG", storage_cfg), \
             patch("kento.vm_hook.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="/var/lib/vz/snippets/probe\n",
            )
            path, name = find_snippets_dir()

        assert name == "local"
        assert path == Path("/var/lib/vz/snippets")

    def test_no_snippets_storage_errors(self, tmp_path):
        config = tmp_path / "vm.conf"
        config.write_text("")
        storage_cfg = tmp_path / "storage.cfg"
        # No snippets in content
        storage_cfg.write_text(
            "dir: local\n"
            "\tpath /var/lib/vz\n"
            "\tcontent images,iso\n"
        )

        with patch("kento.vm_hook.VM_CONFIG_FILE", config), \
             patch("kento.vm_hook._STORAGE_CFG", storage_cfg):
            with pytest.raises(SystemExit):
                find_snippets_dir()


class TestWriteSnippetsWrapper:
    def test_writes_wrapper_and_returns_ref(self, tmp_path):
        snippets = tmp_path / "snippets"
        snippets.mkdir()
        hook_path = Path("/var/lib/kento/vm/test/kento-hook")

        with patch("kento.vm_hook.find_snippets_dir", return_value=(snippets, "local")):
            ref = write_snippets_wrapper(100, hook_path)

        assert ref == "local:snippets/kento-vm-100.sh"
        wrapper = snippets / "kento-vm-100.sh"
        assert wrapper.is_file()
        assert wrapper.stat().st_mode & 0o755 == 0o755
        assert str(hook_path) in wrapper.read_text()


class TestDeleteSnippetsWrapper:
    def test_deletes_wrapper(self, tmp_path):
        snippets = tmp_path / "snippets"
        snippets.mkdir()
        wrapper = snippets / "kento-vm-100.sh"
        wrapper.write_text("#!/bin/sh\n")

        with patch("kento.vm_hook.find_snippets_dir", return_value=(snippets, "local")):
            delete_snippets_wrapper(100)

        assert not wrapper.exists()

    def test_missing_wrapper_is_noop(self, tmp_path):
        snippets = tmp_path / "snippets"
        snippets.mkdir()

        with patch("kento.vm_hook.find_snippets_dir", return_value=(snippets, "local")):
            delete_snippets_wrapper(999)  # should not raise

    def test_no_snippets_storage_is_noop(self):
        with patch("kento.vm_hook.find_snippets_dir", side_effect=SystemExit(1)):
            delete_snippets_wrapper(100)  # should not raise
