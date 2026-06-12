"""Subprocess wrapper that converts CalledProcessError/FileNotFoundError into
kento-branded error messages, not Python tracebacks."""

import logging
import subprocess
from typing import Sequence

from kento.errors import SubprocessError

logger = logging.getLogger("kento")


def run_or_die(
    cmd: Sequence[str],
    what: str,
    *,
    name: str | None = None,
    hint: str | None = None,
    env: dict | None = None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess:
    """Run cmd and raise SubprocessError on failure.

    Args:
        cmd: Command + args (first element is the executable).
        what: Human-readable operation description, e.g. "start LXC container".
              Used in the error message: "failed to {what}..."
        name: Optional instance name for the message.
        hint: Optional follow-on line suggesting what to do next.
        env, cwd: Passed to subprocess.run.

    Returns the CompletedProcess on success. On failure raises SubprocessError
    with a message of the form:
        failed to {what}[ {name}] (exit {rc})[: {stderr_snippet}]
    carrying cmd and returncode. FileNotFoundError / PermissionError / OSError
    raise SubprocessError with returncode=None.
    """
    try:
        result = subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            env=env,
            cwd=cwd,
        )
    except FileNotFoundError:
        tool = cmd[0] if cmd else "(empty cmd)"
        msg = f"'{tool}' not found on PATH. Install it or check your PATH."
        if hint:
            logger.info("hint: %s", hint)
        raise SubprocessError(msg, cmd=list(cmd))
    except OSError as e:
        # PermissionError (binary lacks +x, or is a directory) and other OSError
        # (ENOEXEC / wrong-arch) — surface a branded message, not a traceback.
        tool = cmd[0] if cmd else "(empty cmd)"
        msg = f"cannot execute '{tool}': {e.strerror} (check permissions/arch)"
        if hint:
            logger.info("hint: %s", hint)
        raise SubprocessError(msg, cmd=list(cmd))

    if result.returncode != 0:
        label = f"{what} {name}" if name else what
        stderr = (result.stderr or "").strip()
        # Keep the stderr snippet short so it's still scannable. Full output
        # from the external tool already went to the user's terminal if the
        # caller didn't capture it.
        if len(stderr) > 500:
            stderr = stderr[:500] + "... (truncated)"
        msg = f"failed to {label} (exit {result.returncode})"
        if stderr:
            msg += f": {stderr}"
        if hint:
            logger.info("hint: %s", hint)
        raise SubprocessError(msg, cmd=list(cmd), returncode=result.returncode)

    return result
