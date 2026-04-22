"""Tests for PVE-LXC snippets hookscript wrapper."""

from pathlib import Path
from unittest.mock import patch

import pytest

from kento.lxc_hook import (
    generate_lxc_snippets_wrapper,
    write_lxc_snippets_wrapper,
    delete_lxc_snippets_wrapper,
)


class TestGenerateLxcSnippetsWrapper:
    def test_is_shell_script(self):
        wrapper = generate_lxc_snippets_wrapper(Path("/path/to/hook"))
        assert wrapper.startswith("#!/bin/sh")

    def test_embeds_hook_path(self):
        wrapper = generate_lxc_snippets_wrapper(
            Path("/var/lib/kento/pve/100/kento-hook"))
        assert "/var/lib/kento/pve/100/kento-hook" in wrapper

    def test_has_post_start_case(self):
        wrapper = generate_lxc_snippets_wrapper(Path("/h"))
        assert "post-start)" in wrapper

    def test_has_post_stop_case(self):
        wrapper = generate_lxc_snippets_wrapper(Path("/h"))
        assert "post-stop)" in wrapper

    def test_post_start_execs_hook_with_start_host(self):
        wrapper = generate_lxc_snippets_wrapper(Path("/h"))
        assert 'exec "/h" "$VMID" "" "start-host"' in wrapper

    def test_post_stop_execs_hook_with_post_stop(self):
        wrapper = generate_lxc_snippets_wrapper(Path("/h"))
        assert 'exec "/h" "$VMID" "" "post-stop"' in wrapper

    def test_receives_vmid_and_phase(self):
        wrapper = generate_lxc_snippets_wrapper(Path("/h"))
        assert 'VMID="$1"' in wrapper
        assert 'PHASE="$2"' in wrapper

    def test_other_phases_exit_zero(self):
        wrapper = generate_lxc_snippets_wrapper(Path("/h"))
        assert "exit 0" in wrapper


class TestWriteLxcSnippetsWrapper:
    def test_writes_wrapper_with_preresolved_params(self, tmp_path):
        snippets = tmp_path / "snippets"
        snippets.mkdir()
        hook_path = Path("/var/lib/kento/pve/100/kento-hook")

        ref = write_lxc_snippets_wrapper(100, hook_path,
                                          snippets_dir=snippets,
                                          storage_name="local")

        assert ref == "local:snippets/kento-lxc-100.sh"
        wrapper = snippets / "kento-lxc-100.sh"
        assert wrapper.is_file()
        assert wrapper.stat().st_mode & 0o755 == 0o755
        assert str(hook_path) in wrapper.read_text()

    def test_uses_custom_storage_name(self, tmp_path):
        snippets = tmp_path / "snippets"
        snippets.mkdir()
        hook_path = Path("/var/lib/kento/pve/200/kento-hook")

        ref = write_lxc_snippets_wrapper(200, hook_path,
                                          snippets_dir=snippets,
                                          storage_name="mystore")

        assert ref == "mystore:snippets/kento-lxc-200.sh"
        wrapper = snippets / "kento-lxc-200.sh"
        assert wrapper.is_file()

    def test_calls_find_snippets_dir_when_not_provided(self, tmp_path):
        snippets = tmp_path / "snippets"
        snippets.mkdir()
        hook_path = Path("/var/lib/kento/pve/300/kento-hook")

        with patch("kento.vm_hook.find_snippets_dir",
                   return_value=(snippets, "local")):
            ref = write_lxc_snippets_wrapper(300, hook_path)

        assert ref == "local:snippets/kento-lxc-300.sh"
        wrapper = snippets / "kento-lxc-300.sh"
        assert wrapper.is_file()
        assert wrapper.stat().st_mode & 0o755 == 0o755
        assert str(hook_path) in wrapper.read_text()

    def test_filename_uses_lxc_infix(self, tmp_path):
        """Filenames must use -lxc- to avoid collision with vm wrappers."""
        snippets = tmp_path / "snippets"
        snippets.mkdir()
        hook_path = Path("/h")

        write_lxc_snippets_wrapper(100, hook_path,
                                    snippets_dir=snippets,
                                    storage_name="local")

        assert (snippets / "kento-lxc-100.sh").exists()
        assert not (snippets / "kento-vm-100.sh").exists()


class TestDeleteLxcSnippetsWrapper:
    def test_deletes_wrapper(self, tmp_path):
        snippets = tmp_path / "snippets"
        snippets.mkdir()
        wrapper = snippets / "kento-lxc-100.sh"
        wrapper.write_text("#!/bin/sh\n")

        with patch("kento.vm_hook.find_snippets_dir",
                   return_value=(snippets, "local")):
            delete_lxc_snippets_wrapper(100)

        assert not wrapper.exists()

    def test_missing_wrapper_is_noop(self, tmp_path):
        snippets = tmp_path / "snippets"
        snippets.mkdir()

        with patch("kento.vm_hook.find_snippets_dir",
                   return_value=(snippets, "local")):
            delete_lxc_snippets_wrapper(999)  # should not raise

    def test_no_snippets_storage_is_noop(self):
        with patch("kento.vm_hook.find_snippets_dir",
                   side_effect=SystemExit(1)):
            delete_lxc_snippets_wrapper(100)  # should not raise
