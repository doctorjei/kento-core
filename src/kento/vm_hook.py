"""Generate per-VM PVE hookscripts (qm hookscripts)."""

import subprocess
import sys
from pathlib import Path

from kento.defaults import VM_CONFIG_FILE, load_config

_STORAGE_CFG = Path("/etc/pve/storage.cfg")


def generate_vm_hook(container_dir: Path, layers: str, name: str,
                     state_dir: Path) -> str:
    """Return a hookscript with baked-in paths for a PVE VM.

    The script receives VMID as $1 and phase as $2 from qm.
    """
    return f"""#!/bin/sh
set -eu

VMID="$1"
PHASE="$2"

NAME="{name}"
CONTAINER_DIR="{container_dir}"
STATE_DIR="{state_dir}"
LAYERS="{layers}"

case "$PHASE" in
    pre-start)
        # 1. Validate memory consistency
        QM_CONF="/etc/pve/qemu-server/${{VMID}}.conf"
        if [ -f "$QM_CONF" ]; then
            CONF_MEM=$(sed -n 's/^memory: *//p' "$QM_CONF")
            ARGS_MEM=$(sed -n 's/.*size=\\([0-9]*\\)M.*/\\1/p' "$QM_CONF")
            if [ -n "$CONF_MEM" ] && [ -n "$ARGS_MEM" ]; then
                if [ "$CONF_MEM" != "$ARGS_MEM" ]; then
                    echo "Error: memory mismatch — memory: ${{CONF_MEM}}M but memfd size=${{ARGS_MEM}}M in args." >&2
                    echo "Run: kento vm scrub $NAME to regenerate config with matching values." >&2
                    exit 1
                fi
            fi
        fi

        # 2. Validate layer paths
        IFS=:
        for dir in $LAYERS; do
            if [ ! -d "$dir" ]; then
                echo "Error: layer path missing: $dir" >&2
                echo "Image may have changed. Run: kento vm scrub $NAME" >&2
                exit 1
            fi
        done
        unset IFS

        # 3. Mount overlayfs
        ROOTFS="$CONTAINER_DIR/rootfs"
        mkdir -p "$STATE_DIR/upper" "$STATE_DIR/work" "$ROOTFS"
        export LIBMOUNT_FORCE_MOUNT2=always
        mount -t overlay overlay \\
            -o "lowerdir=$LAYERS,upperdir=$STATE_DIR/upper,workdir=$STATE_DIR/work" \\
            "$ROOTFS"

        # 4. Validate kernel and initramfs
        if [ ! -f "$ROOTFS/boot/vmlinuz" ]; then
            umount "$ROOTFS" 2>/dev/null || true
            echo "Error: kernel not found at $ROOTFS/boot/vmlinuz" >&2
            exit 1
        fi
        if [ ! -f "$ROOTFS/boot/initramfs.img" ]; then
            umount "$ROOTFS" 2>/dev/null || true
            echo "Error: initramfs not found at $ROOTFS/boot/initramfs.img" >&2
            exit 1
        fi

        # 5. Inject guest-side config (hostname/network/tz/env/ssh-key) into
        # the mounted rootfs before virtiofsd starts. A failing inject is a
        # start failure — don't boot a misconfigured VM. set -eu at the top
        # causes the hookscript to abort on non-zero exit, which PVE treats
        # as a VM start failure.
        sh "$CONTAINER_DIR/kento-inject.sh" "$ROOTFS" "$CONTAINER_DIR"

        # 6. Find and start virtiofsd
        VIRTIOFSD=""
        for p in virtiofsd /usr/libexec/virtiofsd /usr/lib/qemu/virtiofsd /usr/lib/virtiofsd /usr/bin/virtiofsd; do
            if command -v "$p" >/dev/null 2>&1 || [ -x "$p" ]; then
                VIRTIOFSD="$p"
                break
            fi
        done
        if [ -z "$VIRTIOFSD" ]; then
            umount "$ROOTFS" 2>/dev/null || true
            echo "Error: virtiofsd not found" >&2
            exit 1
        fi

        SOCKET="$CONTAINER_DIR/virtiofsd.sock"
        setsid $VIRTIOFSD --socket-path="$SOCKET" --shared-dir="$ROOTFS" --cache=auto \
            </dev/null >"$CONTAINER_DIR/virtiofsd.log" 2>&1 &
        VFS_PID=$!
        echo "$VFS_PID" > "$CONTAINER_DIR/kento-virtiofsd-pid"

        # 7. Wait for socket (5s timeout)
        TRIES=50
        while [ $TRIES -gt 0 ]; do
            [ -e "$SOCKET" ] && break
            sleep 0.1
            TRIES=$((TRIES - 1))
        done
        if [ ! -e "$SOCKET" ]; then
            kill "$VFS_PID" 2>/dev/null || true
            umount "$ROOTFS" 2>/dev/null || true
            echo "Error: virtiofsd socket did not appear at $SOCKET" >&2
            exit 1
        fi
        ;;

    post-stop)
        # 1. Kill virtiofsd
        PID_FILE="$CONTAINER_DIR/kento-virtiofsd-pid"
        if [ -f "$PID_FILE" ]; then
            VFS_PID=$(cat "$PID_FILE")
            if [ -d "/proc/$VFS_PID" ]; then
                kill "$VFS_PID" 2>/dev/null || true
                # Wait up to 5s for exit
                TRIES=50
                while [ $TRIES -gt 0 ] && [ -d "/proc/$VFS_PID" ]; do
                    sleep 0.1
                    TRIES=$((TRIES - 1))
                done
                # Force kill if still alive
                if [ -d "/proc/$VFS_PID" ]; then
                    kill -9 "$VFS_PID" 2>/dev/null || true
                fi
            fi
            rm -f "$PID_FILE"
        fi

        # 2. Unmount overlayfs
        ROOTFS="$CONTAINER_DIR/rootfs"
        mountpoint -q "$ROOTFS" 2>/dev/null && umount "$ROOTFS" || true

        # 3. Clean up socket
        rm -f "$CONTAINER_DIR/virtiofsd.sock"
        ;;
esac
"""


def generate_snippets_wrapper(hook_path: str) -> str:
    """Return a thin wrapper script that forwards to the real hook."""
    return f"""#!/bin/sh
exec "{hook_path}" "$@"
"""


def write_vm_hook(container_dir: Path, layers: str, name: str,
                  state_dir: Path) -> Path:
    """Generate and write the VM hook script into the container directory."""
    hook_path = container_dir / "kento-hook"
    hook_path.write_text(generate_vm_hook(container_dir, layers, name, state_dir))
    hook_path.chmod(0o755)
    return hook_path


def find_snippets_dir() -> tuple[Path, str]:
    """Find the PVE snippets directory and storage name.

    Returns (snippets_path, storage_name).
    """
    config = load_config(VM_CONFIG_FILE)
    storage_name = config.get("snippets_storage")

    if not storage_name:
        first_dir_storage = None
        first_dir_content = None
        if _STORAGE_CFG.is_file():
            current_storage = None
            current_type = None
            for line in _STORAGE_CFG.read_text().splitlines():
                stripped = line.strip()
                if stripped.startswith(("dir:", "nfs:", "cifs:", "glusterfs:",
                                       "zfspool:", "btrfs:", "lvmthin:", "lvm:")):
                    current_type = stripped.split(":")[0]
                    current_storage = stripped.split(":", 1)[1].strip().split()[0]
                elif stripped.startswith("content") and current_storage:
                    content = stripped.split(None, 1)[1] if len(stripped.split(None, 1)) > 1 else ""
                    if "snippets" in content.split(","):
                        storage_name = current_storage
                        break
                    if first_dir_storage is None and current_type == "dir":
                        first_dir_storage = current_storage
                        first_dir_content = content.strip()

        if not storage_name:
            msg = "Error: no PVE storage has 'snippets' in its content types.\n"
            if first_dir_storage and first_dir_content:
                msg += (f"\nEnable snippets on your '{first_dir_storage}' storage:\n"
                        f"  pvesm set {first_dir_storage} --content "
                        f"{first_dir_content},snippets\n")
            else:
                msg += ("\nEnable snippets on a storage (e.g. 'local'):\n"
                        "  pvesm set local --content iso,vztmpl,backup,snippets\n")
            msg += (f"\nOr set a specific storage in /etc/kento/vm.conf:\n"
                    f"  snippets_storage = <name>\n")
            print(msg, file=sys.stderr)
            sys.exit(1)

    # Resolve the filesystem path via pvesm
    result = subprocess.run(
        ["pvesm", "path", f"{storage_name}:snippets/probe"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Error: failed to resolve snippets path for storage '{storage_name}': "
              f"{result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    # pvesm path returns something like /var/lib/vz/snippets/probe
    # Strip the filename to get the directory
    snippets_path = Path(result.stdout.strip()).parent
    return snippets_path, storage_name


def write_snippets_wrapper(vmid: int, hook_path: Path, *,
                           snippets_dir: Path | None = None,
                           storage_name: str | None = None) -> str:
    """Write a snippets wrapper and return the PVE storage reference.

    Returns e.g. "local:snippets/kento-vm-100.sh"
    """
    if snippets_dir is None or storage_name is None:
        snippets_dir, storage_name = find_snippets_dir()
    wrapper_name = f"kento-vm-{vmid}.sh"
    wrapper_path = snippets_dir / wrapper_name
    wrapper_path.write_text(generate_snippets_wrapper(str(hook_path)))
    wrapper_path.chmod(0o755)
    return f"{storage_name}:snippets/{wrapper_name}"


def delete_snippets_wrapper(vmid: int) -> None:
    """Delete the snippets wrapper for a given VMID."""
    try:
        snippets_dir, _ = find_snippets_dir()
        wrapper = snippets_dir / f"kento-vm-{vmid}.sh"
        wrapper.unlink(missing_ok=True)
    except SystemExit:
        pass  # No snippets storage = nothing to clean up
