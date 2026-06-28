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


def _wrap_command(command: list[str],
                  user: str | None,
                  env: dict[str, str] | None) -> list[str]:
    """Build the in-guest command, layering ``env`` then ``user`` (innermost-out).

    The wrapping is composed so it works identically on both backends (the guest
    program is the same argv after ``lxc-attach -- `` / ``pct exec -- ``):

    * ``env`` — prepend an in-guest ``env K=V … `` so the variables are set in
      the guest's process environment (NOT the host's — we never pass them to
      the host ``subprocess.run``, which would set them on lxc-attach/pct).
    * ``user`` — wrap in ``runuser -u <user> -- `` so the command runs as that
      user inside the guest. ``runuser`` is the non-PAM, root-only switch
      (present on any systemd guest); it sits OUTSIDE ``env`` so ``runuser``
      itself is found on root's PATH and the ``env`` assignments apply to the
      target command.

    (``tty`` is NOT a factor here — it is a stdio property of the host
    ``lxc-attach``/``pct exec`` invocation, not the in-guest argv.)

    The DEFAULT path (``env`` None, ``user`` None) returns ``command``
    unchanged — byte-identical to the pre-touch behavior.
    """
    inner = list(command)
    if env:
        # ``env K=V … cmd …`` — set the vars in the guest before exec'ing cmd.
        env_prefix = ["env"]
        for key, value in env.items():
            env_prefix.append(f"{key}={value}")
        inner = env_prefix + inner
    if user is not None:
        # ``runuser -u <user> -- cmd …`` — drop to <user> inside the guest.
        inner = ["runuser", "-u", user, "--", *inner]
    return inner


def exec_cmd(name: str, command: list[str],
             namespace: str | None = None,
             *,
             tty: bool = False,
             user: str | None = None,
             env: dict[str, str] | None = None) -> int:
    """Run ``command`` inside instance ``name``. Returns an exit code.

    ``tty`` / ``user`` / ``env`` are threaded through as far as
    ``lxc-attach``/``pct exec`` allow (the typed ``SystemContainer.exec`` — M13 —
    is the public caller):

    * ``env`` — set in the guest via an in-guest ``env K=V … `` prefix.
    * ``user`` — run as that guest user via ``runuser -u <user> -- ``.
    * ``tty`` — best-effort, and DELIBERATELY does not alter the argv.
      ``lxc-attach``/``pct exec`` allocate a pty when this process's stdio is
      itself a terminal (the inherited-stdio default), and there is no flag on
      either tool (within this minimal touch) to force one when the caller has
      no terminal — neither tool will mint a pty out of a pipe. So ``tty=True``
      from an interactive context already gets a pty (via inheritance), and
      ``tty=True`` from a non-terminal context CANNOT get one. We honor the
      parameter to the limit the tools allow (inheritance) and do not pretend to
      force a pty we cannot. This keeps the default path byte-identical and is
      honest about the limit (brief JC1).

    The DEFAULT path (``tty=False``, ``user=None``, ``env=None``) is
    byte-identical to the original behavior: the same ``lxc-attach -n <name> --
    <command>`` / ``pct exec <vmid> -- <command>`` with inherited stdio.
    """
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

    # ``tty`` is accepted to honor the M13 contract but does not shape the argv
    # (see the docstring): the inherited-stdio pty is the only lever the tools
    # give us within this minimal touch.
    del tty
    guest_cmd = _wrap_command(command, user, env)

    if mode == "pve":
        # pve-lxc: the instance directory name IS the VMID.
        vmid = container_dir.name
        return subprocess.run(["pct", "exec", vmid, "--", *guest_cmd]).returncode

    # plain lxc: name is the container name; inherit stdio.
    return subprocess.run(["lxc-attach", "-n", name, "--", *guest_cmd]).returncode
