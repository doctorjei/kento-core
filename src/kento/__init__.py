"""Kento — compose OCI images into LXC system containers via overlayfs."""

import os
import pwd
import sys
from pathlib import Path

__version__ = "0.1.0"

LXC_BASE = Path("/var/lib/lxc")


def require_root() -> None:
    if os.getuid() != 0:
        print("Error: must run as root", file=sys.stderr)
        sys.exit(1)


def upper_base(name: str) -> Path:
    """Return the base directory for a container's upper and work dirs.

    When run via sudo, uses the invoking user's XDG data directory
    (~user/.local/share/kento/<name>/) so writable state is per-user.
    When run as root directly, uses /var/lib/lxc/<name>/.
    """
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        home = Path(pwd.getpwnam(sudo_user).pw_dir)
        return home / ".local" / "share" / "kento" / name
    return LXC_BASE / name
