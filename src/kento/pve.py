"""Proxmox VE integration — VMID allocation and PVE config generation."""

import json
import socket
import sys
from pathlib import Path

from kento.defaults import LXC_TTY, LXC_MOUNT_AUTO, LXC_MOUNT_AUTO_NESTING

PVE_DIR = Path("/etc/pve")
PVE_LXC_DIR = PVE_DIR / "lxc"
PVE_QEMU_DIR = PVE_DIR / "qemu-server"


def is_pve() -> bool:
    """Return True if running on a Proxmox VE host."""
    return PVE_DIR.is_dir()


def _used_vmids() -> set[int]:
    """Return the set of VMIDs already in use.

    Fast path: reads /etc/pve/.vmlist (JSON index).
    Fallback: scans /etc/pve/lxc/*.conf and /etc/pve/qemu-server/*.conf.
    """
    vmlist = PVE_DIR / ".vmlist"
    if vmlist.is_file():
        try:
            data = json.loads(vmlist.read_text())
            return {int(k) for k in data.get("ids", {})}
        except (json.JSONDecodeError, ValueError):
            pass

    # Fallback: scan config directories
    ids: set[int] = set()
    for d in (PVE_LXC_DIR, PVE_QEMU_DIR):
        if d.is_dir():
            for f in d.glob("*.conf"):
                try:
                    ids.add(int(f.stem))
                except ValueError:
                    continue
    return ids


def next_vmid() -> int:
    """Return the lowest free VMID >= 100."""
    used = _used_vmids()
    vmid = 100
    while vmid in used:
        vmid += 1
    return vmid


def validate_vmid(vmid: int) -> None:
    """Exit with error if VMID is invalid or already taken."""
    if vmid < 100:
        print(f"Error: VMID must be >= 100, got {vmid}", file=sys.stderr)
        sys.exit(1)
    used = _used_vmids()
    if vmid in used:
        print(f"Error: VMID {vmid} is already in use", file=sys.stderr)
        sys.exit(1)


def write_pve_config(vmid: int, content: str) -> Path:
    """Write a PVE config to /etc/pve/nodes/<node>/lxc/<VMID>.conf.

    pmxcfs (the PVE cluster filesystem) requires directories to be created
    one level at a time — os.makedirs() doesn't work because stat() returns
    ENOENT on empty virtual directories while mkdir() returns EEXIST.
    """
    node = socket.gethostname()
    conf_dir = PVE_DIR / "nodes" / node / "lxc"
    # Create each directory level, ignoring EEXIST (pmxcfs quirk)
    for parent in [PVE_DIR / "nodes", PVE_DIR / "nodes" / node, conf_dir]:
        try:
            parent.mkdir()
        except FileExistsError:
            pass
    conf_path = conf_dir / f"{vmid}.conf"
    conf_path.write_text(content)
    return conf_path


def delete_pve_config(vmid: int) -> None:
    """Delete a PVE config from /etc/pve/nodes/<node>/lxc/<VMID>.conf.

    No error if the file doesn't exist (idempotent).
    """
    node = socket.gethostname()
    conf_path = PVE_DIR / "nodes" / node / "lxc" / f"{vmid}.conf"
    conf_path.unlink(missing_ok=True)


def generate_pve_config(name: str, vmid: int, container_dir: Path, *,
                        bridge: str | None = None, net_type: str | None = None,
                        nesting: bool = True,
                        ip: str | None = None,
                        gateway: str | None = None,
                        nameserver: str | None = None,
                        searchdomain: str | None = None,
                        timezone: str | None = None,
                        env: list[str] | None = None) -> str:
    """Generate a PVE-format LXC config for /etc/pve/lxc/<VMID>.conf."""
    hook = container_dir / "kento-hook"
    lines = [
        "ostype: unmanaged",
        f"hostname: {name}",
        f"rootfs: {container_dir}/rootfs",
    ]
    # Network config based on net_type
    if net_type == "bridge" and bridge:
        lines.append(
            "net0: name=eth0,bridge={bridge}{ip_part}{gw_part},type=veth".format(
                bridge=bridge,
                ip_part=f",ip={ip}" if ip else "",
                gw_part=f",gw={gateway}" if gateway else "",
            )
        )
    elif net_type == "host":
        lines.append("lxc.net.0.type: none")  # shares host network
    elif bridge:  # backward compat: bridge passed without net_type
        lines.append(
            "net0: name=eth0,bridge={bridge}{ip_part}{gw_part},type=veth".format(
                bridge=bridge,
                ip_part=f",ip={ip}" if ip else "",
                gw_part=f",gw={gateway}" if gateway else "",
            )
        )
    # net_type == "none" or net_type is None with no bridge: no network lines
    if nameserver:
        lines.append(f"nameserver: {nameserver}")
    if searchdomain:
        lines.append(f"searchdomain: {searchdomain}")
    if timezone:
        lines.append(f"timezone: {timezone}")
    if nesting:
        lines.append("features: nesting=1")
        lines.append("lxc.mount.entry: proc dev/.lxc/proc proc create=dir,optional 0 0")
        lines.append("lxc.mount.entry: sys dev/.lxc/sys sysfs create=dir,optional 0 0")
        lines.append("lxc.mount.entry: /dev/fuse dev/fuse none bind,create=file,optional 0 0")
        lines.append("lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file,optional 0 0")
    lines.append(f"lxc.hook.pre-mount: {hook}")
    mount_auto = LXC_MOUNT_AUTO_NESTING if nesting else LXC_MOUNT_AUTO
    lines.append(f"lxc.mount.auto: {mount_auto}")
    lines.append(f"lxc.tty.max: {LXC_TTY}")
    if env:
        for e in env:
            lines.append(f"lxc.environment: {e}")
    return "\n".join(lines) + "\n"


def generate_qm_config(name: str, vmid: int, container_dir: Path, *,
                        hookscript_ref: str,
                        memory: int = 512,
                        cores: int = 1,
                        machine: str = "q35",
                        bridge: str | None = None,
                        net_type: str | None = None,
                        kvm: bool = True) -> str:
    """Generate a PVE QM config for a kento VM."""
    rootfs = container_dir / "rootfs"
    socket_path = container_dir / "virtiofsd.sock"

    lines = [
        f"name: {name}",
        "ostype: l26",
        f"machine: {machine}",
        f"memory: {memory}",
        f"cores: {cores}",
        f"hookscript: {hookscript_ref}",
        "serial0: socket",
    ]

    # Build args line — passed raw to QEMU
    args_parts = []
    if kvm:
        args_parts.append("-enable-kvm")
    args_parts += [
        f"-kernel {rootfs}/boot/vmlinuz",
        f"-initrd {rootfs}/boot/initramfs.img",
        '-append "console=ttyS0 rootfstype=virtiofs root=rootfs"',
        "-nographic",
        f"-chardev socket,id=vfs,path={socket_path}",
        "-device vhost-user-fs-pci,chardev=vfs,tag=rootfs",
        f"-object memory-backend-memfd,id=mem,size={memory}M,share=on",
        "-numa node,memdev=mem",
    ]
    # Join with space (PVE's args: is a single line)
    lines.append(f"args: {' '.join(args_parts)}")

    # Network
    if net_type == "bridge" and bridge:
        lines.append(f"net0: virtio,bridge={bridge}")

    return "\n".join(lines) + "\n"


def write_qm_config(vmid: int, content: str) -> Path:
    """Write a QM config to /etc/pve/nodes/<node>/qemu-server/<VMID>.conf.

    Same pmxcfs mkdir pattern as write_pve_config().
    """
    node = socket.gethostname()
    conf_dir = PVE_DIR / "nodes" / node / "qemu-server"
    for parent in [PVE_DIR / "nodes", PVE_DIR / "nodes" / node, conf_dir]:
        try:
            parent.mkdir()
        except FileExistsError:
            pass
    conf_path = conf_dir / f"{vmid}.conf"
    conf_path.write_text(content)
    return conf_path


def delete_qm_config(vmid: int) -> None:
    """Delete a QM config. No error if the file doesn't exist."""
    node = socket.gethostname()
    conf_path = PVE_DIR / "nodes" / node / "qemu-server" / f"{vmid}.conf"
    conf_path.unlink(missing_ok=True)
