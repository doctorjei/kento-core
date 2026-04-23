"""Create an instance backed by an OCI image."""

import subprocess
import sys
from pathlib import Path

from kento import (LXC_BASE, VM_BASE, _scan_namespace, next_instance_name,
                   require_root, sanitize_image_name, upper_base)
from kento.cloudinit import detect_cloudinit, write_seed
from kento.defaults import LXC_TTY, LXC_MOUNT_AUTO, LXC_MOUNT_AUTO_NESTING
from kento.hook import write_hook
from kento.inject import write_inject
from kento.layers import resolve_layers


def generate_config(name: str, lxc_dir: Path, *, bridge: str | None = None,
                    net_type: str | None = None,
                    nesting: bool = True,
                    ip: str | None = None, gateway: str | None = None,
                    env: list[str] | None = None,
                    port: str | None = None,
                    memory: int | None = None,
                    cores: int | None = None,
                    unconfined: bool = False,
                    mode: str = "lxc") -> str:
    hook = lxc_dir / "kento-hook"
    lines = [
        f"lxc.uts.name = {name}",
        f"lxc.rootfs.path = dir:{lxc_dir}/rootfs",
        "",
        "lxc.hook.version = 1",
        f"lxc.hook.pre-start = {hook}",
        f"lxc.hook.post-stop = {hook}",
    ]
    if port is not None:
        lines.append(f"lxc.hook.start-host = {hook}")
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

    # Plain-LXC on modern OCI images (systemd 256+) needs AppArmor unconfined:
    # the stock lxc-container-default-with-nesting profile blocks the credentials
    # tmpfs mount used by ImportCredential= directives, making systemd-journald,
    # systemd-networkd, systemd-tmpfiles-setup all fail with status=243/CREDENTIALS.
    # PVE-LXC doesn't have this problem: pct uses apparmor.profile=generated which
    # labels in-container processes :unconfined automatically. For plain LXC our
    # only fix today is to drop confinement entirely — gated behind --unconfined.
    #
    # common.conf must be included BEFORE nesting.conf so apparmor.profile ends up
    # set AFTER both includes (otherwise nesting.conf would override it).
    if unconfined and mode == "lxc":
        lines.append("lxc.include = /usr/share/lxc/config/common.conf")
    if nesting:
        lines.append("lxc.include = /usr/share/lxc/config/nesting.conf")
        lines.append("lxc.mount.entry = /dev/fuse dev/fuse none bind,create=file,optional 0 0")
        lines.append("lxc.mount.entry = /dev/net/tun dev/net/tun none bind,create=file,optional 0 0")
    if unconfined and mode == "lxc":
        lines.append("lxc.apparmor.profile = unconfined")
        lines.append("lxc.apparmor.allow_nesting = 1")
        lines.append("lxc.apparmor.allow_incomplete = 1")
    if env:
        for e in env:
            lines.append(f"lxc.environment = {e}")
    if memory is not None:
        lines.append(f"lxc.cgroup2.memory.max = {memory * 1048576}")
    if cores is not None:
        lines.append(f"lxc.cgroup2.cpu.max = {cores * 100000} 100000")

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


def _generate_ssh_host_keys(dest_dir: Path) -> None:
    """Generate SSH host key pairs (rsa, ecdsa, ed25519) in dest_dir."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for key_type, extra_args in [("rsa", ["-b", "4096"]), ("ecdsa", []), ("ed25519", [])]:
        key_path = dest_dir / f"ssh_host_{key_type}_key"
        cmd = ["ssh-keygen", "-t", key_type] + extra_args + ["-f", str(key_path), "-N", ""]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except FileNotFoundError:
            print("Error: ssh-keygen not found. Install openssh-client to use --ssh-host-keys.",
                  file=sys.stderr)
            sys.exit(1)


def _copy_ssh_host_keys(src_dir: Path, dest_dir: Path) -> None:
    """Copy ssh_host_* files from src_dir into dest_dir."""
    import shutil
    dest_dir.mkdir(parents=True, exist_ok=True)
    for f in sorted(src_dir.iterdir()):
        if f.name.startswith("ssh_host_") and f.is_file():
            shutil.copy2(f, dest_dir / f.name)


def create(image: str, *, name: str | None = None, bridge: str | None = None,
           nesting: bool = True,
           start: bool = False, mode: str,
           pve: bool | None = None,
           vmid: int = 0, memory: int | None = None, cores: int | None = None,
           port: str | None = None,
           ip: str | None = None, gateway: str | None = None,
           dns: str | None = None, searchdomain: str | None = None,
           timezone: str | None = None,
           env: list[str] | None = None,
           ssh_keys: list[str] | None = None,
           ssh_key_user: str = "root",
           ssh_host_keys: bool = False,
           ssh_host_key_dir: str | None = None,
           mac: str | None = None,
           config_mode: str = "auto",
           net_type: str | None = None,
           unconfined: bool = False,
           force: bool = False) -> None:
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

    # Validate --ssh-host-key-dir early
    if ssh_host_key_dir is not None:
        src = Path(ssh_host_key_dir)
        if not src.is_dir():
            print(f"Error: SSH host key directory not found: {ssh_host_key_dir}",
                  file=sys.stderr)
            sys.exit(1)
        has_key = any(f.name.startswith("ssh_host_") and f.name.endswith("_key")
                      and f.is_file() for f in src.iterdir())
        if not has_key:
            print(f"Error: no ssh_host_*_key files found in {ssh_host_key_dir}",
                  file=sys.stderr)
            sys.exit(1)

    # Resolve PVE promotion
    from kento.pve import is_pve
    if pve is True:
        if not is_pve():
            print("Error: --pve specified but this is not a PVE host", file=sys.stderr)
            sys.exit(1)
        if mode == "vm":
            mode = "pve-vm"
        else:
            mode = "pve"
    elif pve is False:
        pass
    else:
        if is_pve():
            if mode == "vm":
                mode = "pve-vm"
            else:
                mode = "pve"

    # --unconfined is only meaningful for plain LXC. PVE-LXC uses
    # apparmor.profile=generated which doesn't have the credentials bug.
    # Reject here to catch PVE auto-promotion on PVE hosts (the CLI rejects
    # explicit --pve + --unconfined earlier, but autodetection needs this).
    if unconfined and mode == "pve":
        print("Error: --unconfined is only for plain LXC; PVE-LXC uses "
              "apparmor.profile=generated which doesn't have this issue.",
              file=sys.stderr)
        sys.exit(1)

    # Plain-LXC on modern OCI images (systemd 256+) is broken by the default
    # AppArmor profile: journald/tmpfiles/networkd all fail with status=243/
    # CREDENTIALS. The only plain-LXC fix today is full `unconfined` — gate it
    # behind an explicit flag so users acknowledge the tradeoff. See docs/
    # troubleshooting.md for the full story. Runs AFTER PVE auto-promotion so
    # that on PVE hosts the mode becomes "pve" before this check and the gate
    # stays silent — PVE-LXC doesn't hit the systemd credentials bug.
    if mode == "lxc" and not unconfined:
        print("Error: plain LXC mode requires '--unconfined' due to the "
              "systemd 256+ credentials bug.", file=sys.stderr)
        print("  This runs the container without AppArmor confinement — do not "
              "use for untrusted workloads.", file=sys.stderr)
        print("", file=sys.stderr)
        print("Alternatives:", file=sys.stderr)
        print("  - kento lxc create --pve ...          # PVE-LXC mode, AppArmor-confined at host boundary",
              file=sys.stderr)
        print("  - kento vm create ...                 # VM mode, stronger isolation via QEMU",
              file=sys.stderr)
        print("  - kento lxc create --unconfined ...   # acknowledge tradeoff and proceed",
              file=sys.stderr)
        sys.exit(1)

    # Pre-validate PVE snippets storage (before any filesystem writes).
    # pve-vm always needs it; pve-lxc needs it when port/memory/cores set
    # (PVE strips lxc.hook.start-host, so we use hookscript: instead).
    _snippets_info = None
    if mode == "pve-vm":
        from kento.vm_hook import find_snippets_dir
        _snippets_info = find_snippets_dir()
    elif mode == "pve" and (port is not None or memory is not None
                             or cores is not None):
        from kento.vm_hook import find_snippets_dir
        _snippets_info = find_snippets_dir()

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
    else:
        # Scan both namespaces: for PVE-LXC/PVE-VM, container_id is the VMID
        # while `name` lives in kento-name, so a bare (base_dir / name).exists()
        # misses same-name duplicates across VMIDs. When --force is set, only
        # scan the current namespace — the user has opted in to duplicate names
        # across namespaces (bare shortcuts like `kento start foo` then require
        # explicit `kento lxc start foo` / `kento vm start foo`).
        if force:
            conflict = _scan_namespace(name, base_dir) is not None
        else:
            conflict = (_scan_namespace(name, LXC_BASE) is not None
                        or _scan_namespace(name, VM_BASE) is not None)
        if conflict:
            print(f"Error: instance name already taken: {name}", file=sys.stderr)
            sys.exit(1)

    # Validate mode-specific flags
    if vmid and mode not in ("pve", "pve-vm"):
        print(f"Error: --vmid cannot be used with {mode.upper()} mode", file=sys.stderr)
        sys.exit(1)
    if port is not None and mode in ("lxc", "pve"):
        if network["type"] != "bridge":
            print("Error: --port requires bridge networking for LXC/PVE mode",
                  file=sys.stderr)
            sys.exit(1)
    if port is not None and mode in ("vm", "pve-vm"):
        if network["type"] == "bridge":
            print("Error: --port cannot be used with bridge networking in VM mode",
                  file=sys.stderr)
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
        print(f"Error: instance already exists: {container_id}", file=sys.stderr)
        sys.exit(1)

    # Resolve layers (validates image exists)
    layers = resolve_layers(image)
    if not layers:
        print(f"Error: failed to resolve layer paths for {image}",
              file=sys.stderr)
        sys.exit(1)

    from kento.layers import create_image_hold
    create_image_hold(image, name)

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
    # the guest's ~/.ssh/authorized_keys on every start (target user controlled
    # by kento-ssh-user, defaulting to root).
    if ssh_key_contents is not None:
        (container_dir / "kento-authorized-keys").write_text(ssh_key_contents)
    if ssh_key_user != "root":
        (container_dir / "kento-ssh-user").write_text(ssh_key_user + "\n")

    # Generate or copy SSH host keys
    if ssh_host_keys:
        _generate_ssh_host_keys(container_dir / "ssh-host-keys")
    elif ssh_host_key_dir is not None:
        _copy_ssh_host_keys(Path(ssh_host_key_dir), container_dir / "ssh-host-keys")

    # Determine config mode (injection vs cloud-init)
    if config_mode == "auto":
        if detect_cloudinit(layers):
            effective_config_mode = "cloudinit"
        else:
            effective_config_mode = "injection"
    else:
        effective_config_mode = config_mode
        if config_mode == "cloudinit" and not detect_cloudinit(layers):
            print("Warning: --config-mode cloudinit specified but cloud-init not detected in image",
                  file=sys.stderr)

    # Write config mode metadata
    (container_dir / "kento-config-mode").write_text(effective_config_mode + "\n")

    # Generate cloud-init seed if in cloudinit mode
    if effective_config_mode == "cloudinit":
        host_key_dir = container_dir / "ssh-host-keys"
        write_seed(
            container_dir, name=name,
            ip=ip, gateway=gateway, dns=dns, searchdomain=searchdomain,
            timezone=timezone, env=env,
            ssh_keys=ssh_key_contents, ssh_key_user=ssh_key_user,
            ssh_host_key_dir=host_key_dir if host_key_dir.is_dir() else None,
        )

    if mode in ("vm", "pve-vm"):
        # Resolve MAC address for VM modes: user override wins, otherwise
        # auto-generate a stable deterministic MAC from the container name
        # (plain VM) or VMID (PVE-VM). Writing the result to kento-mac means
        # scrub/recreate keep the same MAC (external DHCP reservations work).
        from kento.vm import generate_mac
        if mac is None:
            if mode == "pve-vm":
                mac_value = generate_mac(str(vmid))
            else:
                mac_value = generate_mac(name)
        else:
            mac_value = mac
        (container_dir / "kento-mac").write_text(mac_value + "\n")

        # Resolve memory/cores: CLI > config file > hardcoded defaults
        from kento.defaults import get_vm_defaults
        vm_defaults = get_vm_defaults()
        effective_memory = memory if memory is not None else vm_defaults["memory"]
        effective_cores = cores if cores is not None else vm_defaults["cores"]
        (container_dir / "kento-memory").write_text(str(effective_memory) + "\n")
        (container_dir / "kento-cores").write_text(str(effective_cores) + "\n")

        # Write port mapping (usermode networking only)
        if network["type"] == "usermode":
            from kento.vm import allocate_port
            if port is None:
                host_port = allocate_port()
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
            hookscript_ref = write_snippets_wrapper(
                vmid, container_dir / "kento-hook",
                snippets_dir=_snippets_info[0],
                storage_name=_snippets_info[1],
            )

            # Write VMID reference
            (container_dir / "kento-vmid").write_text(str(vmid) + "\n")

            # Generate and write QM config
            qm_conf = write_qm_config(
                vmid,
                generate_qm_config(
                    name, vmid, container_dir,
                    hookscript_ref=hookscript_ref,
                    memory=effective_memory,
                    cores=effective_cores,
                    machine=vm_defaults["machine"],
                    kvm=vm_defaults["kvm"],
                    bridge=bridge,
                    net_type=network.get("type"),
                    mac=mac_value,
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
        # Port forwarding for LXC/PVE modes
        if port is not None:
            from kento.vm import allocate_port
            if port == "auto":
                host_port = allocate_port()
                guest_port = 22
            else:
                host_port, guest_port = port.split(":")
                host_port, guest_port = int(host_port), int(guest_port)
            (container_dir / "kento-port").write_text(f"{host_port}:{guest_port}\n")

        # Persist memory/cores so the start-host hook can propagate the limit
        # into the inner ns cgroup on PVE-LXC (outer cgroup gets the ceiling
        # from PVE's `memory:`/`cpulimit:`, but processes live in ns/ and
        # read "max" without this).
        if memory is not None:
            (container_dir / "kento-memory").write_text(str(memory) + "\n")
        if cores is not None:
            (container_dir / "kento-cores").write_text(str(cores) + "\n")

        # Generate hook (LXC/PVE only) + inject.sh (shared with VM/PVE-VM modes)
        write_hook(container_dir, layers, name, state_dir)
        write_inject(container_dir)

        # Generate config
        if mode == "pve":
            hookscript_ref = None
            if _snippets_info is not None:
                from kento.lxc_hook import write_lxc_snippets_wrapper
                hookscript_ref = write_lxc_snippets_wrapper(
                    vmid, container_dir / "kento-hook",
                    snippets_dir=_snippets_info[0],
                    storage_name=_snippets_info[1],
                )
            pve_conf = write_pve_config(
                vmid,
                generate_pve_config(name, vmid, container_dir, bridge=bridge,
                                    net_type=network.get("type"),
                                    nesting=nesting, ip=ip,
                                    gateway=gateway, nameserver=dns,
                                    searchdomain=searchdomain,
                                    timezone=timezone, env=env,
                                    port=port,
                                    memory=memory, cores=cores,
                                    hookscript_ref=hookscript_ref)
            )
            config_path = str(pve_conf)
        else:
            (container_dir / "config").write_text(
                generate_config(name, container_dir, bridge=bridge,
                                net_type=network.get("type"),
                                nesting=nesting,
                                ip=ip, gateway=gateway, env=env,
                                port=port,
                                memory=memory, cores=cores,
                                unconfined=unconfined, mode=mode)
            )
            config_path = f"{container_dir}/config"

        print(f"\nContainer created: {name}")
        print(f"  Image:   {image}")
        print(f"  Bridge:  {bridge}")
        if mode == "pve":
            print(f"  VMID:    {vmid}")
        if port is not None:
            print(f"  Port:    {host_port}:{guest_port}")
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
