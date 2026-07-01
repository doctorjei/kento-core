"""Tests for VM hookscript generation."""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from kento.vm_hook import (
    generate_vm_hook, generate_snippets_wrapper, write_vm_hook,
    find_snippets_dir, write_snippets_wrapper, delete_snippets_wrapper,
)
from kento.errors import StateError, SubprocessError


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

    def test_non_store_layers_fall_back_to_absolute(self):
        hook = generate_vm_hook(Path("/d"), "/a:/b", "x", Path("/d"))
        assert 'OVERLAY_BASE="/"' in hook
        assert 'LAYERS="/a:/b"' in hook

    def test_uses_chdir_relative_short_links(self, tmp_path):
        """A podman-store image must bake OVERLAY_BASE + relative l/<short>
        lowerdir and cd into the base in a subshell (kernel 4096-byte limit)."""
        root = tmp_path / "var/lib/containers/storage/overlay"
        paths = []
        for lid, short in [("aaaa", "SHORTAAAAAAAAAAAAAAAAAAAAAA"),
                           ("bbbb", "SHORTBBBBBBBBBBBBBBBBBBBBBB")]:
            (root / lid / "diff").mkdir(parents=True)
            (root / lid / "link").write_text(short + "\n")
            paths.append(str(root / lid / "diff"))
        layers = ":".join(paths)
        hook = generate_vm_hook(Path("/d"), layers, "x", Path("/d"))
        assert f'OVERLAY_BASE="{root}"' in hook
        assert 'LAYERS="l/SHORTAAAAAAAAAAAAAAAAAAAAAA:l/SHORTBBBBBBBBBBBBBBBBBBBBBB"' in hook
        assert 'cd "$OVERLAY_BASE"' in hook
        assert f"{root}/aaaa/diff" not in hook

    def test_mount_in_subshell_before_virtiofsd(self):
        """The validation loop + mount run inside a subshell so the chdir into
        OVERLAY_BASE never leaks to inject.sh / virtiofsd."""
        hook = generate_vm_hook(Path("/d"), "/a:/b", "x", Path("/d"))
        cd_idx = hook.index('cd "$OVERLAY_BASE"')
        mount_idx = hook.index("mount -t overlay")
        close_idx = hook.index("overlay mount failed")
        inject_idx = hook.index("kento-inject.sh")
        virtiofsd_idx = hook.index("$VIRTIOFSD --socket-path=")
        assert cd_idx < mount_idx < close_idx < inject_idx < virtiofsd_idx

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

    def test_post_stop_guards_numeric_vfs_pid(self):
        """An empty/garbage kento-virtiofsd-pid must not make '[ -d /proc/$VFS_PID ]'
        test '/proc/' (always true) and stall the post-stop hook for 5s. The
        generated script must blank a non-numeric VFS_PID and only enter the
        kill/wait block when it is set."""
        hook = generate_vm_hook(Path("/d"), "/a:/b", "x", Path("/d"))
        # Numeric guard: blanks VFS_PID unless it is all digits.
        assert 'case "$VFS_PID" in' in hook
        assert "''|*[!0-9]*) VFS_PID='' ;;" in hook
        # Kill/wait block only entered for a non-empty (numeric) pid.
        assert '[ -n "$VFS_PID" ] && [ -d "/proc/$VFS_PID" ]' in hook

    def test_contains_layer_validation(self):
        hook = generate_vm_hook(Path("/d"), "/a:/b", "x", Path("/d"))
        assert "layer path missing" in hook

    def test_contains_kernel_validation(self):
        hook = generate_vm_hook(Path("/d"), "/a:/b", "x", Path("/d"))
        assert "vmlinuz" in hook
        assert "initramfs" in hook

    def test_kernel_override_resolution_shell_twin(self):
        """FIX-2: the hook is the shell twin of vm.resolve_boot_sources — it
        must resolve the kernel/initramfs from the kento-kernel / kento-initramfs
        override markers when present, else the in-image rootfs/boot/* default,
        each side independent, then validate the RESOLVED path."""
        hook = generate_vm_hook(Path("/d"), "/a:/b", "x", Path("/d"))
        # In-image defaults seed the two vars.
        assert 'KERNEL="$ROOTFS/boot/vmlinuz"' in hook
        assert 'INITRAMFS="$ROOTFS/boot/initramfs.img"' in hook
        # Per-side override read from the markers (mirrors resolve_boot_sources).
        assert '[ -f "$CONTAINER_DIR/kento-kernel" ] && KERNEL=$(cat "$CONTAINER_DIR/kento-kernel")' in hook
        assert '[ -f "$CONTAINER_DIR/kento-initramfs" ] && INITRAMFS=$(cat "$CONTAINER_DIR/kento-initramfs")' in hook
        # Validation + error messages reference the RESOLVED path, not the
        # hardcoded in-image one.
        assert '[ ! -f "$KERNEL" ]' in hook
        assert '[ ! -f "$INITRAMFS" ]' in hook
        assert 'kernel not found at $KERNEL' in hook
        assert 'initramfs not found at $INITRAMFS' in hook
        # umount-on-failure preserved for both sides.
        assert hook.count('umount "$ROOTFS" 2>/dev/null || true') >= 2
        # Drift-guard comment points at the Python source of truth.
        assert "resolve_boot_sources" in hook

    def test_is_executable_shell_script(self):
        hook = generate_vm_hook(Path("/d"), "/a:/b", "x", Path("/d"))
        assert hook.startswith("#!/bin/sh")

    def test_receives_vmid_and_phase(self):
        hook = generate_vm_hook(Path("/d"), "/a:/b", "x", Path("/d"))
        assert 'VMID="$1"' in hook
        assert 'PHASE="$2"' in hook

    def test_contains_inject_call(self):
        """Hookscript must invoke kento-inject.sh with $ROOTFS and $CONTAINER_DIR."""
        hook = generate_vm_hook(Path("/d"), "/a:/b", "x", Path("/d"))
        assert 'sh "$CONTAINER_DIR/kento-inject.sh" "$ROOTFS" "$CONTAINER_DIR"' in hook

    def test_virtiofsd_stdio_redirected(self):
        """virtiofsd must redirect stdio so PVE hookscript pipes close."""
        hook = generate_vm_hook(Path("/d"), "/a:/b", "x", Path("/d"))
        assert '</dev/null >' in hook
        assert 'virtiofsd.log' in hook

    def test_inject_call_between_mount_and_virtiofsd(self):
        """inject.sh must be called after overlayfs mount but before virtiofsd starts."""
        hook = generate_vm_hook(Path("/d"), "/a:/b", "x", Path("/d"))
        mount_idx = hook.index("mount -t overlay")
        inject_idx = hook.index('sh "$CONTAINER_DIR/kento-inject.sh"')
        # virtiofsd launch is the `$VIRTIOFSD --socket-path=` line (the actual
        # invocation), not the search loop above it.
        virtiofsd_idx = hook.index('$VIRTIOFSD --socket-path=')
        assert mount_idx < inject_idx < virtiofsd_idx

    def test_overlay_mount_failure_is_actionable(self):
        """The overlay-mount-failure handler must emit the same generous,
        actionable diagnostic hook.sh emits — naming the writable upperdir, its
        fstype, the tmpfile+RENAME_WHITEOUT cause (virtiofs), and the tmpfs /
        ext4-or-xfs / KENTO_STATE_DIR remediation — while keeping the existing
        'overlay mount failed' landmark line."""
        hook = generate_vm_hook(Path("/d"), "/a:/b", "x", Path("/d"))
        # Landmark line 1 preserved (other tests index on this).
        assert "overlay mount failed" in hook
        # fstype detection via the -T (target) form, degrading to "unknown".
        assert 'findmnt -no FSTYPE -T "$STATE_DIR"' in hook
        assert '_upper_fstype="unknown"' in hook
        # Names the writable layer and reports its fstype inline.
        assert "$STATE_DIR/upper" in hook
        assert "on $_upper_fstype" in hook
        # Cause: tmpfile + RENAME_WHITEOUT, e.g. virtiofs; honest "may not".
        assert "RENAME_WHITEOUT" in hook
        assert "virtiofs" in hook
        assert "may not support" in hook
        # Remediation: tmpfs or a real (ext4/xfs) fs, or KENTO_STATE_DIR.
        assert "tmpfs" in hook
        assert "ext4/xfs" in hook
        assert "KENTO_STATE_DIR" in hook
        # Still a hard failure.
        assert "exit 1" in hook

    def test_generated_script_is_valid_posix_shell(self, tmp_path):
        """The hookscript is built from a Python f-string with doubled braces
        ({{ }}) for literal shell braces; a mis-escaped brace would either break
        the f-string at import or emit invalid shell. Write the generated script
        and `sh -n` it to prove it parses as valid POSIX shell."""
        hook = generate_vm_hook(
            Path("/var/lib/kento/vm/test"), "/a:/b", "test",
            Path("/var/lib/kento/vm/test"),
        )
        script = tmp_path / "kento-hook"
        script.write_text(hook)
        result = subprocess.run(
            ["sh", "-n", str(script)], capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"generated hookscript failed `sh -n`:\n{result.stderr}"
        )


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
            with pytest.raises(StateError):
                find_snippets_dir()

    def test_no_snippets_error_message_actionable(self, tmp_path):
        """Error message includes pvesm set command when dir storage found."""
        config = tmp_path / "vm.conf"
        config.write_text("")
        storage_cfg = tmp_path / "storage.cfg"
        storage_cfg.write_text(
            "dir: local\n"
            "\tpath /var/lib/vz\n"
            "\tcontent iso,vztmpl,backup\n"
        )

        with patch("kento.vm_hook.VM_CONFIG_FILE", config), \
             patch("kento.vm_hook._STORAGE_CFG", storage_cfg):
            with pytest.raises(StateError, match="pvesm set local --content iso,vztmpl,backup,snippets"):
                find_snippets_dir()

        # Also check the vm.conf hint is in the message
        with patch("kento.vm_hook.VM_CONFIG_FILE", config), \
             patch("kento.vm_hook._STORAGE_CFG", storage_cfg):
            with pytest.raises(StateError, match="/etc/kento/vm.conf"):
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


    def test_writes_wrapper_with_preresolved_params(self, tmp_path):
        """write_snippets_wrapper accepts pre-resolved snippets_dir and storage_name."""
        snippets = tmp_path / "snippets"
        snippets.mkdir()
        hook_path = Path("/var/lib/kento/vm/test/kento-hook")

        ref = write_snippets_wrapper(100, hook_path,
                                      snippets_dir=snippets,
                                      storage_name="mystore")

        assert ref == "mystore:snippets/kento-vm-100.sh"
        wrapper = snippets / "kento-vm-100.sh"
        assert wrapper.is_file()


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
        with patch("kento.vm_hook.find_snippets_dir", side_effect=StateError("no snippets storage")):
            delete_snippets_wrapper(100)  # should not raise
