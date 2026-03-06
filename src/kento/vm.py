"""Tenkei VM mode — QEMU + virtiofs VM management."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

VM_BASE = Path("/var/lib/kento/vm")

# virtiofsd is often installed outside PATH (e.g. /usr/libexec/virtiofsd on Debian)
_VIRTIOFSD_SEARCH = ["/usr/libexec/virtiofsd", "/usr/lib/qemu/virtiofsd"]


def _find_virtiofsd() -> str:
    """Locate the virtiofsd binary."""
    import shutil
    path = shutil.which("virtiofsd")
    if path:
        return path
    for candidate in _VIRTIOFSD_SEARCH:
        if Path(candidate).is_file():
            return candidate
    print("Error: virtiofsd not found. Install virtiofsd or check PATH.", file=sys.stderr)
    sys.exit(1)


def allocate_port(scan_dir: Path | None = None) -> int:
    """Return the next free host port starting from 10022.

    Scans kento-port files in the VM base directory to find used ports.
    """
    base = scan_dir or VM_BASE
    used_ports: set[int] = set()
    if base.is_dir():
        for d in base.iterdir():
            if d.is_dir():
                port_file = d / "kento-port"
                if port_file.is_file():
                    try:
                        host_port = int(port_file.read_text().strip().split(":")[0])
                        used_ports.add(host_port)
                    except (ValueError, IndexError):
                        continue
    port = 10022
    while port in used_ports:
        port += 1
    return port


def is_vm_running(lxc_dir: Path) -> bool:
    """Check if a VM is running by verifying the QEMU PID file."""
    pid_file = lxc_dir / "kento-qemu-pid"
    if not pid_file.is_file():
        return False
    try:
        pid = int(pid_file.read_text().strip())
        return Path(f"/proc/{pid}").is_dir()
    except (ValueError, OSError):
        return False


def _is_mountpoint(path: Path) -> bool:
    """Check if a path is a mountpoint."""
    return subprocess.run(
        ["mountpoint", "-q", str(path)], capture_output=True,
    ).returncode == 0


def mount_rootfs(lxc_dir: Path, layers: str, state_dir: Path) -> None:
    """Mount overlayfs at lxc_dir/rootfs on the host."""
    rootfs = lxc_dir / "rootfs"
    if _is_mountpoint(rootfs):
        print(f"Error: rootfs already mounted at {rootfs}", file=sys.stderr)
        sys.exit(1)
    upper = state_dir / "upper"
    work = state_dir / "work"
    opts = f"lowerdir={layers},upperdir={upper},workdir={work}"
    env = {**os.environ, "LIBMOUNT_FORCE_MOUNT2": "always"}
    subprocess.run(
        ["mount", "-t", "overlay", "overlay", "-o", opts, str(rootfs)],
        env=env, check=True,
    )


def unmount_rootfs(lxc_dir: Path) -> None:
    """Unmount overlayfs at lxc_dir/rootfs."""
    rootfs = lxc_dir / "rootfs"
    subprocess.run(["umount", str(rootfs)], check=True)


def start_vm(lxc_dir: Path, name: str) -> None:
    """Start a VM: mount rootfs, launch virtiofsd + QEMU, write PID files."""
    if is_vm_running(lxc_dir):
        print(f"Error: VM {name} is already running", file=sys.stderr)
        sys.exit(1)

    layers = (lxc_dir / "kento-layers").read_text().strip()
    state_dir = Path((lxc_dir / "kento-state").read_text().strip())

    # Mount overlayfs
    mount_rootfs(lxc_dir, layers, state_dir)

    rootfs = lxc_dir / "rootfs"

    # Validate kernel and initramfs exist
    kernel = rootfs / "boot" / "vmlinuz"
    initramfs = rootfs / "boot" / "initramfs.img"
    if not kernel.is_file():
        unmount_rootfs(lxc_dir)
        print(f"Error: kernel not found at {kernel}", file=sys.stderr)
        sys.exit(1)
    if not initramfs.is_file():
        unmount_rootfs(lxc_dir)
        print(f"Error: initramfs not found at {initramfs}", file=sys.stderr)
        sys.exit(1)

    # Read port mapping
    port_text = (lxc_dir / "kento-port").read_text().strip()
    host_port, guest_port = port_text.split(":")

    # Start virtiofsd
    virtiofsd_bin = _find_virtiofsd()
    socket_path = lxc_dir / "virtiofsd.sock"
    virtiofsd = subprocess.Popen(
        [virtiofsd_bin,
         f"--socket-path={socket_path}",
         f"--shared-dir={rootfs}",
         "--cache=auto"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    (lxc_dir / "kento-virtiofsd-pid").write_text(str(virtiofsd.pid) + "\n")

    # Wait for socket to appear
    for _ in range(50):
        if socket_path.exists():
            break
        time.sleep(0.1)

    # Start QEMU
    memory = "512"
    qemu = subprocess.Popen(
        ["qemu-system-x86_64",
         "-kernel", str(kernel),
         "-initrd", str(initramfs),
         "-m", memory, "-enable-kvm", "-cpu", "host",
         "-nographic",
         "-chardev", f"socket,id=vfs,path={socket_path}",
         "-device", "vhost-user-fs-pci,chardev=vfs,tag=rootfs",
         "-object", f"memory-backend-memfd,id=mem,size={memory}M,share=on",
         "-numa", "node,memdev=mem",
         "-netdev", f"user,id=net0,hostfwd=tcp:127.0.0.1:{host_port}-:{guest_port}",
         "-device", "virtio-net-pci,netdev=net0",
         "-append", "console=ttyS0 rootfstype=virtiofs root=rootfs"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    (lxc_dir / "kento-qemu-pid").write_text(str(qemu.pid) + "\n")

    print(f"Started: {name}")
    print(f"  SSH: ssh -p {host_port} root@localhost")


def _kill_and_wait(pid_file: Path, timeout: float = 5.0) -> None:
    """Send SIGTERM to a process and wait for it to exit."""
    if not pid_file.is_file():
        return
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        pid_file.unlink(missing_ok=True)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pid_file.unlink(missing_ok=True)
        return
    # Wait for process to exit
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not Path(f"/proc/{pid}").is_dir():
            break
        time.sleep(0.1)
    else:
        # Still alive — force kill
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    pid_file.unlink(missing_ok=True)


def stop_vm(lxc_dir: Path) -> None:
    """Stop a VM: kill QEMU + virtiofsd, unmount rootfs, clean up."""
    _kill_and_wait(lxc_dir / "kento-qemu-pid")
    _kill_and_wait(lxc_dir / "kento-virtiofsd-pid")

    # Unmount rootfs
    rootfs = lxc_dir / "rootfs"
    if _is_mountpoint(rootfs):
        subprocess.run(["umount", str(rootfs)])

    # Clean up socket
    (lxc_dir / "virtiofsd.sock").unlink(missing_ok=True)
