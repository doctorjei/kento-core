"""Kento — compose OCI images into LXC system containers via overlayfs."""

import os
import pwd
import sys
from pathlib import Path

__version__ = "0.5.3"

LXC_BASE = Path("/var/lib/lxc")
VM_BASE = Path("/var/lib/kento/vm")


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

    The transformation is bijective (reversible):
      '-' → '--',  '/' → '-',  '_' → '__',  ':' → '_'
    """
    s = image.replace("-", "--")
    s = s.replace("/", "-")
    s = s.replace("_", "__")
    s = s.replace(":", "_")
    return s


def next_instance_name(base_name: str, scan_dir: Path) -> str:
    """Return the next available auto-generated instance name.

    Appends -0, -1, -2, ... to base_name until an unused name is found.
    Checks both directory names and kento-name files in scan_dir.
    """
    used_names: set[str] = set()
    if scan_dir.is_dir():
        for d in scan_dir.iterdir():
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
