"""Remove a kento-managed instance."""

import shutil
import subprocess
import sys
from pathlib import Path

from kento import is_running, read_mode, require_root, resolve_container


def destroy(name: str, force: bool = False, *, container_dir: Path | None = None, mode: str | None = None) -> None:
    require_root()

    if container_dir is None:
        container_dir = resolve_container(name)
    container_id = container_dir.name

    if mode is None:
        # Detect mode (default lxc for containers created before mode tracking)
        mode = read_mode(container_dir)

    # Read state dir before we delete anything
    state_file = container_dir / "kento-state"
    state_dir = Path(state_file.read_text().strip()) if state_file.is_file() else container_dir

    # Check if running
    running = is_running(container_dir, mode)

    if running and not force:
        print(f"Error: instance {name} is running. "
              f"Use 'kento lxc destroy -f {name}' or 'kento vm destroy -f {name}' to force removal.",
              file=sys.stderr)
        sys.exit(1)

    if running:
        print("Stopping...")
        try:
            if mode == "vm":
                from kento.vm import stop_vm
                stop_vm(container_dir, force=True)
            elif mode == "pve-vm":
                vmid = (container_dir / "kento-vmid").read_text().strip()
                subprocess.run(["qm", "stop", vmid], check=True, capture_output=True)
            elif mode == "pve":
                subprocess.run(["pct", "stop", container_id], check=True, capture_output=True)
            else:
                subprocess.run(["lxc-stop", "-n", container_id], check=True, capture_output=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            # destroy() only runs the stop-before-remove path when called with -f
            # (the non-force path errors above). Log and continue to cleanup so
            # a partially-wedged instance can still be removed.
            if isinstance(e, subprocess.CalledProcessError):
                stderr = (e.stderr or b"").decode("utf-8", "replace").strip()
                print(f"Warning: stop failed (exit {e.returncode}); proceeding with cleanup: {stderr}",
                      file=sys.stderr)
            else:
                print(f"Warning: stop tool not found ({e.filename}); proceeding with cleanup.",
                      file=sys.stderr)

    # Unmount rootfs if still mounted. Use the busy-mount-hardened helper:
    # under -f we want a wedged instance (qm timeout, leaked virtiofsd, etc.)
    # to still succeed via fuser-kill + lazy umount fallback rather than hang
    # the destroy. Without -f we keep strict semantics — fail loudly.
    rootfs = container_dir / "rootfs"
    if subprocess.run(["mountpoint", "-q", str(rootfs)],
                      capture_output=True).returncode == 0:
        from kento.vm import _umount_with_retry
        if not _umount_with_retry(rootfs, force=force):
            print(f"Error: failed to unmount {rootfs}. Is the container still running?",
                  file=sys.stderr)
            sys.exit(1)

    # Release OCI image mount
    from kento.layers import _podman_cmd
    image = (container_dir / "kento-image").read_text().strip()
    subprocess.run(
        [*_podman_cmd(), "image", "unmount", image],
        capture_output=True,
    )

    # Read vmid before deletion (needed for pve-vm cleanup)
    vmid_str = None
    if mode == "pve-vm":
        vmid_file = container_dir / "kento-vmid"
        vmid_str = vmid_file.read_text().strip() if vmid_file.is_file() else None

    # Clean up platform-specific config BEFORE removing container_dir
    if mode == "pve":
        from kento.pve import delete_pve_config
        from kento.lxc_hook import delete_lxc_snippets_wrapper
        delete_pve_config(int(container_id))
        delete_lxc_snippets_wrapper(int(container_id))
    elif mode == "pve-vm" and vmid_str:
        from kento.pve import delete_qm_config
        from kento.vm_hook import delete_snippets_wrapper
        delete_qm_config(int(vmid_str))
        delete_snippets_wrapper(int(vmid_str))

    from kento.layers import remove_image_hold
    name_file = container_dir / "kento-name"
    hold_name = name_file.read_text().strip() if name_file.is_file() else name
    remove_image_hold(hold_name)

    # Remove state dir if separate from container_dir
    if state_dir != container_dir and state_dir.is_dir():
        shutil.rmtree(state_dir)

    shutil.rmtree(container_dir)

    print(f"Removed: {name}")
