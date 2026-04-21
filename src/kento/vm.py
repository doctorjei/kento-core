"""Tenkei VM mode — QEMU + virtiofs VM management."""

import hashlib
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from kento import VM_BASE
from kento.defaults import VM_MEMORY, VM_KVM, VM_MACHINE

# Locally-administered MAC prefix (QEMU's standard block).
MAC_PREFIX = "52:54:00"

_MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")


def is_valid_mac(mac: str) -> bool:
    """Return True if mac is a valid colon-separated 6-pair hex MAC."""
    return bool(_MAC_RE.match(mac))


def generate_mac(identifier: str) -> str:
    """Return a stable deterministic MAC address for the given identifier.

    The identifier is the container name (plain VM) or VMID (PVE-VM).
    Uses 52:54:00 prefix + 3 bytes from sha256(identifier).
    """
    digest = hashlib.sha256(identifier.encode()).digest()[:3]
    suffix = ":".join(f"{b:02x}" for b in digest)
    return f"{MAC_PREFIX}:{suffix}"

# virtiofsd is often installed outside PATH (e.g. /usr/libexec/virtiofsd on Debian)
_VIRTIOFSD_SEARCH = ["/usr/libexec/virtiofsd", "/usr/lib/qemu/virtiofsd",
                     "/usr/lib/virtiofsd", "/usr/bin/virtiofsd"]


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


_PORT_MIN = 10022
_PORT_MAX = 10999


def _port_is_free(port: int) -> bool:
    """Check if a TCP port is available on localhost."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
            return True
    except OSError:
        return False


def allocate_port() -> int:
    """Return the next free host port in range 10022-10999.

    Scans kento-port files in both LXC and VM base directories to find used
    ports, then verifies the candidate port is actually free on the host.
    """
    from kento import LXC_BASE
    used_ports: set[int] = set()
    for base in (VM_BASE, LXC_BASE):
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
    for port in range(_PORT_MIN, _PORT_MAX + 1):
        if port not in used_ports and _port_is_free(port):
            return port
    print("Error: no free port in range 10022-10999", file=sys.stderr)
    sys.exit(1)


def is_vm_running(container_dir: Path) -> bool:
    """Check if a VM is running by verifying the QEMU PID file."""
    pid_file = container_dir / "kento-qemu-pid"
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


def mount_rootfs(container_dir: Path, layers: str, state_dir: Path) -> None:
    """Mount overlayfs at container_dir/rootfs on the host."""
    rootfs = container_dir / "rootfs"
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


def unmount_rootfs(container_dir: Path) -> None:
    """Unmount overlayfs at container_dir/rootfs."""
    rootfs = container_dir / "rootfs"
    subprocess.run(["umount", str(rootfs)], check=True)


def start_vm(container_dir: Path, name: str) -> None:
    """Start a VM: mount rootfs, launch virtiofsd + QEMU, write PID files."""
    if is_vm_running(container_dir):
        print(f"Error: VM {name} is already running", file=sys.stderr)
        sys.exit(1)

    layers = (container_dir / "kento-layers").read_text().strip()
    state_dir = Path((container_dir / "kento-state").read_text().strip())

    # Mount overlayfs
    mount_rootfs(container_dir, layers, state_dir)

    rootfs = container_dir / "rootfs"

    # Validate kernel and initramfs exist
    kernel = rootfs / "boot" / "vmlinuz"
    initramfs = rootfs / "boot" / "initramfs.img"
    if not kernel.is_file():
        unmount_rootfs(container_dir)
        print(f"Error: kernel not found at {kernel}", file=sys.stderr)
        sys.exit(1)
    if not initramfs.is_file():
        unmount_rootfs(container_dir)
        print(f"Error: initramfs not found at {initramfs}", file=sys.stderr)
        sys.exit(1)

    # Inject guest-side config (hostname/network/tz/env/ssh-key) into the
    # mounted rootfs before virtiofsd starts. inject.sh reads kento metadata
    # files and writes them into the overlayfs upper layer. A failing inject
    # is a start failure — don't boot a misconfigured VM.
    inject_script = container_dir / "kento-inject.sh"
    if not inject_script.is_file():
        unmount_rootfs(container_dir)
        print(f"Error: inject script not found at {inject_script}", file=sys.stderr)
        sys.exit(1)
    subprocess.run(
        ["sh", str(inject_script), str(rootfs), str(container_dir)],
        check=True,
    )

    # Read port mapping (usermode networking)
    port_file = container_dir / "kento-port"
    host_port = guest_port = None
    if port_file.is_file():
        port_text = port_file.read_text().strip()
        host_port, guest_port = port_text.split(":")

    # Start virtiofsd
    virtiofsd_bin = _find_virtiofsd()
    socket_path = container_dir / "virtiofsd.sock"
    virtiofsd = subprocess.Popen(
        [virtiofsd_bin,
         f"--socket-path={socket_path}",
         f"--shared-dir={rootfs}",
         "--cache=auto"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    (container_dir / "kento-virtiofsd-pid").write_text(str(virtiofsd.pid) + "\n")

    # Wait for socket to appear
    for _ in range(50):
        if socket_path.exists():
            break
        time.sleep(0.1)

    # Start QEMU
    memory = str(VM_MEMORY)
    qemu_cmd = ["qemu-system-x86_64",
         "-kernel", str(kernel),
         "-initrd", str(initramfs),
         "-m", memory,
         "-machine", VM_MACHINE,
    ]
    if VM_KVM:
        qemu_cmd += ["-enable-kvm", "-cpu", "host"]
    qemu_cmd += [
         "-nographic",
         "-chardev", f"socket,id=vfs,path={socket_path}",
         "-device", "vhost-user-fs-pci,chardev=vfs,tag=rootfs",
         "-object", f"memory-backend-memfd,id=mem,size={memory}M,share=on",
         "-numa", "node,memdev=mem",
    ]
    if host_port is not None:
        # Include MAC if available (kento-mac written at create time for VM modes)
        mac_file = container_dir / "kento-mac"
        device = "virtio-net-pci,netdev=net0"
        if mac_file.is_file():
            mac = mac_file.read_text().strip()
            if mac:
                device = f"virtio-net-pci,netdev=net0,mac={mac}"
        qemu_cmd += [
             "-netdev", f"user,id=net0,hostfwd=tcp:127.0.0.1:{host_port}-:{guest_port}",
             "-device", device,
        ]
    qemu_cmd += [
         "-append", "console=ttyS0 rootfstype=virtiofs root=rootfs",
    ]
    qemu = subprocess.Popen(
        qemu_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    (container_dir / "kento-qemu-pid").write_text(str(qemu.pid) + "\n")

    print(f"Started: {name}")
    if port_file.is_file():
        print(f"  SSH: ssh -p {host_port} root@localhost")


def _kill_and_wait(pid_file: Path, timeout: float = 5.0, *, force: bool = False) -> None:
    """Send SIGTERM (or SIGKILL if force) to a process and wait for it to exit."""
    if not pid_file.is_file():
        return
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        pid_file.unlink(missing_ok=True)
        return
    try:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
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


def stop_vm(container_dir: Path, *, force: bool = False) -> None:
    """Stop a VM: kill QEMU + virtiofsd, unmount rootfs, clean up."""
    _kill_and_wait(container_dir / "kento-qemu-pid", force=force)
    _kill_and_wait(container_dir / "kento-virtiofsd-pid", force=force)

    # Unmount rootfs
    rootfs = container_dir / "rootfs"
    if _is_mountpoint(rootfs):
        result = subprocess.run(["umount", str(rootfs)])
        if result.returncode != 0:
            print(f"Error: failed to unmount {rootfs}. Is the container still running?",
                  file=sys.stderr)
            sys.exit(1)

    # Clean up socket
    (container_dir / "virtiofsd.sock").unlink(missing_ok=True)
