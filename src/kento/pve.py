"""Proxmox VE integration — VMID allocation and PVE config generation."""

import json
import logging
import os
import platform
import socket
from pathlib import Path

from kento import LXC_BASE, VM_BASE
from kento.defaults import (
    APPARMOR_SYSTEMD_RULES,
    LXC_TTY, LXC_MOUNT_AUTO, LXC_MOUNT_AUTO_NESTING,
    PVE_LXC_UNLIMITED_MEMORY_MB,
)
from kento.errors import ValidationError

logger = logging.getLogger("kento")

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


def _pve_view_vmids() -> set[int]:
    """Return the set of VMIDs PVE itself knows about.

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


def _kento_recorded_vmids() -> set[int]:
    """Return VMIDs kento itself has recorded for PVE instances.

    PVE's view (`_pve_view_vmids`) misses an instance whose `.conf` was
    destroyed out-of-band (an "orphan"): kento still owns the instance dir
    but PVE reports the VMID as free, so a naive allocation would re-hand it
    out and collide. Union these in so allocation/validation reserve them.

      - PVE-LXC: the instance dir lives under LXC_BASE and its NAME is the
        VMID (an integer) — mirrors list.py's `vmid = container_dir.name`.
      - PVE-VM: the instance dir lives under VM_BASE; the VMID is stored in a
        ``kento-vmid`` file inside it.

    Best-effort and purely additive: missing base dirs, non-integer dir
    names, and missing/malformed ``kento-vmid`` files are skipped, never
    fatal. Nothing here reaps or mutates state.
    """
    ids: set[int] = set()

    # PVE-LXC: dir name is the VMID.
    if LXC_BASE.is_dir():
        for d in LXC_BASE.iterdir():
            if not d.is_dir():
                continue
            try:
                ids.add(int(d.name))
            except ValueError:
                continue

    # PVE-VM: VMID recorded in the kento-vmid file.
    if VM_BASE.is_dir():
        for d in VM_BASE.iterdir():
            vmid_file = d / "kento-vmid"
            if not vmid_file.is_file():
                continue
            try:
                ids.add(int(vmid_file.read_text().strip()))
            except (ValueError, OSError):
                continue

    return ids


def _used_vmids() -> set[int]:
    """Return the set of VMIDs already in use.

    Union of (a) PVE's own view (`_pve_view_vmids`) and (b) kento's recorded
    VMIDs (`_kento_recorded_vmids`) so that orphaned kento instances — whose
    PVE `.conf` was destroyed out-of-band — still reserve their VMID and are
    not reassigned.
    """
    return _pve_view_vmids() | _kento_recorded_vmids()


def next_vmid() -> int:
    """Return the lowest free VMID >= 100."""
    used = _used_vmids()
    vmid = 100
    while vmid in used:
        vmid += 1
    return vmid


def validate_vmid(vmid: int) -> None:
    """Raise ValidationError if VMID is invalid or already taken."""
    if vmid < 100:
        raise ValidationError(
            f"VMID must be >= 100, got {vmid} "
            f"(VMIDs 1-99 are reserved by Proxmox for internal use)."
        )
    used = _used_vmids()
    if vmid in used:
        raise ValidationError(f"VMID {vmid} is already in use")


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
                        nesting: bool = False,
                        ip: str | None = None,
                        gateway: str | None = None,
                        nameserver: str | None = None,
                        searchdomain: str | None = None,
                        timezone: str | None = None,
                        env: list[str] | None = None,
                        port: str | None = None,
                        memory: int | None = None,
                        cores: int | None = None,
                        hookscript_ref: str | None = None,
                        unprivileged: bool = False) -> str:
    """Generate a PVE-format LXC config for /etc/pve/lxc/<VMID>.conf."""
    hook = container_dir / "kento-hook"
    lines = [
        f"arch: {_pve_arch()}",
        "ostype: unmanaged",
        f"hostname: {name}",
        f"rootfs: {container_dir}/rootfs",
    ]
    if unprivileged:
        lines.append("unprivileged: 1")
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
    # Hook point for the overlay assembly (per-layer idmap for unprivileged).
    # Privileged: pre-mount. The container has no userns, so pre-mount runs in
    # the host initial namespace as real root — exactly where the overlay mount
    # belongs. (In the green privileged regression.)
    # Unprivileged: pre-start, which is the ONLY hook that runs in the host
    # INITIAL namespace as real root. pre-mount and mount both run in the
    # container's CHILD userns (mapped uid 100000), where creating an idmapped
    # bind mount fails with EPERM — so the per-layer idmap assembly cannot
    # happen there. Verified empirically on bifrost (run 19: pre-start has
    # uid_map `0 0 4294967295` + full caps and assembles cleanly; pre-mount/
    # mount run with uid_map `0 100000 65536` and hit EPERM) and corroborated by
    # deep-research (pre-start is the sole host-ns hook). The earlier spike's
    # pre-mount->mount switch rested on the false belief that mount runs as real
    # root; it does not.
    if unprivileged:
        lines.append(f"lxc.hook.pre-start: {hook}")
    else:
        lines.append(f"lxc.hook.pre-mount: {hook}")
    # start-host runs on the host after the container is running. We use it for
    # (a) nftables DNAT port-forwarding, and (b) propagating memory/cores into
    # the inner `ns` cgroup on PVE-LXC so the guest sees its own limit instead
    # of "max" (the outer cgroup gets the ceiling but `lxc.cgroup.dir.container.inner`
    # nests the actual namespace one level deeper). Register it whenever any of
    # those features need it.
    _post_stop_emitted = False
    if hookscript_ref is not None:
        lines.append(f"hookscript: {hookscript_ref}")
    elif port is not None or memory is not None or cores is not None:
        lines.append(f"lxc.hook.start-host: {hook}")
        lines.append(f"lxc.hook.post-stop: {hook}")
        _post_stop_emitted = True
    # Unprivileged containers require post-stop to clean up idmapped bind
    # mounts ($STATE_DIR/idmap). Register it unconditionally when unprivileged
    # is True, but only if the port/memory/cores branch hasn't already done so
    # (avoid a duplicate lxc.hook.post-stop line).
    if unprivileged and not _post_stop_emitted:
        lines.append(f"lxc.hook.post-stop: {hook}")
    mount_auto = LXC_MOUNT_AUTO_NESTING if nesting else LXC_MOUNT_AUTO
    lines.append(f"lxc.mount.auto: {mount_auto}")
    # Modern systemd (256+) needs userns_create + sandboxing mounts that the
    # default non-nesting LXC apparmor profile denies under AppArmor 4.x; without
    # this the guest boots network-dead. Parity with plain-lxc. When nesting is on,
    # features: nesting=1 already provides a permissive profile, so skip.
    if not nesting:
        lines.append("lxc.apparmor.profile: generated")
        for rule in APPARMOR_SYSTEMD_RULES:
            lines.append(f"lxc.apparmor.raw: {rule}")
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
    else:
        # No --memory => the user wants "unlimited". On pve-lxc, OMITTING the
        # `memory:` field is NOT unlimited: PVE silently backfills its 512 MiB
        # schema default at `pct start` and enforces it host-side on the
        # container cgroup (surprise OOM-kills). Emit the schema-ceiling
        # sentinel instead; its byte value exceeds the cgroup's representable
        # max, so the kernel clamps memory.max to literal `max` (true
        # unlimited). No lxc.cgroup2.memory.max line here: that raw line only
        # exists to mirror a FINITE cap into the guest cgroup root, and an
        # unlimited/`max` value has no finite limit to mirror. (plain-lxc is
        # unaffected — liblxc treats an omitted limit as truly unlimited.)
        lines.append(f"memory: {PVE_LXC_UNLIMITED_MEMORY_MB}")
    if cores is not None:
        # PVE's `cores` sets cpuset affinity only (restrict which CPUs).
        # `cpulimit` is the quota field that translates to cgroup cpu.max,
        # matching plain-LXC's `lxc.cgroup2.cpu.max = N*100000 100000`.
        lines.append(f"cores: {cores}")
        lines.append(f"cpulimit: {cores}")
        lines.append(f"lxc.cgroup2.cpu.max: {cores * 100000} 100000")
    # Pass-through lines (v1.2.0 Phase B, B3): each non-empty line in
    # kento-pve-args is appended verbatim AFTER kento's own lines. PVE's
    # config parser is last-value-wins within the global section, so
    # appending lets the user override kento defaults (e.g. the user
    # writes `memory: 4096` and wins over a kento-emitted `memory: 512`).
    # Denylist in create.py blocks keys kento owns structurally
    # (rootfs:, mp0:, arch:, hostname:, lxc.rootfs.path).
    lines.extend(_read_passthrough_lines(container_dir / "kento-pve-args"))
    return "\n".join(lines) + "\n"


def _read_passthrough_lines(path: Path) -> list[str]:
    """Return non-empty lines from a kento-pve-args-style file, stripped of
    trailing newlines. Absent file returns an empty list.

    kento does not parse or validate the contents — the B1 denylist has
    already rejected the structural collisions, and anything else is
    user-authored and kento trusts it.
    """
    if not path.is_file():
        return []
    out: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.rstrip("\r")
        if not line:
            continue
        out.append(line)
    return out


def generate_qm_args(container_dir: Path, *,
                     memory: int = 1024,
                     kvm: bool = True) -> str:
    """Build the kento-managed `args:` payload for a PVE VM config.

    Returns the portion that follows ``args: `` — a single space-separated
    line of QEMU arguments.  kento assumes exclusive ownership of this
    field: callers mixing in user-supplied args will have those overwritten.

    The memfd ``size=`` is derived from ``memory`` and must be kept in
    sync with PVE's top-level ``memory:`` field, otherwise the pre-start
    hookscript validator aborts the VM start.

    Pass-through flags (v1.2.0 Phase B): if ``container_dir/kento-qemu-args``
    exists, each non-empty line is appended space-separated to the args
    payload — kento's own flags first, then pass-through. QEMU honours the
    last occurrence of a flag, so ``--qemu-arg '-m 2048'`` overrides the
    kento-managed defaults. Since ``args:`` is tokenized by qm with simple
    whitespace splitting (NOT shlex), a pass-through line that itself
    contains whitespace is a foot-gun: the user must split it across
    multiple --qemu-arg flags. Raises ValidationError at consumption time.

    Usermode networking: if ``container_dir/kento-port`` exists (format
    ``HOST:GUEST``), a slirp netdev + virtio-net device are injected so a
    pve-vm with ``--network usermode`` gets the same slirp networking +
    host-port forwarding as a plain vm (see vm.py). The device MAC is read
    from ``container_dir/kento-mac`` when present and non-empty. Injected
    before the pass-through loop so a user ``--qemu-arg`` still wins
    (qm honours the last occurrence). For bridge/host/none modes the file
    is absent and nothing is emitted (net0 is handled in generate_qm_config).
    """
    rootfs = container_dir / "rootfs"
    socket_path = container_dir / "virtiofsd.sock"

    args_parts = []
    if kvm:
        args_parts.append("-enable-kvm")
        # Nesting (v1.3.0): inject kento's own -cpu. kento-nesting holds "1"
        # (expose vmx/svm for nested accelerated VMs) or "0"/absent (mask them).
        # CPU model is always `host`; nesting OFF deterministically strips vmx/svm.
        # Gated on kvm because the `host` model requires KVM; without it PVE's
        # own default -cpu applies under TCG (matches vm.py's `if VM_KVM` gating).
        # [E2E-VALIDATE] D-strict: assumes PVE accepts a duplicate -cpu in args:
        # and QEMU honors the last one. Fallback if not: D-simple (cpu: host config
        # field). Emitted before the kento-qemu-args pass-through below so a user
        # --qemu-arg '-cpu ...' still wins.
        nesting_file = container_dir / "kento-nesting"
        nesting_on = nesting_file.is_file() and nesting_file.read_text().strip() == "1"
        cpu = "host" if nesting_on else "host,vmx=off,svm=off"
        args_parts.append(f"-cpu {cpu}")
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

    # Usermode networking (slirp). Mirrors the plain-vm path in vm.py: when
    # kento-port (HOST:GUEST) is present, emit a slirp netdev with hostfwd plus
    # a virtio-net device. PVE appends this args: line verbatim to the qemu
    # cmdline, so this gives pve-vm --network usermode the same NIC + port
    # forwarding as plain vm. Emitted before the pass-through loop so a user
    # --qemu-arg still wins (qm honours the last occurrence). Each args_parts
    # element is a single "-flag value" pair whose value has no whitespace,
    # satisfying qm's whitespace tokenization.
    from kento.vm import _read_hostfwds
    hostfwds = _read_hostfwds(container_dir)
    if hostfwds:
        device = "virtio-net-pci,netdev=net0"
        mac_file = container_dir / "kento-mac"
        if mac_file.is_file():
            mac = mac_file.read_text().strip()
            if mac:
                device = f"virtio-net-pci,netdev=net0,mac={mac}"
        # One netdev with N comma-joined hostfwd= options. Each args_parts
        # element must contain no whitespace (qm whitespace-tokenizes the
        # args: line); comma-joining keeps the whole -netdev value one token.
        netdev = "user,id=net0," + ",".join(
            f"hostfwd={hf}" for hf in hostfwds)
        args_parts += [
            f"-netdev {netdev}",
            f"-device {device}",
        ]

    passthrough_file = container_dir / "kento-qemu-args"
    if passthrough_file.is_file():
        for line in passthrough_file.read_text().splitlines():
            if not line:
                continue
            # qm's args: is whitespace-tokenized with no quoting support,
            # so any whitespace in a single pass-through entry would split
            # into two QEMU flags at boot. Reject explicitly rather than
            # silently mangling the user's intent.
            if any(c.isspace() for c in line):
                raise ValidationError(
                    f"kento-qemu-args line contains whitespace which "
                    f"qm does not tokenize safely: {line!r}. Split into "
                    f"separate --qemu-arg flags instead."
                )
            args_parts.append(line)

    return " ".join(args_parts)


def generate_qm_config(name: str, vmid: int, container_dir: Path, *,
                        hookscript_ref: str,
                        memory: int = 1024,
                        cores: int = 1,
                        machine: str = "q35",
                        bridge: str | None = None,
                        net_type: str | None = None,
                        kvm: bool = True,
                        mac: str | None = None) -> str:
    """Generate a PVE QM config for a kento VM."""
    lines = [
        f"name: {name}",
        "ostype: l26",
        f"machine: {machine}",
        f"memory: {memory}",
        f"cores: {cores}",
        f"hookscript: {hookscript_ref}",
        "serial0: socket",
    ]

    # args: is kento-managed (see generate_qm_args); user-added content
    # would be overwritten by scrub.
    lines.append(f"args: {generate_qm_args(container_dir, memory=memory, kvm=kvm)}")

    # Network. PVE's net0 format: virtio=<MAC>,bridge=<name>. Include MAC
    # whenever we have one so external DHCP reservations stay stable across
    # recreate/scrub.
    if net_type == "bridge" and bridge:
        if mac:
            lines.append(f"net0: virtio={mac},bridge={bridge}")
        else:
            lines.append(f"net0: virtio,bridge={bridge}")

    # Pass-through lines (v1.2.0 Phase B, B3): same contract as generate_pve_config.
    # Appended AFTER kento's own lines so last-value-wins hands the user control
    # over duplicate keys (e.g. user-supplied `balloon: 0` overrides nothing,
    # but `cores: 8` would override kento's `cores: <N>` from the create flag).
    lines.extend(_read_passthrough_lines(container_dir / "kento-pve-args"))

    return "\n".join(lines) + "\n"


def _parse_qm_conf_field(content: str, field: str) -> str | None:
    """Return the value of a top-level qm config field, or None if absent.

    Matches ``<field>: <value>`` lines in the global section (the section
    before any ``[snapshot]`` header). Returns the last occurrence if the
    field is repeated (shouldn't happen, but shouldn't mask the repeat
    either).
    """
    value: str | None = None
    for raw in content.splitlines():
        line = raw.rstrip()
        stripped = line.lstrip()
        if stripped.startswith("[") and stripped.endswith("]"):
            break  # snapshot section; stop here
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, sep, val = stripped.partition(":")
        if sep and key.strip() == field:
            value = val.strip()
    return value


def sync_qm_args_to_memory(vmid: int, container_dir: Path, *,
                           kvm: bool = True) -> tuple[int | None, int | None]:
    """Rewrite the ``args:`` line in the PVE qm config so memfd ``size=``
    matches PVE's ``memory:`` field.

    Use case: ``qm set <vmid> --memory N`` updates the top-level
    ``memory:`` field but leaves the embedded ``size=<N>M`` inside
    ``args:`` stale. kento's pre-start hookscript validator catches this
    and refuses to boot; ``kento vm scrub`` calls this helper to rewrite
    args in place so the next start succeeds.

    Behaviour:
    - If the qm config is missing or has no ``memory:`` field, this is a
      no-op (returns (None, None)).
    - If an ``args:`` line exists it is replaced with a freshly-generated
      one. If it doesn't exist (e.g. externally-stripped config), one is
      appended.
    - PVE's config wins: kento's ``kento-memory`` / ``kento-cores``
      metadata files are rewritten to match the qm config so subsequent
      operations see a consistent value.

    kento assumes exclusive ownership of ``args:`` — any user-added QEMU
    flags on that line will be overwritten. Workaround: don't edit
    ``args:`` directly.

    Returns (memory, cores) parsed from qm config (either may be None
    if the config doesn't have that field).
    """
    node = _pve_node_name()
    conf_path = PVE_DIR / "nodes" / node / "qemu-server" / f"{vmid}.conf"
    if not conf_path.is_file():
        return (None, None)

    content = conf_path.read_text()
    memory_raw = _parse_qm_conf_field(content, "memory")
    cores_raw = _parse_qm_conf_field(content, "cores")
    if memory_raw is None:
        # No memory: field — nothing to sync against.
        return (None, _coerce_int(cores_raw))

    try:
        memory = int(memory_raw)
    except ValueError:
        return (None, _coerce_int(cores_raw))

    new_args_line = f"args: {generate_qm_args(container_dir, memory=memory, kvm=kvm)}"

    # Only the GLOBAL (pre-first-section) `args:` field is kento's to rewrite.
    # PVE stores each snapshot's full config — including its own `args:` line —
    # under a `[<snapname>]` section. Mirror _parse_qm_conf_field's boundary:
    # once we hit the first section header, append the remainder verbatim and
    # stop touching args: (rewriting/dropping snapshot args: corrupts them).
    out_lines: list[str] = []
    replaced = False
    in_global = True
    insert_idx: int | None = None  # where to insert if args: never appeared
    for raw in content.splitlines():
        stripped = raw.strip()
        if in_global and stripped.startswith("[") and stripped.endswith("]"):
            # First snapshot header: leave the global region. Remember this
            # spot so an absent global args: lands BEFORE the section, not at
            # EOF (which would fall inside/after a snapshot section).
            in_global = False
            insert_idx = len(out_lines)
        if in_global and raw.startswith("args:"):
            if not replaced:
                out_lines.append(new_args_line)
                replaced = True
            # Drop duplicate global args lines (shouldn't occur, be explicit).
            continue
        out_lines.append(raw)
    if not replaced:
        if insert_idx is not None:
            out_lines.insert(insert_idx, new_args_line)
        else:
            out_lines.append(new_args_line)

    # Preserve trailing newline behaviour.
    new_content = "\n".join(out_lines)
    if content.endswith("\n") and not new_content.endswith("\n"):
        new_content += "\n"
    conf_path.write_text(new_content)

    # PVE's config wins: rewrite kento metadata to match so `kento info`,
    # plain-VM fallback, and other consumers agree.
    (container_dir / "kento-memory").write_text(f"{memory}\n")
    cores = _coerce_int(cores_raw)
    if cores is not None:
        (container_dir / "kento-cores").write_text(f"{cores}\n")

    return (memory, cores)


def _coerce_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


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
