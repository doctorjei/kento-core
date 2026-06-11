"""Show journal logs from a kento-managed instance.

Dispatch per mode:
- lxc      -> lxc-attach -n <name> -- journalctl <args>   (inherited stdio)
- pve      -> pct exec <vmid> -- journalctl <args>        (pve-lxc)
- vm       -> error (use 'kento attach' for the serial console, or SSH)
- pve-vm   -> error (same)

Extra args (e.g. -f, -n, 50) are forwarded verbatim to journalctl.
"""

import subprocess
import sys
from pathlib import Path

from kento import read_mode, require_root, resolve_any


def logs(name: str, args: list[str],
         namespace: str | None = None) -> int:
    """Show ``journalctl`` output for instance ``name``. Returns an exit code."""
    require_root()

    container_dir, mode = resolve_any(name, namespace)
    if mode is None:
        mode = read_mode(container_dir)

    if mode in ("vm", "pve-vm"):
        print(
            "Error: 'kento logs' is not supported for VM instances. "
            "Use 'kento attach <name>' for the serial console, or SSH + "
            "journalctl inside the guest.",
            file=sys.stderr,
        )
        return 1

    if mode == "pve":
        # pve-lxc: the instance directory name IS the VMID.
        vmid = container_dir.name
        return subprocess.run(
            ["pct", "exec", vmid, "--", "journalctl", *args]
        ).returncode

    # plain lxc: name is the container name; inherit stdio.
    return subprocess.run(
        ["lxc-attach", "-n", name, "--", "journalctl", *args]
    ).returncode
