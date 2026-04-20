"""Create a container backed by an OCI image."""

import subprocess
import sys
from pathlib import Path

from kento import LXC_BASE, VM_BASE, require_root, upper_base, detect_mode, sanitize_image_name, next_instance_name
from kento.defaults import LXC_TTY, LXC_MOUNT_AUTO, LXC_MOUNT_AUTO_NESTING
from kento.hook import write_hook
from kento.inject import write_inject
from kento.layers import resolve_layers


def generate_config(name: str, lxc_dir: Path, *, bridge: str | None = None,
                    net_type: str | None = None,
                    nesting: bool = True,
                    ip: str | None = None, gateway: str | None = None,
                    env: list[str] | None = None) -> str:
    hook = lxc_dir / "kento-hook"
    lines = [
        f"lxc.uts.name = {name}",
        f"lxc.rootfs.path = dir:{lxc_dir}/rootfs",
        "",
        "lxc.hook.version = 1",
        f"lxc.hook.pre-start = {hook}",
        f"lxc.hook.post-stop = {hook}",
    ]
    # Network config based on net_type
    if net_type == "bridge" and bridge:
        lines += [
            "",
            "lxc.net.0.type = veth",
            f"lxc.net.0.link = {bridge}",
            "lxc.net.0.flags = up",
        ]
        if ip:
            lines.append(f"lxc.net.0.ipv4.address = {ip}")
            if gateway:
                lines.append(f"lxc.net.0.ipv4.gateway = {gateway}")
    elif net_type == "host":
        lines += [
            "",
            "lxc.net.0.type = none",  # shares host network
        ]
    elif bridge:  # backward compat: bridge passed without net_type
        lines += [
            "",
            "lxc.net.0.type = veth",
            f"lxc.net.0.link = {bridge}",
            "lxc.net.0.flags = up",
        ]
        if ip:
            lines.append(f"lxc.net.0.ipv4.address = {ip}")
            if gateway:
                lines.append(f"lxc.net.0.ipv4.gateway = {gateway}")
    # net_type == "none" or net_type is None with no bridge: no network lines
    mount_auto = LXC_MOUNT_AUTO_NESTING if nesting else LXC_MOUNT_AUTO
    lines += [
        "",
        f"lxc.mount.auto = {mount_auto}",
        f"lxc.tty.max = {LXC_TTY}",
    ]

    if nesting:
        lines.append("lxc.include = /usr/share/lxc/config/nesting.conf")
        lines.append("lxc.mount.entry = /dev/fuse dev/fuse none bind,create=file,optional 0 0")
        lines.append("lxc.mount.entry = /dev/net/tun dev/net/tun none bind,create=file,optional 0 0")
    if env:
        for e in env:
            lines.append(f"lxc.environment = {e}")

    return "\n".join(lines) + "\n"


def _inject_network_config(state_dir: Path, ip: str,
                           gateway: str | None = None,
                           dns: str | None = None,
                           searchdomain: str | None = None,
                           mode: str = "lxc") -> None:
    """Write 10-static.network into the overlayfs upper layer."""
    # VM modes use predictable naming (e.g. enp0s2), so match by type.
    # LXC/PVE modes always have eth0 (configured by LXC veth).
    match_line = "Type=ether" if mode in ("vm", "pve-vm") else "Name=eth0"
    lines = [
        "[Match]",
        match_line,
        "",
        "[Network]",
        f"Address={ip}",
    ]
    if gateway:
        lines.append(f"Gateway={gateway}")
    if dns:
        lines.append(f"DNS={dns}")
    if searchdomain:
        lines.append(f"Domains={searchdomain}")
    lines.append("")

    net_dir = state_dir / "upper" / "etc" / "systemd" / "network"
    net_dir.mkdir(parents=True, exist_ok=True)
    (net_dir / "10-static.network").write_text("\n".join(lines))


def _inject_hostname(state_dir: Path, hostname: str) -> None:
    """Write /etc/hostname into the overlayfs upper layer."""
    etc = state_dir / "upper" / "etc"
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "hostname").write_text(hostname + "\n")


def _inject_timezone(state_dir: Path, timezone: str) -> None:
    """Write timezone config into the overlayfs upper layer."""
    etc = state_dir / "upper" / "etc"
    etc.mkdir(parents=True, exist_ok=True)
    localtime = etc / "localtime"
    localtime.unlink(missing_ok=True)
    localtime.symlink_to(f"/usr/share/zoneinfo/{timezone}")
    (etc / "timezone").write_text(timezone + "\n")


def _inject_env(state_dir: Path, env_list: list[str]) -> None:
    """Write /etc/environment into the overlayfs upper layer."""
    etc = state_dir / "upper" / "etc"
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "environment").write_text("\n".join(env_list) + "\n")


def create(image: str, *, name: str | None = None, bridge: str | None = None,
           nesting: bool = True,
           start: bool = False, mode: str | None = None,
           vmid: int = 0, port: str | None = None,
           ip: str | None = None, gateway: str | None = None,
           dns: str | None = None, searchdomain: str | None = None,
           timezone: str | None = None,
           env: list[str] | None = None,
           ssh_keys: list[str] | None = None,
           net_type: str | None = None) -> None:
    require_root()

    # Validate and read SSH key files early (before any filesystem changes)
    ssh_key_contents: str | None = None
    if ssh_keys:
        parts = []
        for key_path in ssh_keys:
            p = Path(key_path)
            if not p.is_file():
                print(f"Error: SSH key file not found: {key_path}",
                      file=sys.stderr)
                sys.exit(1)
            parts.append(p.read_text())
        ssh_key_contents = "\n".join(parts)
        if not ssh_key_contents.endswith("\n"):
            ssh_key_contents += "\n"

    # Resolve mode
    mode = detect_mode(mode)

    # Auto-detect pve-vm: VM mode on a PVE host
    if mode == "vm":
        from kento.pve import is_pve
        if is_pve():
            mode = "pve-vm"

    # Resolve network configuration
    from kento import resolve_network
    network = resolve_network(net_type, bridge, mode, port)
    bridge = network["bridge"]
    port = network["port"]

    # Determine base directory for this mode
    base_dir = VM_BASE if mode in ("vm", "pve-vm") else LXC_BASE

    # Resolve container name
    if name is None:
        base_name = sanitize_image_name(image)
        other_dir = LXC_BASE if base_dir == VM_BASE else VM_BASE
        name = next_instance_name(base_name, base_dir, other_dir=other_dir)
    elif (base_dir / name).exists():
        print(f"Error: container name already taken: {name}", file=sys.stderr)
        sys.exit(1)

    # Validate mode-specific flags
    if vmid and mode not in ("pve", "pve-vm"):
        print(f"Error: --vmid cannot be used with {mode.upper()} mode", file=sys.stderr)
        sys.exit(1)
    if port is not None and mode not in ("vm", "pve-vm"):
        print(f"Error: --port cannot be used with {mode.upper()} mode", file=sys.stderr)
        sys.exit(1)
    if ip is not None and mode in ("vm", "pve-vm"):
        print("Error: --ip cannot be used with VM mode", file=sys.stderr)
        sys.exit(1)
    if gateway and not ip:
        print("Error: --gateway requires --ip", file=sys.stderr)
        sys.exit(1)
    if mode in ("vm", "pve-vm"):
        if not nesting:
            print("Warning: --nesting is ignored in VM mode", file=sys.stderr)

    # Resolve container_id for directory paths
    if mode == "pve":
        from kento.pve import next_vmid, validate_vmid, generate_pve_config, write_pve_config
        if vmid:
            validate_vmid(vmid)
        else:
            vmid = next_vmid()
        container_id = str(vmid)
        print(f"Mode: pve (VMID {vmid})")
    elif mode == "pve-vm":
        from kento.pve import next_vmid, validate_vmid, generate_qm_config, write_qm_config
        from kento.vm_hook import write_vm_hook, write_snippets_wrapper
        if vmid:
            validate_vmid(vmid)
        else:
            vmid = next_vmid()
        container_id = name  # VM_BASE uses name, not VMID
        print(f"Mode: pve-vm (VMID {vmid})")
    elif mode == "vm":
        container_id = name
        print("Mode: vm")
    else:
        container_id = name
        print("Mode: lxc")

    container_dir = base_dir / container_id

    if container_dir.exists():
        print(f"Error: container already exists: {container_id}", file=sys.stderr)
        sys.exit(1)

    # Resolve layers (validates image exists)
    layers = resolve_layers(image)
    if not layers:
        print(f"Error: failed to resolve layer paths for {image}",
              file=sys.stderr)
        sys.exit(1)

    # Create directory structure — upper/work may be outside container_dir for sudo users
    state_dir = upper_base(container_id, base_dir if mode in ("vm", "pve-vm") else None)
    (container_dir / "rootfs").mkdir(parents=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "upper").mkdir(exist_ok=True)
    (state_dir / "work").mkdir(exist_ok=True)

    # Write image reference, layer paths, state dir, mode, and name
    (container_dir / "kento-image").write_text(image + "\n")
    (container_dir / "kento-layers").write_text(layers + "\n")
    (container_dir / "kento-state").write_text(str(state_dir) + "\n")
    (container_dir / "kento-mode").write_text(mode + "\n")
    (container_dir / "kento-name").write_text(name + "\n")

    # Write static IP config if requested
    if ip or dns or searchdomain:
        net_parts = []
        if ip:
            net_parts.append(f"ip={ip}")
        if gateway:
            net_parts.append(f"gateway={gateway}")
        if dns:
            net_parts.append(f"dns={dns}")
        if searchdomain:
            net_parts.append(f"searchdomain={searchdomain}")
        (container_dir / "kento-net").write_text("\n".join(net_parts) + "\n")
        if ip:
            _inject_network_config(state_dir, ip, gateway, dns, searchdomain,
                                   mode=mode)
        elif dns or searchdomain:
            resolved_dir = state_dir / "upper" / "etc" / "systemd" / "resolved.conf.d"
            resolved_dir.mkdir(parents=True, exist_ok=True)
            lines = ["[Resolve]"]
            if dns:
                lines.append(f"DNS={dns}")
            if searchdomain:
                lines.append(f"Domains={searchdomain}")
            lines.append("")
            (resolved_dir / "90-kento.conf").write_text("\n".join(lines))

    # Write hostname into guest
    _inject_hostname(state_dir, name)

    # Write timezone config if requested
    if timezone:
        (container_dir / "kento-tz").write_text(timezone + "\n")
        _inject_timezone(state_dir, timezone)

    # Write environment variables if requested
    if env:
        (container_dir / "kento-env").write_text("\n".join(env) + "\n")
        _inject_env(state_dir, env)

    # Write SSH authorized_keys metadata if requested. Hook copies this into
    # the guest's /root/.ssh/authorized_keys on every start.
    if ssh_key_contents is not None:
        (container_dir / "kento-authorized-keys").write_text(ssh_key_contents)

    if mode in ("vm", "pve-vm"):
        # Write port mapping (usermode networking only)
        if network["type"] == "usermode":
            from kento.vm import allocate_port
            if port is None:
                host_port = allocate_port(base_dir)
                guest_port = 22
            else:
                host_port, guest_port = port.split(":")
                host_port, guest_port = int(host_port), int(guest_port)
            (container_dir / "kento-port").write_text(f"{host_port}:{guest_port}\n")

        if mode == "pve-vm":
            # Generate VM hookscript + inject.sh (hookscript invokes inject.sh
            # in its pre-start phase after overlayfs mount, before virtiofsd).
            write_vm_hook(container_dir, layers, name, state_dir)
            write_inject(container_dir)

            # Write snippets wrapper and get PVE reference
            hookscript_ref = write_snippets_wrapper(vmid, container_dir / "kento-hook")

            # Write VMID reference
            (container_dir / "kento-vmid").write_text(str(vmid) + "\n")

            # Generate and write QM config
            from kento.defaults import get_vm_defaults
            vm_defaults = get_vm_defaults()
            qm_conf = write_qm_config(
                vmid,
                generate_qm_config(
                    name, vmid, container_dir,
                    hookscript_ref=hookscript_ref,
                    memory=vm_defaults["memory"],
                    cores=vm_defaults["cores"],
                    machine=vm_defaults["machine"],
                    kvm=vm_defaults["kvm"],
                    bridge=bridge,
                    net_type=network.get("type"),
                ),
            )

            print(f"\nVM created: {name}")
            print(f"  Image:   {image}")
            print(f"  VMID:    {vmid}")
            if network["type"] == "usermode":
                print(f"  Port:    {host_port}:{guest_port}")
            elif network["type"] == "bridge":
                print(f"  Bridge:  {bridge}")
            print(f"  Config:  {qm_conf}")
            print(f"  Dir:     {container_dir}")
        else:
            # Plain VM mode (no PVE)
            write_inject(container_dir)
            print(f"\nContainer created: {name}")
            print(f"  Image:   {image}")
            if network["type"] == "usermode":
                print(f"  Port:    {host_port}:{guest_port}")
            print(f"  Dir:     {container_dir}")
    else:
        # Generate hook (LXC/PVE only) + inject.sh (shared with future VM/PVE-VM modes)
        write_hook(container_dir, layers, name, state_dir)
        write_inject(container_dir)

        # Generate config
        if mode == "pve":
            pve_conf = write_pve_config(
                vmid,
                generate_pve_config(name, vmid, container_dir, bridge=bridge,
                                    net_type=network.get("type"),
                                    nesting=nesting, ip=ip,
                                    gateway=gateway, nameserver=dns,
                                    searchdomain=searchdomain,
                                    timezone=timezone, env=env)
            )
            config_path = str(pve_conf)
        else:
            (container_dir / "config").write_text(
                generate_config(name, container_dir, bridge=bridge,
                                net_type=network.get("type"),
                                nesting=nesting,
                                ip=ip, gateway=gateway, env=env)
            )
            config_path = f"{container_dir}/config"

        print(f"\nContainer created: {name}")
        print(f"  Image:   {image}")
        print(f"  Bridge:  {bridge}")
        if mode == "pve":
            print(f"  VMID:    {vmid}")
        print(f"  Nesting: {nesting}")
        print(f"  Config:  {config_path}")

    if start:
        print("\nStarting...")
        if mode in ("vm", "pve-vm"):
            if mode == "pve-vm":
                subprocess.run(["qm", "start", str(vmid)], check=True)
            else:
                from kento.vm import start_vm
                start_vm(container_dir, name)
        elif mode == "pve":
            subprocess.run(["pct", "start", str(vmid)], check=True)
        else:
            subprocess.run(["lxc-start", "-n", name], check=True)
        print("  Status: running")
    else:
        print(f"  Status: stopped (use 'kento start {name}' to boot)")
