"""Cross-process lock for kento state mutations.

Used by create() to serialize allocate-port / next-vmid / next-instance-name
/ container_dir.mkdir across concurrent kento invocations. Without this, two
concurrent `kento create` runs can allocate the same VMID, port, or
auto-generated name.
"""

import errno
import fcntl
import os
import sys
from contextlib import contextmanager
from pathlib import Path

# Primary lock path (tmpfs-backed, cleared on reboot — fine for a lock).
# Fallback when /run is not writable (unusual, but handles root-owned
# read-only /run under some container setups).
_PRIMARY_LOCK = Path("/run/kento.lock")
_FALLBACK_LOCK = Path("/var/lib/kento/.lock")


def _open_lock_fd() -> int:
    """Open (creating if needed) the kento lock file and return its fd.

    Tries /run/kento.lock first. Falls back to /var/lib/kento/.lock if
    /run is not writable. The file is kept open for the duration of the
    lock; flock is released on close.
    """
    for path in (_PRIMARY_LOCK, _FALLBACK_LOCK):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            return os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            continue
    print(
        "Error: could not open a kento lock file at /run/kento.lock or "
        "/var/lib/kento/.lock. Check filesystem permissions.",
        file=sys.stderr,
    )
    sys.exit(1)


@contextmanager
def kento_lock():
    """Acquire the kento-wide exclusive lock for the duration of a block.

    Blocks until the lock is available. Use this around any sequence that
    must be atomic across concurrent `kento` processes — notably the
    allocate-then-write steps in create() (ports, VMIDs, auto-names, and
    container_dir creation).
    """
    fd = _open_lock_fd()
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
