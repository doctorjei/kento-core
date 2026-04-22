"""Scrub a kento-managed instance back to clean OCI state."""

import shutil
import subprocess
import sys
from pathlib import Path

from kento import is_running, read_mode, require_root, resolve_container
from kento.hook import write_hook
from kento.layers import resolve_layers
from kento.vm_hook import write_vm_hook


def reset(name: str, *, container_dir: Path | None = None, mode: str | None = None) -> None:
    require_root()

    if container_dir is None:
        container_dir = resolve_container(name)

    if mode is None:
        # Detect mode (default lxc for containers created before mode tracking)
        mode = read_mode(container_dir)

    # Refuse if running
    if is_running(container_dir, mode):
        print(f"Error: instance is running. Stop it first: kento stop {name}",
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

    # Regenerate cloud-init seed if in cloudinit mode
    config_mode_file = container_dir / "kento-config-mode"
    if config_mode_file.is_file() and config_mode_file.read_text().strip() == "cloudinit":
        from kento.cloudinit import write_seed
        # Gather config from metadata files
        net_file_ci = container_dir / "kento-net"
        net_cfg_ci = {}
        if net_file_ci.is_file():
            for line in net_file_ci.read_text().strip().splitlines():
                k, v = line.split("=", 1)
                net_cfg_ci[k] = v
        tz_file_ci = container_dir / "kento-tz"
        env_file_ci = container_dir / "kento-env"
        ci_ssh_keys = None
        auth_keys_file = container_dir / "kento-authorized-keys"
        if auth_keys_file.is_file():
            ci_ssh_keys = auth_keys_file.read_text()
        ssh_user_file = container_dir / "kento-ssh-user"
        ci_ssh_user = ssh_user_file.read_text().strip() if ssh_user_file.is_file() else "root"
        ci_host_key_dir = container_dir / "ssh-host-keys"
        name_file_ci = container_dir / "kento-name"
        write_seed(
            container_dir,
            name=name_file_ci.read_text().strip() if name_file_ci.is_file() else name,
            ip=net_cfg_ci.get("ip"),
            gateway=net_cfg_ci.get("gateway"),
            dns=net_cfg_ci.get("dns"),
            searchdomain=net_cfg_ci.get("searchdomain"),
            timezone=tz_file_ci.read_text().strip() if tz_file_ci.is_file() else None,
            env=env_file_ci.read_text().strip().splitlines() if env_file_ci.is_file() else None,
            ssh_keys=ci_ssh_keys, ssh_key_user=ci_ssh_user,
            ssh_host_key_dir=ci_host_key_dir if ci_host_key_dir.is_dir() else None,
        )

    # Re-resolve layers from image
    image = (container_dir / "kento-image").read_text().strip()
    layers = resolve_layers(image)
    (container_dir / "kento-layers").write_text(layers + "\n")

    # Regenerate the mode-appropriate hook so fresh image paths land in it.
    # Plain VM mode has no hook (QEMU starts from Python), but pve-vm does —
    # and its hook shape (VMID $1 / PHASE $2) is incompatible with the LXC
    # hook shape (uses $3 for hook type), so using the wrong writer here
    # silently breaks `qm start` after scrub.
    if mode == "pve-vm":
        write_vm_hook(container_dir, layers, name, state_dir)
    elif mode != "vm":
        write_hook(container_dir, layers, name, state_dir)

    # PVE-LXC: regenerate snippets wrapper when the container has
    # port/memory/cores metadata. The wrapper path is derived from the
    # VMID (container_dir.name) and the kento-hook path, so this is
    # idempotent — we rewrite the same file. Done for consistency in
    # case the kento-hook path ever changes across upgrades.
    if mode == "pve":
        if any((container_dir / f).is_file()
               for f in ("kento-port", "kento-memory", "kento-cores")):
            from kento.lxc_hook import write_lxc_snippets_wrapper
            write_lxc_snippets_wrapper(
                int(container_dir.name),
                container_dir / "kento-hook",
            )

    print(f"Scrubbed: {name}")
    print("  Writable layer cleared, layers re-resolved from image.")
