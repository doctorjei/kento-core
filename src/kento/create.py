"""Create a container backed by an OCI image."""

import subprocess
import sys
from pathlib import Path

from kento import LXC_BASE, VM_BASE, require_root, upper_base, detect_mode, sanitize_image_name, next_instance_name
from kento.hook import write_hook
from kento.layers import resolve_layers


def generate_config(name: str, lxc_dir: Path, *, bridge: str = "lxcbr0",
                    memory: int = 0, cores: int = 0,
                    nesting: bool = True) -> str:
    hook = lxc_dir / "kento-hook"
    lines = [
        f"lxc.uts.name = {name}",
        f"lxc.rootfs.path = dir:{lxc_dir}/rootfs",
        "",
        "lxc.hook.version = 1",
        f"lxc.hook.pre-start = {hook}",
        f"lxc.hook.post-stop = {hook}",
        "",
        "lxc.net.0.type = veth",
        f"lxc.net.0.link = {bridge}",
        "lxc.net.0.flags = up",
        "",
        "lxc.init.cmd = /sbin/init",
        "lxc.mount.auto = proc:rw sys:rw cgroup:rw",
        "lxc.apparmor.profile = unconfined",
        "lxc.tty.max = 4",
        "lxc.pty.max = 1024",
    ]

    if memory:
        lines.append(f"lxc.cgroup2.memory.max = {memory}M")
    if cores:
        lines.append(f"lxc.cgroup2.cpuset.cpus = 0-{cores - 1}")
    if nesting:
        lines.append("lxc.include = /usr/share/lxc/config/nesting.conf")

    return "\n".join(lines) + "\n"


def create(image: str, *, name: str | None = None, bridge: str | None = None,
           memory: int = 0, cores: int = 0, nesting: bool = True,
           start: bool = False, mode: str | None = None,
           vmid: int = 0, port: str | None = None) -> None:
    require_root()

    # Resolve mode
    mode = detect_mode(mode)

    # Determine base directory for this mode
    base_dir = VM_BASE if mode == "vm" else LXC_BASE

    # Resolve container name
    if name is None:
        base_name = sanitize_image_name(image)
        name = next_instance_name(base_name, base_dir)
    elif (base_dir / name).exists():
        print(f"Error: container name already taken: {name}", file=sys.stderr)
        sys.exit(1)

    # Validate mode-specific flags
    if vmid and mode != "pve":
        print(f"Error: --vmid cannot be used with {mode.upper()} mode", file=sys.stderr)
        sys.exit(1)
    if port is not None and mode != "vm":
        print(f"Error: --port cannot be used with {mode.upper()} mode", file=sys.stderr)
        sys.exit(1)
    if mode == "vm":
        if bridge is not None:
            print("Warning: --bridge is ignored in VM mode", file=sys.stderr)
        if not nesting:
            print("Warning: --nesting is ignored in VM mode", file=sys.stderr)

    # Resolve bridge default per mode (not applicable for VM)
    if mode != "vm" and bridge is None:
        bridge = "vmbr0" if mode == "pve" else "lxcbr0"

    # Resolve container_id for directory paths
    if mode == "pve":
        from kento.pve import next_vmid, validate_vmid, generate_pve_config, write_pve_config
        if vmid:
            validate_vmid(vmid)
        else:
            vmid = next_vmid()
        container_id = str(vmid)
        print(f"Mode: pve (VMID {vmid})")
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
    state_dir = upper_base(container_id, base_dir if mode == "vm" else None)
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

    if mode == "vm":
        # Write port mapping
        from kento.vm import allocate_port
        if port is None:
            host_port = allocate_port(base_dir)
            guest_port = 22
        else:
            host_port, guest_port = port.split(":")
            host_port, guest_port = int(host_port), int(guest_port)
        (container_dir / "kento-port").write_text(f"{host_port}:{guest_port}\n")

        print(f"\nContainer created: {name}")
        print(f"  Image:   {image}")
        print(f"  Port:    {host_port}:{guest_port}")
        print(f"  Dir:     {container_dir}")
    else:
        # Generate hook (LXC/PVE only)
        write_hook(container_dir, layers, name, state_dir)

        # Generate config
        if mode == "pve":
            pve_memory = memory if memory else 512
            pve_cores = cores if cores else 1
            pve_conf = write_pve_config(
                vmid,
                generate_pve_config(name, vmid, container_dir, bridge=bridge,
                                    memory=pve_memory, cores=pve_cores,
                                    nesting=nesting)
            )
            config_path = str(pve_conf)
        else:
            (container_dir / "config").write_text(
                generate_config(name, container_dir, bridge=bridge, memory=memory,
                                cores=cores, nesting=nesting)
            )
            config_path = f"{container_dir}/config"

        print(f"\nContainer created: {name}")
        print(f"  Image:   {image}")
        print(f"  Bridge:  {bridge}")
        if mode == "pve":
            print(f"  VMID:    {vmid}")
            print(f"  Memory:  {pve_memory} MB")
            print(f"  Cores:   {pve_cores}")
        else:
            if memory:
                print(f"  Memory:  {memory} MB")
            if cores:
                print(f"  Cores:   {cores}")
        print(f"  Nesting: {nesting}")
        print(f"  Config:  {config_path}")

    if start:
        print("\nStarting container...")
        if mode == "vm":
            from kento.vm import start_vm
            start_vm(container_dir, name)
        elif mode == "pve":
            subprocess.run(["pct", "start", str(vmid)], check=True)
        else:
            subprocess.run(["lxc-start", "-n", name], check=True)
        print("  Status: running")
    else:
        print(f"  Status: stopped (use 'kento container start {name}' to boot)")
