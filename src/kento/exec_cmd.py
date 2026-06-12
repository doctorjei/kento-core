"""Run a command inside a kento-managed instance (non-interactive exec).

Dispatch per mode:
- lxc      -> lxc-attach -n <name> -- cmd...   (inherited stdio)
- pve      -> pct exec <vmid> -- cmd...        (pve-lxc; vmid is the dir name)
- vm       -> error (no in-guest agent; use SSH or 'kento attach')
- pve-vm   -> error (same)

The module is named exec_cmd to avoid any confusion with the ``exec`` builtin.
"""

import logging
import subprocess
from pathlib import Path

from kento import read_mode, require_root, resolve_any
from kento.errors import ModeError, ValidationError

logger = logging.getLogger("kento")


def exec_cmd(name: str, command: list[str],
             namespace: str | None = None) -> int:
    """Run ``command`` inside instance ``name``. Returns an exit code."""
    require_root()

    if not command:
        raise ValidationError(
            "exec requires a command, e.g. "
            "'kento exec <name> -- ls -la'"
        )

    container_dir, mode = resolve_any(name, namespace)
    if mode is None:
        mode = read_mode(container_dir)

    if mode in ("vm", "pve-vm"):
        raise ModeError(
            "'kento exec' is not supported for VM instances "
            "(no in-guest agent). Use SSH, or 'kento attach <name>' for an "
            "interactive console."
        )

    if mode == "pve":
        # pve-lxc: the instance directory name IS the VMID.
        vmid = container_dir.name
        return subprocess.run(["pct", "exec", vmid, "--", *command]).returncode

    # plain lxc: name is the container name; inherit stdio.
    return subprocess.run(["lxc-attach", "-n", name, "--", *command]).returncode
