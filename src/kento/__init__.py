"""Kento — compose OCI images into LXC system containers via overlayfs."""

import os
import pwd
import sys
from pathlib import Path

__version__ = "0.9.0"

LXC_BASE = Path("/var/lib/lxc")
VM_BASE = Path("/var/lib/kento/vm")


def _bridge_exists(name: str) -> bool:
    """Check if a network bridge interface exists."""
    return Path(f"/sys/class/net/{name}").is_dir()


def detect_bridge() -> str | None:
    """Detect the first available network bridge.

    Checks vmbr0 (PVE default), then lxcbr0 (LXC default).
    Returns the bridge name or None if no bridge found.
    """
    for name in ("vmbr0", "lxcbr0"):
        if _bridge_exists(name):
            return name
    return None


def resolve_network(net_type: str | None, bridge_name: str | None,
                    mode: str, port: str | None = None) -> dict:
    """Resolve network configuration for container/VM creation.

    Returns dict with keys: type, bridge, port
    - type: "bridge", "host", "usermode", or "none"
    - bridge: bridge name (str) or None
    - port: "host:guest" (str) or None
    """
    # Port implies usermode if no explicit network set (VM/PVE-VM only).
    # For LXC/PVE, port forwarding uses iptables DNAT which requires bridge.
    if port is not None and net_type is None:
        if mode in ("vm", "pve-vm"):
            net_type = "usermode"

    # Auto-detect if no network type specified
    if net_type is None:
        bridge = detect_bridge()
        if bridge:
            net_type = "bridge"
            bridge_name = bridge
            print(f"Network: using bridge {bridge}")
        elif mode in ("vm", "pve-vm"):
            net_type = "usermode"
            print("Network: no bridge found, using usermode networking")
        else:
            net_type = "none"
            print("Network: no bridge found, networking disabled")
    elif net_type == "bridge" and bridge_name is None:
        # --network bridge without name: auto-detect bridge
        bridge_name = detect_bridge()
        if bridge_name is None:
            print("Error: --network bridge specified but no bridge interface found "
                  "(checked vmbr0, lxcbr0)", file=sys.stderr)
            sys.exit(1)
        print(f"Network: using bridge {bridge_name}")

    return {
        "type": net_type,
        "bridge": bridge_name,
        "port": port,
    }


def read_mode(container_dir: Path, default: str = "lxc") -> str:
    """Read the kento-mode file from a container directory."""
    mode_file = container_dir / "kento-mode"
    return mode_file.read_text().strip() if mode_file.is_file() else default


def require_root() -> None:
    if os.getuid() != 0:
        print("Error: must run as root", file=sys.stderr)
        sys.exit(1)


def detect_mode(force: str | None = None) -> str:
    """Return 'pve', 'lxc', or 'vm' based on environment or explicit override.

    When force is set (e.g. 'vm'), returns it directly.
    Otherwise auto-detects PVE vs plain LXC (VM is never auto-detected).
    """
    if force:
        return force
    from kento.pve import is_pve
    return "pve" if is_pve() else "lxc"


def upper_base(name: str, base: Path | None = None) -> Path:
    """Return the base directory for a container's upper and work dirs.

    When run via sudo, uses the invoking user's XDG data directory
    (~user/.local/share/kento/<name>/) so writable state is per-user.
    When run as root directly, uses the provided base (or LXC_BASE)/<name>/.
    """
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        home = Path(pwd.getpwnam(sudo_user).pw_dir)
        return home / ".local" / "share" / "kento" / name
    return (base or LXC_BASE) / name


def sanitize_image_name(image: str) -> str:
    """Convert an OCI image reference to a filesystem-safe name.

    Substitution order: '-' → '--',  '/' → '-',  '_' → '__',  ':' → '_'

    The transformation is injective for typical OCI image references but not
    bijective in the general case — adjacent '_:' and ':_' sequences produce
    collisions (e.g. 'a_:b' and 'a:_b' both map to 'a___b').
    """
    s = image.replace("-", "--")
    s = s.replace("/", "-")
    s = s.replace("_", "__")
    s = s.replace(":", "_")
    return s


def next_instance_name(base_name: str, scan_dir: Path,
                       other_dir: Path | None = None) -> str:
    """Return the next available auto-generated instance name.

    Appends -0, -1, -2, ... to base_name until an unused name is found.
    Checks both directory names and kento-name files in scan_dir.
    When other_dir is provided, also checks that directory for name conflicts
    so that auto-generated names are unique across both namespaces.
    """
    used_names: set[str] = set()
    for d_root in (scan_dir, other_dir):
        if d_root is not None and d_root.is_dir():
            for d in d_root.iterdir():
                if d.is_dir():
                    used_names.add(d.name)
                    name_file = d / "kento-name"
                    if name_file.is_file():
                        used_names.add(name_file.read_text().strip())
    n = 0
    while True:
        candidate = f"{base_name}-{n}"
        if candidate not in used_names:
            return candidate
        n += 1


def is_running(container_dir: Path, mode: str) -> bool:
    """Check if a container is running, using the mode-appropriate method."""
    import subprocess
    if mode == "vm":
        from kento.vm import is_vm_running
        return is_vm_running(container_dir)
    elif mode == "pve-vm":
        vmid_file = container_dir / "kento-vmid"
        if not vmid_file.is_file():
            return False
        vmid = vmid_file.read_text().strip()
        result = subprocess.run(
            ["qm", "status", vmid],
            capture_output=True, text=True,
        )
        return result.returncode == 0 and "running" in result.stdout
    elif mode == "pve":
        result = subprocess.run(
            ["pct", "status", container_dir.name],
            capture_output=True, text=True,
        )
        return result.returncode == 0 and "running" in result.stdout
    else:
        result = subprocess.run(
            ["lxc-info", "-n", container_dir.name, "-sH"],
            capture_output=True, text=True,
        )
        return result.returncode == 0 and "RUNNING" in result.stdout


def resolve_container(name: str, scan_dir: Path | None = None) -> Path:
    """Resolve a container name to its directory path.

    For LXC mode, the name IS the directory name (fast path).
    For PVE mode, scans kento-name files to find the matching directory.
    When scan_dir is None, searches both LXC_BASE and VM_BASE.
    Returns the container directory path, or exits with error if not found.
    """
    bases = [scan_dir] if scan_dir else [LXC_BASE, VM_BASE]

    for base in bases:
        # Fast path: directory name matches
        direct = base / name
        if direct.is_dir() and (direct / "kento-image").is_file():
            return direct

        # Scan kento-name files
        if base.is_dir():
            for d in sorted(base.iterdir()):
                if not d.is_dir():
                    continue
                name_file = d / "kento-name"
                if name_file.is_file() and name_file.read_text().strip() == name:
                    if (d / "kento-image").is_file():
                        return d

    print(f"Error: container not found: {name}", file=sys.stderr)
    sys.exit(1)


def _scan_namespace(name: str, base: Path) -> Path | None:
    """Scan a single base directory for a container/VM by name.

    Returns the directory path if found, None otherwise.
    """
    # Fast path: directory name matches
    direct = base / name
    if direct.is_dir() and (direct / "kento-image").is_file():
        return direct

    # Scan kento-name files
    if base.is_dir():
        for d in sorted(base.iterdir()):
            if not d.is_dir():
                continue
            name_file = d / "kento-name"
            if name_file.is_file() and name_file.read_text().strip() == name:
                if (d / "kento-image").is_file():
                    return d
    return None


def resolve_in_namespace(name: str, namespace: str) -> Path:
    """Resolve a name within a specific namespace ('container' or 'vm').

    Searches only LXC_BASE (for 'container') or VM_BASE (for 'vm').
    Exits with error if not found.
    """
    base = LXC_BASE if namespace == "container" else VM_BASE
    result = _scan_namespace(name, base)
    if result is not None:
        return result
    print(f"Error: No {namespace} named '{name}'", file=sys.stderr)
    sys.exit(1)


def resolve_any(name: str) -> tuple[Path, str]:
    """Resolve a name across both namespaces.

    Returns (container_dir, mode) where mode is read from the kento-mode file.
    Exits with error if ambiguous (found in both) or not found.
    """
    lxc_hit = _scan_namespace(name, LXC_BASE)
    vm_hit = _scan_namespace(name, VM_BASE)

    if lxc_hit and vm_hit:
        print(
            f"Ambiguous: '{name}' exists as both LXC container and VM. "
            "Use 'kento container <cmd>' or 'kento vm <cmd>'.",
            file=sys.stderr,
        )
        sys.exit(1)

    if lxc_hit:
        return lxc_hit, read_mode(lxc_hit)

    if vm_hit:
        return vm_hit, read_mode(vm_hit, "vm")

    print(f"Error: No container or VM named '{name}'", file=sys.stderr)
    sys.exit(1)


def check_name_conflict(name: str, target_namespace: str) -> bool:
    """Check if a name already exists in the OTHER namespace.

    Returns True if a conflict exists, False otherwise.
    Does not error — the caller decides what to do.
    """
    if target_namespace == "container":
        other_base = VM_BASE
    else:
        other_base = LXC_BASE
    return _scan_namespace(name, other_base) is not None
