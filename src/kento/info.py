"""Show instance details."""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from kento import is_running


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


def _read_passthrough_args(container_dir: Path, filename: str) -> list[str]:
    """Read a pass-through args file (kento-qemu-args or kento-pve-args).

    Returns a list of non-empty lines. Absent file returns []. Written at
    create time (v1.2.0 Phase B1); surfaced here for info output.
    """
    f = container_dir / filename
    if not f.is_file():
        return []
    return [line for line in f.read_text().splitlines() if line]


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
         as_json: bool = False, verbose: bool = False) -> str:
    """Return container information as a rendered string."""

    # Gather metadata
    data = {}
    data["name"] = _read_meta(container_dir, "kento-name") or name
    data["image"] = _read_meta(container_dir, "kento-image") or "unknown"
    # Normalize the raw mode ('pve' -> 'pve-lxc') so inspect --json and
    # list --json agree on the mode string. type stays the LXC/VM family.
    data["mode"] = "pve-lxc" if mode == "pve" else mode
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

    nesting = _read_meta(container_dir, "kento-nesting")
    if nesting is not None:
        data["nesting"] = (nesting == "1")

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

    # Pass-through flags (v1.2.0 Phase B4). Always emitted in JSON (empty
    # list when absent) so machine consumers get a stable schema. Human
    # output surfaces them only under --verbose and only when non-empty.
    data["qemu_args"] = _read_passthrough_args(container_dir, "kento-qemu-args")
    data["pve_args"] = _read_passthrough_args(container_dir, "kento-pve-args")
    data["lxc_args"] = _read_passthrough_args(container_dir, "kento-lxc-args")

    # Verbose additions
    if verbose:
        upper = state_dir / "upper"
        if upper.is_dir():
            data["upper_size"] = _get_size(upper)

        if layer_paths:
            data["layers"] = layer_paths
            # Individual layer sizes, positionally aligned with layers:
            # absent layer dirs get a None placeholder so layer_sizes[i]
            # always corresponds to layers[i].
            sizes = []
            for lp in layer_paths:
                p = Path(lp)
                sizes.append(_get_size(p) if p.is_dir() else None)
            data["layer_sizes"] = sizes

    # Output
    if as_json:
        return json.dumps(data, indent=2)
    return _format_human(data, verbose, ssh_keygen_missing=_ssh_keygen_missing)


def _format_human(data: dict, verbose: bool, *,
                  ssh_keygen_missing: bool = False) -> str:
    """Return container info as a human-readable string."""
    lines = []
    lines.append(f"Name:       {data['name']}")
    lines.append(f"Image:      {data['image']}")
    lines.append(f"Mode:       {data['mode']} ({data['type']})")
    lines.append(f"Status:     {data['status']}")
    lines.append(f"Created:    {data['created']}")
    lines.append(f"Directory:  {data['directory']}")
    lines.append(f"State:      {data['state_directory']}")

    if "config_mode" in data:
        lines.append(f"Config:     {data['config_mode']}")

    if "vmid" in data:
        lines.append(f"VMID:       {data['vmid']}")
    if "port" in data:
        # kento-port may now hold N forward specs (one per line, §5.7A). The
        # JSON wire keeps the raw string verbatim (Phase 6 owns any projection);
        # the human display lists each forward so N lines don't print mangled.
        port_specs = [s for s in str(data["port"]).splitlines() if s.strip()]
        if len(port_specs) <= 1:
            lines.append(f"Port:       {data['port']}")
        else:
            lines.append(f"Port:       {port_specs[0]}")
            for spec in port_specs[1:]:
                lines.append(f"            {spec}")
    if "network" in data:
        lines.append(f"Network:    {data['network']}")
    if "mac" in data:
        lines.append(f"MAC:        {data['mac']}")
    if "nesting" in data:
        lines.append(f"Nesting:    {'allowed' if data['nesting'] else 'disabled'}")
    if "timezone" in data:
        lines.append(f"Timezone:   {data['timezone']}")
    if data.get("ssh_user", "root") != "root":
        lines.append(f"SSH user:   {data['ssh_user']}")
    if "environment" in data:
        lines.append(f"Env:        {', '.join(data['environment'])}")

    lines.append(f"Layers:     {data['layer_count']}")

    fp = data.get("ssh_host_key_fingerprints", {})
    if fp:
        lines.append("SSH host key fingerprints:")
        # Display order: rsa, ecdsa, ed25519, then any others alphabetically
        order = ["rsa", "ecdsa", "ed25519"]
        ordered_keys = [k for k in order if k in fp]
        ordered_keys += sorted(k for k in fp if k not in order)
        for kt in ordered_keys:
            label = kt.upper()
            lines.append(f"  {label + ':':<10} {fp[kt]}")
    elif ssh_keygen_missing:
        lines.append("SSH host key fingerprints:")
        lines.append("  ssh-keygen not found, cannot display fingerprints")

    if verbose:
        if "upper_size" in data:
            lines.append(f"Upper size: {data['upper_size']}")
        if "layers" in data:
            lines.append("Layer paths:")
            layer_sizes = data.get("layer_sizes", [])
            for i, lp in enumerate(data["layers"]):
                size = layer_sizes[i] if i < len(layer_sizes) else None
                if size is None:
                    size = "missing"
                lines.append(f"  [{i}] {lp} ({size})")

        qemu_args = data.get("qemu_args", [])
        pve_args = data.get("pve_args", [])
        lxc_args = data.get("lxc_args", [])
        if qemu_args or pve_args or lxc_args:
            lines.append("Pass-through flags:")
            if qemu_args:
                lines.append("  --qemu-arg:")
                for line in qemu_args:
                    lines.append(f"    {line}")
            if pve_args:
                lines.append("  --pve-arg:")
                for line in pve_args:
                    lines.append(f"    {line}")
            if lxc_args:
                lines.append("  --lxc-arg:")
                for line in lxc_args:
                    lines.append(f"    {line}")

    return "\n".join(lines)
