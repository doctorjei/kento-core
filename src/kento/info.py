"""Show container details."""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from kento import is_running, read_mode, require_root


def _read_meta(container_dir: Path, filename: str) -> str | None:
    """Read a kento metadata file, return stripped content or None."""
    f = container_dir / filename
    return f.read_text().strip() if f.is_file() else None


def _get_size(path: Path) -> str:
    """Get human-readable size of a directory."""
    result = subprocess.run(
        ["du", "-sh", str(path)],
        capture_output=True, text=True,
    )
    return result.stdout.split()[0] if result.returncode == 0 else "?"


def _get_ssh_host_key_fingerprints(
    container_dir: Path,
) -> tuple[dict[str, str], bool]:
    """Read SSH host key fingerprints from ssh-host-keys/ directory.

    Returns (fingerprints_dict, has_keys) where:
    - fingerprints_dict maps key type (e.g. "rsa") to fingerprint string
    - has_keys is True when .pub files exist (even if ssh-keygen failed)

    fingerprints_dict is empty if no host keys, no .pub files, or
    ssh-keygen is unavailable.
    """
    keys_dir = container_dir / "ssh-host-keys"
    if not keys_dir.is_dir():
        return {}, False

    pub_files = sorted(keys_dir.glob("*.pub"))
    if not pub_files:
        return {}, False

    fingerprints: dict[str, str] = {}
    for pub_path in pub_files:
        # Extract key type from filename: ssh_host_rsa_key.pub -> rsa
        stem = pub_path.stem  # e.g. ssh_host_rsa_key
        parts = stem.split("_")
        # Expected: ssh_host_<type>_key
        if len(parts) >= 4 and parts[0] == "ssh" and parts[1] == "host":
            key_type = "_".join(parts[2:-1])  # handles multi-word types
        else:
            key_type = stem  # fallback to full stem

        try:
            result = subprocess.run(
                ["ssh-keygen", "-lf", str(pub_path)],
                capture_output=True, text=True,
            )
        except FileNotFoundError:
            return {}, True  # has keys but ssh-keygen not available

        if result.returncode == 0 and result.stdout.strip():
            # Output: "<bits> <fingerprint> <comment> (<type>)"
            fields = result.stdout.strip().split()
            if len(fields) >= 2:
                fingerprints[key_type] = fields[1]

    return fingerprints, True


def info(name: str, *, container_dir: Path, mode: str,
         as_json: bool = False, verbose: bool = False) -> None:
    """Display container information."""

    # Gather metadata
    data = {}
    data["name"] = _read_meta(container_dir, "kento-name") or name
    data["image"] = _read_meta(container_dir, "kento-image") or "unknown"
    data["mode"] = mode
    data["type"] = "VM" if mode in ("vm", "pve-vm") else "LXC"
    data["status"] = "running" if is_running(container_dir, mode) else "stopped"
    data["directory"] = str(container_dir)

    # State dir
    state_text = _read_meta(container_dir, "kento-state")
    state_dir = Path(state_text) if state_text else container_dir
    data["state_directory"] = str(state_dir)

    # Optional metadata
    config_mode = _read_meta(container_dir, "kento-config-mode")
    if config_mode:
        data["config_mode"] = config_mode

    vmid = _read_meta(container_dir, "kento-vmid")
    if vmid:
        data["vmid"] = int(vmid)

    port = _read_meta(container_dir, "kento-port")
    if port:
        data["port"] = port

    net = _read_meta(container_dir, "kento-net")
    if net:
        data["network"] = net

    mac = _read_meta(container_dir, "kento-mac")
    if mac:
        data["mac"] = mac

    tz = _read_meta(container_dir, "kento-tz")
    if tz:
        data["timezone"] = tz

    ssh_user = _read_meta(container_dir, "kento-ssh-user") or "root"
    data["ssh_user"] = ssh_user

    env = _read_meta(container_dir, "kento-env")
    if env:
        data["environment"] = env.splitlines()

    # Layers
    layers_text = _read_meta(container_dir, "kento-layers")
    if layers_text:
        layer_paths = layers_text.split(":")
        data["layer_count"] = len(layer_paths)
    else:
        layer_paths = []
        data["layer_count"] = 0

    # Created timestamp (directory mtime)
    try:
        mtime = os.path.getmtime(container_dir)
        data["created"] = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        data["created"] = "unknown"

    # SSH host key fingerprints
    fingerprints, has_host_keys = _get_ssh_host_key_fingerprints(container_dir)
    data["ssh_host_key_fingerprints"] = fingerprints
    # Track whether keys exist but ssh-keygen was missing (for human output)
    _ssh_keygen_missing = has_host_keys and not fingerprints

    # Verbose additions
    if verbose:
        upper = state_dir / "upper"
        if upper.is_dir():
            data["upper_size"] = _get_size(upper)

        if layer_paths:
            data["layers"] = layer_paths
            # Individual layer sizes
            sizes = []
            for lp in layer_paths:
                p = Path(lp)
                if p.is_dir():
                    sizes.append(_get_size(p))
            data["layer_sizes"] = sizes

    # Output
    if as_json:
        print(json.dumps(data, indent=2))
    else:
        _print_human(data, verbose,
                     ssh_keygen_missing=_ssh_keygen_missing)


def _print_human(data: dict, verbose: bool, *,
                 ssh_keygen_missing: bool = False) -> None:
    """Print container info in human-readable format."""
    print(f"Name:       {data['name']}")
    print(f"Image:      {data['image']}")
    print(f"Mode:       {data['mode']} ({data['type']})")
    print(f"Status:     {data['status']}")
    print(f"Created:    {data['created']}")
    print(f"Directory:  {data['directory']}")
    print(f"State:      {data['state_directory']}")

    if "config_mode" in data:
        print(f"Config:     {data['config_mode']}")

    if "vmid" in data:
        print(f"VMID:       {data['vmid']}")
    if "port" in data:
        print(f"Port:       {data['port']}")
    if "network" in data:
        print(f"Network:    {data['network']}")
    if "mac" in data:
        print(f"MAC:        {data['mac']}")
    if "timezone" in data:
        print(f"Timezone:   {data['timezone']}")
    if data.get("ssh_user", "root") != "root":
        print(f"SSH user:   {data['ssh_user']}")
    if "environment" in data:
        print(f"Env:        {', '.join(data['environment'])}")

    print(f"Layers:     {data['layer_count']}")

    fp = data.get("ssh_host_key_fingerprints", {})
    if fp:
        print("SSH host key fingerprints:")
        # Display order: rsa, ecdsa, ed25519, then any others alphabetically
        order = ["rsa", "ecdsa", "ed25519"]
        ordered_keys = [k for k in order if k in fp]
        ordered_keys += sorted(k for k in fp if k not in order)
        for kt in ordered_keys:
            label = kt.upper()
            print(f"  {label + ':':<10} {fp[kt]}")
    elif ssh_keygen_missing:
        print("SSH host key fingerprints:")
        print("  ssh-keygen not found, cannot display fingerprints")

    if verbose:
        if "upper_size" in data:
            print(f"Upper size: {data['upper_size']}")
        if "layers" in data:
            print("Layer paths:")
            for i, lp in enumerate(data["layers"]):
                size = data.get("layer_sizes", [])[i] if i < len(data.get("layer_sizes", [])) else "?"
                print(f"  [{i}] {lp} ({size})")
