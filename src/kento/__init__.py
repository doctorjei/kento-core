"""Kento — compose OCI images into LXC system containers via overlayfs."""

import os
import sys
from pathlib import Path

__version__ = "0.1.0"

LXC_BASE = Path("/var/lib/lxc")


def require_root() -> None:
    if os.getuid() != 0:
        print("Error: must run as root", file=sys.stderr)
        sys.exit(1)
