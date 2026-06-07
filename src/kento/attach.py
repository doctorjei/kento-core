"""Attach to a kento-managed instance's console (interactive).

Dispatch per mode:
- lxc      -> lxc-attach -n <name>   (inherited stdio)
- pve      -> pct enter <vmid>       (pve-lxc; vmid is the instance dir name)
- pve-vm   -> qm terminal <vmid>
- vm       -> pure-Python serial relay to <container_dir>/serial.sock

The VM path connects an AF_UNIX socket to the serial console exposed by
start_vm (D1) and relays stdin<->socket with the local tty in raw mode.
Detach with Ctrl-] then Q. The escape handling lives in EscapeDetector, a
pure state machine that is unit-testable without a tty or socket.
"""

import os
import select
import socket
import subprocess
import sys
from pathlib import Path

from kento import read_mode, require_root, resolve_any

# Ctrl-] (GS, group separator) — the classic telnet/qm escape lead-in.
ESCAPE_BYTE = 0x1d


class EscapeDetector:
    """Pure state machine translating raw stdin bytes into relay actions.

    Feed one byte at a time. Each ``feed`` returns one of:
      - ("forward", bytes)   send these bytes to the socket
      - ("detach", None)     user requested detach (Ctrl-] then Q/q)
      - ("swallow", None)    byte consumed, nothing to send yet

    Sequence semantics:
      - A lone ESCAPE_BYTE (0x1d) is swallowed and arms the detector.
      - While armed, the next byte decides:
          * 'Q'/'q'      -> detach
          * ESCAPE_BYTE  -> forward a single literal 0x1d (so a doubled
                            Ctrl-] sends one Ctrl-] through; the detector
                            disarms, NOT re-arms)
          * anything else -> forward the held 0x1d followed by that byte
                             (the escape was not completed, so the lead-in
                             is delivered verbatim)
      - The detector disarms after resolving an armed byte.
    """

    def __init__(self) -> None:
        self._armed = False

    @property
    def armed(self) -> bool:
        return self._armed

    def feed(self, byte: int) -> tuple[str, bytes | None]:
        if not self._armed:
            if byte == ESCAPE_BYTE:
                self._armed = True
                return ("swallow", None)
            return ("forward", bytes([byte]))

        # Armed: resolve the second byte of a potential escape sequence.
        self._armed = False
        if byte in (ord("Q"), ord("q")):
            return ("detach", None)
        if byte == ESCAPE_BYTE:
            # Doubled Ctrl-]: send one literal through, stay disarmed.
            return ("forward", bytes([ESCAPE_BYTE]))
        # Not an escape: deliver the swallowed lead-in then this byte.
        return ("forward", bytes([ESCAPE_BYTE, byte]))


def _relay_serial(name: str, container_dir: Path) -> int:
    """Interactive serial console relay for VM mode. Returns an exit code."""
    sock_path = container_dir / "serial.sock"
    if not sock_path.exists():
        print(
            f"Error: serial socket not found for '{name}' "
            f"({sock_path}). The instance is not running, or it was started "
            f"by an older kento without serial support. Start it with "
            f"'kento start {name}' and retry.",
            file=sys.stderr,
        )
        return 1

    try:
        stdin_fd = sys.stdin.fileno()
        is_tty = os.isatty(stdin_fd)
    except (OSError, ValueError, AttributeError):
        # Redirected/replaced stdin with no real fd: not interactive.
        is_tty = False
    if not is_tty:
        print(
            "Error: 'kento attach' on a VM needs an interactive terminal "
            "(stdin is not a tty). Run it from a real terminal, or use SSH "
            "for non-interactive access.",
            file=sys.stderr,
        )
        return 1

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(sock_path))
    except OSError as exc:
        print(
            f"Error: could not connect to serial socket {sock_path}: {exc}. "
            f"Is '{name}' running?",
            file=sys.stderr,
        )
        return 1

    print(
        f"Connected to {name}. Escape: Ctrl-] then Q",
        file=sys.stderr,
    )

    import termios
    import tty

    old_attrs = termios.tcgetattr(stdin_fd)
    detector = EscapeDetector()
    sock_fd = sock.fileno()
    try:
        tty.setraw(stdin_fd)
        while True:
            rlist, _, _ = select.select([stdin_fd, sock_fd], [], [])
            if sock_fd in rlist:
                data = sock.recv(65536)
                if not data:
                    break  # socket EOF: VM/console closed
                os.write(sys.stdout.fileno(), data)
            if stdin_fd in rlist:
                data = os.read(stdin_fd, 65536)
                if not data:
                    break  # stdin EOF
                detached = False
                out = bytearray()
                for b in data:
                    action, payload = detector.feed(b)
                    if action == "detach":
                        detached = True
                        break
                    if action == "forward" and payload:
                        out.extend(payload)
                    # "swallow": nothing to forward
                if out:
                    sock.sendall(bytes(out))
                if detached:
                    break
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)
        sock.close()
        # Leave the cursor on a fresh line after raw-mode teardown.
        print(f"\r\nDetached from {name}.", file=sys.stderr)
    return 0


def attach(name: str) -> int:
    """Attach to instance ``name``'s console. Returns an exit code."""
    require_root()

    container_dir, mode = resolve_any(name)
    if mode is None:
        mode = read_mode(container_dir)

    if mode == "vm":
        return _relay_serial(name, container_dir)

    if mode == "pve-vm":
        vmid = (container_dir / "kento-vmid").read_text().strip()
        return subprocess.run(["qm", "terminal", vmid]).returncode

    if mode == "pve":
        # pve-lxc: the instance directory name IS the VMID.
        vmid = container_dir.name
        return subprocess.run(["pct", "enter", vmid]).returncode

    # plain lxc: name is the container name; inherit stdio for interactivity.
    return subprocess.run(["lxc-attach", "-n", name]).returncode
