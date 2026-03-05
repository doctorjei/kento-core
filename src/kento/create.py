"""Create an LXC container backed by an OCI image."""

import subprocess
import sys
from pathlib import Path

from kento import LXC_BASE, require_root, upper_base, detect_mode
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


def create(name: str, image: str, *, bridge: str | None = None,
           memory: int = 0, cores: int = 0, nesting: bool = True,
           start: bool = False, mode: str | None = None,
           vmid: int = 0) -> None:
    require_root()

    # Resolve mode
    mode = detect_mode(mode)

    # Validate --vmid not used with --lxc
    if vmid and mode == "lxc":
        print("Error: --vmid cannot be used with LXC mode", file=sys.stderr)
        sys.exit(1)

    # Resolve bridge default per mode
    if bridge is None:
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
    else:
        container_id = name
        print("Mode: lxc")

    lxc_dir = LXC_BASE / container_id

    if lxc_dir.exists():
        print(f"Error: container already exists: {container_id}", file=sys.stderr)
        sys.exit(1)

    # Resolve layers (validates image exists)
    layers = resolve_layers(image)
    if not layers:
        print(f"Error: failed to resolve layer paths for {image}",
              file=sys.stderr)
        sys.exit(1)

    # Create directory structure — upper/work may be outside lxc_dir for sudo users
    state_dir = upper_base(container_id)
    (lxc_dir / "rootfs").mkdir(parents=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "upper").mkdir(exist_ok=True)
    (state_dir / "work").mkdir(exist_ok=True)

    # Write image reference, layer paths, state dir, and mode
    (lxc_dir / "kento-image").write_text(image + "\n")
    (lxc_dir / "kento-layers").write_text(layers + "\n")
    (lxc_dir / "kento-state").write_text(str(state_dir) + "\n")
    (lxc_dir / "kento-mode").write_text(mode + "\n")

    # Generate hook
    write_hook(lxc_dir, layers, name, state_dir)

    # Generate config
    if mode == "pve":
        pve_memory = memory if memory else 512
        pve_cores = cores if cores else 1
        pve_conf = write_pve_config(
            vmid,
            generate_pve_config(name, vmid, lxc_dir, bridge=bridge,
                                memory=pve_memory, cores=pve_cores,
                                nesting=nesting)
        )
        config_path = str(pve_conf)
    else:
        (lxc_dir / "config").write_text(
            generate_config(name, lxc_dir, bridge=bridge, memory=memory,
                            cores=cores, nesting=nesting)
        )
        config_path = f"{lxc_dir}/config"

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
        if mode == "pve":
            subprocess.run(["pct", "start", str(vmid)], check=True)
        else:
            subprocess.run(["lxc-start", "-n", name], check=True)
        print("  Status: running")
    else:
        if mode == "pve":
            print(f"  Status: stopped (use 'pct start {vmid}' to boot)")
        else:
            print(f"  Status: stopped (use 'lxc-start -n {name}' to boot)")
