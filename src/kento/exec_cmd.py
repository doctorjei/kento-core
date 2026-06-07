"""Run a command inside a kento-managed instance (non-interactive exec).

Dispatch per mode:
- lxc      -> lxc-attach -n <name> -- cmd...   (inherited stdio)
- pve      -> pct exec <vmid> -- cmd...        (pve-lxc; vmid is the dir name)
- vm       -> error (no in-guest agent; use SSH or 'kento attach')
- pve-vm   -> error (same)

The module is named exec_cmd to avoid any confusion with the ``exec`` builtin.
"""

import subprocess
import sys
from pathlib import Path

from kento import read_mode, require_root, resolve_any


def exec_cmd(name: str, command: list[str]) -> int:
    """Run ``command`` inside instance ``name``. Returns an exit code."""
    require_root()

    if not command:
        print(
            "Error: exec requires a command, e.g. "
            "'kento exec <name> -- ls -la'",
            file=sys.stderr,
        )
        return 2

    container_dir, mode = resolve_any(name)
    if mode is None:
        mode = read_mode(container_dir)

    if mode in ("vm", "pve-vm"):
        print(
            "Error: 'kento exec' is not supported for VM instances "
            "(no in-guest agent). Use SSH, or 'kento attach <name>' for an "
            "interactive console.",
            file=sys.stderr,
        )
        return 1

    if mode == "pve":
        # pve-lxc: the instance directory name IS the VMID.
        vmid = container_dir.name
        return subprocess.run(["pct", "exec", vmid, "--", *command]).returncode

    # plain lxc: name is the container name; inherit stdio.
    return subprocess.run(["lxc-attach", "-n", name, "--", *command]).returncode
