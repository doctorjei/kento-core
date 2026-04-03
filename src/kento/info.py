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
    vmid = _read_meta(container_dir, "kento-vmid")
    if vmid:
        data["vmid"] = int(vmid)

    port = _read_meta(container_dir, "kento-port")
    if port:
        data["port"] = port

    net = _read_meta(container_dir, "kento-net")
    if net:
        data["network"] = net

    tz = _read_meta(container_dir, "kento-tz")
    if tz:
        data["timezone"] = tz

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
        _print_human(data, verbose)


def _print_human(data: dict, verbose: bool) -> None:
    """Print container info in human-readable format."""
    print(f"Name:       {data['name']}")
    print(f"Image:      {data['image']}")
    print(f"Mode:       {data['mode']} ({data['type']})")
    print(f"Status:     {data['status']}")
    print(f"Created:    {data['created']}")
    print(f"Directory:  {data['directory']}")
    print(f"State:      {data['state_directory']}")

    if "vmid" in data:
        print(f"VMID:       {data['vmid']}")
    if "port" in data:
        print(f"Port:       {data['port']}")
    if "network" in data:
        print(f"Network:    {data['network']}")
    if "timezone" in data:
        print(f"Timezone:   {data['timezone']}")
    if "environment" in data:
        print(f"Env:        {', '.join(data['environment'])}")

    print(f"Layers:     {data['layer_count']}")

    if verbose:
        if "upper_size" in data:
            print(f"Upper size: {data['upper_size']}")
        if "layers" in data:
            print("Layer paths:")
            for i, lp in enumerate(data["layers"]):
                size = data.get("layer_sizes", [])[i] if i < len(data.get("layer_sizes", [])) else "?"
                print(f"  [{i}] {lp} ({size})")
