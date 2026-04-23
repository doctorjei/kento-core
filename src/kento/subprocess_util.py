"""Subprocess wrapper that converts CalledProcessError/FileNotFoundError into
kento-branded error messages, not Python tracebacks."""

import subprocess
import sys
from typing import Sequence


def run_or_die(
    cmd: Sequence[str],
    what: str,
    *,
    name: str | None = None,
    hint: str | None = None,
    env: dict | None = None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess:
    """Run cmd and exit with a clean error on failure.

    Args:
        cmd: Command + args (first element is the executable).
        what: Human-readable operation description, e.g. "start LXC container".
              Used in the error message: "Error: failed to {what}..."
        name: Optional instance name for the message.
        hint: Optional follow-on line suggesting what to do next.
        env, cwd: Passed to subprocess.run.

    Returns the CompletedProcess on success. On failure prints
    Error: failed to {what}[ {name}] (exit {rc}): {stderr_snippet}
    [hint: {hint}]
    and exits with status 1 (CalledProcessError) or 2 (FileNotFoundError).
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
        print(f"Error: '{tool}' not found on PATH. Install it or check your PATH.",
              file=sys.stderr)
        if hint:
            print(f"hint: {hint}", file=sys.stderr)
        sys.exit(2)

    if result.returncode != 0:
        label = f"{what} {name}" if name else what
        stderr = (result.stderr or "").strip()
        # Keep the stderr snippet short so it's still scannable. Full output
        # from the external tool already went to the user's terminal if the
        # caller didn't capture it.
        if len(stderr) > 500:
            stderr = stderr[:500] + "... (truncated)"
        msg = f"Error: failed to {label} (exit {result.returncode})"
        if stderr:
            msg += f": {stderr}"
        print(msg, file=sys.stderr)
        if hint:
            print(f"hint: {hint}", file=sys.stderr)
        sys.exit(1)

    return result
