"""Create an LXC container backed by an OCI image."""

import subprocess
import sys
from pathlib import Path

from kento import LXC_BASE, require_root
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


def create(name: str, image: str, *, bridge: str = "lxcbr0",
           memory: int = 0, cores: int = 0, nesting: bool = True,
           start: bool = False) -> None:
    require_root()

    lxc_dir = LXC_BASE / name

    if lxc_dir.exists():
        print(f"Error: container already exists: {name}", file=sys.stderr)
        sys.exit(1)

    # Resolve layers (validates image exists)
    layers = resolve_layers(image)
    if not layers:
        print(f"Error: failed to resolve layer paths for {image}",
              file=sys.stderr)
        sys.exit(1)

    # Create directory structure
    (lxc_dir / "rootfs").mkdir(parents=True)
    (lxc_dir / "upper").mkdir()
    (lxc_dir / "work").mkdir()

    # Write image reference and layer paths
    (lxc_dir / "kento-image").write_text(image + "\n")
    (lxc_dir / "kento-layers").write_text(layers + "\n")

    # Generate hook and config
    write_hook(lxc_dir, layers, name)
    (lxc_dir / "config").write_text(
        generate_config(name, lxc_dir, bridge=bridge, memory=memory,
                        cores=cores, nesting=nesting)
    )

    print(f"\nContainer created: {name}")
    print(f"  Image:   {image}")
    print(f"  Bridge:  {bridge}")
    if memory:
        print(f"  Memory:  {memory} MB")
    if cores:
        print(f"  Cores:   {cores}")
    print(f"  Nesting: {nesting}")
    print(f"  Config:  {lxc_dir}/config")

    if start:
        print("\nStarting container...")
        subprocess.run(["lxc-start", "-n", name], check=True)
        print("  Status: running")
    else:
        print(f"  Status: stopped (use 'lxc-start -n {name}' to boot)")
