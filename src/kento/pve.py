"""Proxmox VE integration — VMID allocation and PVE config generation."""

import json
import socket
import sys
from pathlib import Path

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
                        bridge: str = "vmbr0", memory: int = 512,
                        cores: int = 1, nesting: bool = True,
                        ip: str | None = None,
                        gateway: str | None = None) -> str:
    """Generate a PVE-format LXC config for /etc/pve/lxc/<VMID>.conf."""
    hook = container_dir / "kento-hook"
    lines = [
        "arch: amd64",
        "ostype: unmanaged",
        f"hostname: {name}",
        f"rootfs: {container_dir}/rootfs",
        f"memory: {memory}",
        "swap: 0",
        f"cores: {cores}",
        "net0: name=eth0,bridge={bridge}{ip_part}{gw_part},type=veth".format(
            bridge=bridge,
            ip_part=f",ip={ip}" if ip else "",
            gw_part=f",gw={gateway}" if gateway else "",
        ),
        "onboot: 0",
    ]
    if nesting:
        lines.append("features: nesting=1")
        lines.append("lxc.mount.entry: proc dev/.lxc/proc proc create=dir,optional 0 0")
        lines.append("lxc.mount.entry: sys dev/.lxc/sys sysfs create=dir,optional 0 0")
        lines.append("lxc.mount.entry: /dev/fuse dev/fuse none bind,create=file,optional 0 0")
        lines.append("lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file,optional 0 0")
    lines.extend([
        f"lxc.hook.pre-mount: {hook}",
        "lxc.mount.auto: proc:rw sys:rw cgroup:rw",
        "lxc.apparmor.profile: unconfined",
        "lxc.init.cmd: /sbin/init",
        "lxc.tty.max: 4",
        "lxc.pty.max: 1024",
    ])
    return "\n".join(lines) + "\n"
