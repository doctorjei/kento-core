"""Suspend / resume a kento-managed VM instance (vCPU pause, not a stop).

`kento suspend` pauses a running VM's vCPUs; `kento resume` un-pauses them.
This is a *pause to RAM* — the VM process keeps running and its memory is
retained — NOT a shutdown. It is therefore VM-modes-only:

- plain vm : QMP ``stop`` / ``cont`` over ``<container_dir>/qmp.sock`` (the
             QMP unix socket exposed by start_vm). See qmp_command below.
- pve-vm   : ``qm suspend <vmid>`` / ``qm resume <vmid>``.
- lxc / pve-lxc : unsupported — there is no vCPU to pause; use
             ``kento stop`` / ``kento start`` instead.

Registered (like attach/exec/logs/set) at the bare + lxc + vm CLI scopes; an
LXC-scoped invocation just hits the unsupported-mode error.
"""

import json
import logging
import socket
import subprocess
from pathlib import Path

from kento import is_running, read_mode, require_root, resolve_any
from kento.errors import ModeError, StateError, SubprocessError

logger = logging.getLogger("kento")

_LXC_UNSUPPORTED = (
    "suspend/resume is not supported for LXC instances; "
    "use 'kento stop' / 'kento start'."
)


def qmp_command(sock_path, *commands, timeout: float = 10):
    """Connect to a QEMU QMP unix socket, negotiate, run commands, return responses.

    Opens ``sock_path`` (AF_UNIX, SOCK_STREAM), reads the server's greeting
    line (``{"QMP": {...}}``), leaves capabilities-negotiation mode by issuing
    ``qmp_capabilities``, then sends each command dict in ``commands`` and
    collects its reply.

    QMP framing: every message is a newline-terminated JSON object. The server
    may interleave asynchronous ``{"event": ...}`` messages with command
    replies, so when waiting for a command's reply we read lines until we get a
    dict that is NOT an event (i.e. carries ``return`` or ``error``).

    Returns the list of reply dicts (one per command in ``commands``), each
    either ``{"return": ...}`` or ``{"error": ...}``. Raises OSError on socket
    failure and ValueError on a malformed/empty stream.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(sock_path))
        reader = _LineReader(sock)

        # 1. Greeting: the server speaks first with {"QMP": {...}}.
        greeting = reader.next_json()
        if "QMP" not in greeting:
            raise ValueError(f"unexpected QMP greeting: {greeting!r}")

        # 2. Leave negotiation mode. The reply is {"return": {}}.
        _send(sock, {"execute": "qmp_capabilities"})
        reader.next_reply()

        # 3. Run each requested command, collecting its reply.
        replies = []
        for cmd in commands:
            _send(sock, cmd)
            replies.append(reader.next_reply())
        return replies
    finally:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()


def _send(sock, obj) -> None:
    """Serialize ``obj`` as a newline-terminated UTF-8 JSON line and send it."""
    sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))


class _LineReader:
    """Buffered newline-delimited JSON reader over a blocking socket."""

    def __init__(self, sock) -> None:
        self._sock = sock
        self._buf = b""

    def _next_line(self) -> bytes:
        while b"\n" not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                if self._buf.strip():
                    line, self._buf = self._buf, b""
                    return line
                raise ValueError("QMP socket closed before a complete message")
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return line

    def next_json(self) -> dict:
        """Return the next non-empty JSON object from the stream."""
        while True:
            line = self._next_line().strip()
            if not line:
                continue
            return json.loads(line.decode("utf-8"))

    def next_reply(self) -> dict:
        """Return the next reply, skipping interleaved async events."""
        while True:
            msg = self.next_json()
            if "event" in msg:
                continue  # async event; keep waiting for the command reply
            return msg


def _vm_qmp(name: str, container_dir: Path, command: str, label: str) -> None:
    """Run a single QMP command on a plain VM. command is 'stop' or 'cont'."""
    sock_path = container_dir / "qmp.sock"
    if not sock_path.exists():
        raise SubprocessError(
            f"QMP socket not found for '{name}' ({sock_path}). The "
            f"instance is not running, or it was started by an older kento "
            f"without QMP support. Start it with 'kento start {name}' and retry."
        )
    try:
        (reply,) = qmp_command(sock_path, {"execute": command})
    except (OSError, ValueError) as exc:
        raise SubprocessError(
            f"QMP {command} failed for '{name}': {exc}. Is it running?"
        ) from exc
    if "error" in reply:
        err = reply["error"]
        desc = err.get("desc", err) if isinstance(err, dict) else err
        raise SubprocessError(f"QMP {command} rejected for '{name}': {desc}")
    logger.info("%s: %s", label, name)


def _pve_vm_qm(name: str, container_dir: Path, verb: str, label: str) -> None:
    """Run 'qm suspend|resume <vmid>' for a pve-vm. verb is 'suspend'/'resume'."""
    vmid = (container_dir / "kento-vmid").read_text().strip()
    result = subprocess.run(
        ["qm", verb, vmid], capture_output=True, text=True)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise SubprocessError(
            f"'qm {verb} {vmid}' failed for '{name}'"
            + (f": {stderr}" if stderr else "")
            + f". Run 'qm {verb} {vmid}' directly for details.",
            cmd=["qm", verb, vmid],
            returncode=result.returncode,
        )
    logger.info("%s: %s", label, name)


def _dispatch(name: str, *, vm_qmp_command: str, qm_verb: str,
              label: str, namespace: str | None = None) -> None:
    """Shared body for suspend/resume."""
    require_root()

    container_dir, mode = resolve_any(name, namespace)
    if mode is None:
        mode = read_mode(container_dir)

    if mode in ("lxc", "pve"):
        raise ModeError(_LXC_UNSUPPORTED)

    if not is_running(container_dir, mode):
        raise StateError(
            f"instance is not running: {name}. "
            f"Start it first: kento start {name}"
        )

    if mode == "vm":
        return _vm_qmp(name, container_dir, vm_qmp_command, label)
    if mode == "pve-vm":
        return _pve_vm_qm(name, container_dir, qm_verb, label)

    # Unknown mode (shouldn't happen — resolve_any returns a known mode).
    raise ModeError(f"unsupported mode {mode!r} for suspend/resume.")


def suspend(name: str, namespace: str | None = None) -> None:
    """Pause a running VM's vCPUs (QMP stop / qm suspend)."""
    _dispatch(name, vm_qmp_command="stop", qm_verb="suspend",
              label="Suspended", namespace=namespace)


def resume(name: str, namespace: str | None = None) -> None:
    """Resume a suspended VM's vCPUs (QMP cont / qm resume)."""
    _dispatch(name, vm_qmp_command="cont", qm_verb="resume",
              label="Resumed", namespace=namespace)
