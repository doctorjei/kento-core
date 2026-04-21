"""Scrub a kento-managed container back to clean OCI state."""

import shutil
import subprocess
import sys
from pathlib import Path

from kento import is_running, read_mode, require_root, resolve_container
from kento.hook import write_hook
from kento.layers import resolve_layers


def reset(name: str, *, container_dir: Path | None = None, mode: str | None = None) -> None:
    require_root()

    if container_dir is None:
        container_dir = resolve_container(name)

    if mode is None:
        # Detect mode (default lxc for containers created before mode tracking)
        mode = read_mode(container_dir)

    # Refuse if running
    if is_running(container_dir, mode):
        print(f"Error: container is running. Stop it first: kento container shutdown {name}",
              file=sys.stderr)
        sys.exit(1)

    # Read state dir
    state_file = container_dir / "kento-state"
    state_dir = Path(state_file.read_text().strip()) if state_file.is_file() else container_dir

    # Unmount rootfs if mounted
    rootfs = container_dir / "rootfs"
    if subprocess.run(["mountpoint", "-q", str(rootfs)],
                      capture_output=True).returncode == 0:
        result = subprocess.run(["umount", str(rootfs)])
        if result.returncode != 0:
            print(f"Error: failed to unmount {rootfs}. Is the container still running?",
                  file=sys.stderr)
            sys.exit(1)

    # Clear writable layer
    upper = state_dir / "upper"
    work = state_dir / "work"
    if upper.exists():
        shutil.rmtree(upper)
    if work.exists():
        shutil.rmtree(work)
    upper.mkdir(parents=True)
    work.mkdir(parents=True)

    # Clean up stale port forwarding state (safety net)
    portfwd_active = container_dir / "kento-portfwd-active"
    portfwd_active.unlink(missing_ok=True)

    # Re-inject guest config from kento metadata
    from kento.create import (_inject_network_config, _inject_hostname,
                              _inject_timezone, _inject_env)

    # Hostname
    name_file = container_dir / "kento-name"
    if name_file.is_file():
        _inject_hostname(state_dir, name_file.read_text().strip())

    # Network (static IP + searchdomain)
    net_file = container_dir / "kento-net"
    if net_file.is_file():
        net_cfg = {}
        for line in net_file.read_text().strip().splitlines():
            k, v = line.split("=", 1)
            net_cfg[k] = v
        if "ip" in net_cfg:
            _inject_network_config(state_dir, net_cfg["ip"],
                                   net_cfg.get("gateway"), net_cfg.get("dns"),
                                   net_cfg.get("searchdomain"), mode=mode)
        elif net_cfg.get("dns") or net_cfg.get("searchdomain"):
            resolved_dir = state_dir / "upper" / "etc" / "systemd" / "resolved.conf.d"
            resolved_dir.mkdir(parents=True, exist_ok=True)
            lines = ["[Resolve]"]
            if net_cfg.get("dns"):
                lines.append(f"DNS={net_cfg['dns']}")
            if net_cfg.get("searchdomain"):
                lines.append(f"Domains={net_cfg['searchdomain']}")
            lines.append("")
            (resolved_dir / "90-kento.conf").write_text("\n".join(lines))

    # Timezone
    tz_file = container_dir / "kento-tz"
    if tz_file.is_file():
        _inject_timezone(state_dir, tz_file.read_text().strip())

    # Environment variables
    env_file = container_dir / "kento-env"
    if env_file.is_file():
        _inject_env(state_dir, env_file.read_text().strip().splitlines())

    # Re-resolve layers from image
    image = (container_dir / "kento-image").read_text().strip()
    layers = resolve_layers(image)
    (container_dir / "kento-layers").write_text(layers + "\n")

    # Regenerate hook (LXC/PVE only — VM mode has no hook)
    if mode != "vm":
        write_hook(container_dir, layers, name, state_dir)

    print(f"Scrubbed: {name}")
    print("  Writable layer cleared, layers re-resolved from image.")
