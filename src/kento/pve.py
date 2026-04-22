"""Proxmox VE integration — VMID allocation and PVE config generation."""

import json
import os
import platform
import socket
import sys
from pathlib import Path

from kento.defaults import LXC_TTY, LXC_MOUNT_AUTO, LXC_MOUNT_AUTO_NESTING

PVE_DIR = Path("/etc/pve")
PVE_LXC_DIR = PVE_DIR / "lxc"
PVE_QEMU_DIR = PVE_DIR / "qemu-server"

_ARCH_MAP = {
    "x86_64": "amd64",
    "aarch64": "arm64",
    "i686": "i386",
    "i386": "i386",
}


def _pve_arch() -> str:
    m = platform.machine()
    return _ARCH_MAP.get(m, m)


def _pve_node_name() -> str:
    """Get the PVE node name from /etc/pve/local symlink.

    PVE's /etc/pve/local is a symlink to /etc/pve/nodes/<node-name>.
    The node name defaults to the hostname but can differ (set during
    PVE installation).  Fall back to socket.gethostname() if the
    symlink doesn't exist (shouldn't happen on a real PVE host).
    """
    local = PVE_DIR / "local"
    if local.is_symlink():
        return Path(os.readlink(local)).name
    return socket.gethostname()


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
    node = _pve_node_name()
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
    node = _pve_node_name()
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
                        env: list[str] | None = None,
                        port: str | None = None,
                        memory: int | None = None,
                        cores: int | None = None,
                        hookscript_ref: str | None = None) -> str:
    """Generate a PVE-format LXC config for /etc/pve/lxc/<VMID>.conf."""
    hook = container_dir / "kento-hook"
    lines = [
        f"arch: {_pve_arch()}",
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
    # start-host runs on the host after the container is running. We use it for
    # (a) nftables DNAT port-forwarding, and (b) propagating memory/cores into
    # the inner `ns` cgroup on PVE-LXC so the guest sees its own limit instead
    # of "max" (the outer cgroup gets the ceiling but `lxc.cgroup.dir.container.inner`
    # nests the actual namespace one level deeper). Register it whenever any of
    # those features need it.
    if hookscript_ref is not None:
        lines.append(f"hookscript: {hookscript_ref}")
    elif port is not None or memory is not None or cores is not None:
        lines.append(f"lxc.hook.start-host: {hook}")
        lines.append(f"lxc.hook.post-stop: {hook}")
    mount_auto = LXC_MOUNT_AUTO_NESTING if nesting else LXC_MOUNT_AUTO
    lines.append(f"lxc.mount.auto: {mount_auto}")
    lines.append(f"lxc.tty.max: {LXC_TTY}")
    if env:
        for e in env:
            lines.append(f"lxc.environment: {e}")
    if memory is not None:
        lines.append(f"memory: {memory}")
        # Also write the raw cgroup field so the guest's cgroup namespace
        # sees the limit. PVE's `memory:` shorthand propagates to the host
        # cgroup, but older PVE versions don't always mirror the value
        # into the guest's cgroup root — so `cat /sys/fs/cgroup/memory.max`
        # inside the container still reads "max". Emitting both is safe.
        lines.append(f"lxc.cgroup2.memory.max: {memory * 1048576}")
    if cores is not None:
        # PVE's `cores` sets cpuset affinity only (restrict which CPUs).
        # `cpulimit` is the quota field that translates to cgroup cpu.max,
        # matching plain-LXC's `lxc.cgroup2.cpu.max = N*100000 100000`.
        lines.append(f"cores: {cores}")
        lines.append(f"cpulimit: {cores}")
        lines.append(f"lxc.cgroup2.cpu.max: {cores * 100000} 100000")
    return "\n".join(lines) + "\n"


def generate_qm_config(name: str, vmid: int, container_dir: Path, *,
                        hookscript_ref: str,
                        memory: int = 512,
                        cores: int = 1,
                        machine: str = "q35",
                        bridge: str | None = None,
                        net_type: str | None = None,
                        kvm: bool = True,
                        mac: str | None = None) -> str:
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

    # Network. PVE's net0 format: virtio=<MAC>,bridge=<name>. Include MAC
    # whenever we have one so external DHCP reservations stay stable across
    # recreate/scrub.
    if net_type == "bridge" and bridge:
        if mac:
            lines.append(f"net0: virtio={mac},bridge={bridge}")
        else:
            lines.append(f"net0: virtio,bridge={bridge}")

    return "\n".join(lines) + "\n"


def write_qm_config(vmid: int, content: str) -> Path:
    """Write a QM config to /etc/pve/nodes/<node>/qemu-server/<VMID>.conf.

    Same pmxcfs mkdir pattern as write_pve_config().
    """
    node = _pve_node_name()
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
    node = _pve_node_name()
    conf_path = PVE_DIR / "nodes" / node / "qemu-server" / f"{vmid}.conf"
    conf_path.unlink(missing_ok=True)
