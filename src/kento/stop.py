"""Shut down a kento-managed instance."""

import logging
from pathlib import Path

from kento import is_running, read_mode, require_root, resolve_container
from kento.errors import SubprocessError, ValidationError
from kento.subprocess_util import run_or_die

logger = logging.getLogger("kento")

DEFAULT_PVE_VM_SHUTDOWN_TIMEOUT = 30

# pct/qm phrasings emitted when the target is already down. is_running()
# ASSUMES RUNNING on a status-query timeout/non-zero (see its docstring), so a
# stop may be issued on an instance that is actually stopped; we must treat
# that as a benign no-op rather than a hard error.
_NOT_RUNNING_MARKERS = (
    "not running",        # "CT is not running", "VM is not running"
    "no such",            # defensive: tool variants
    "is stopped",
)


def _is_not_running_error(text: str) -> bool:
    """Heuristically detect a pct/qm "already stopped" failure from its output."""
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _NOT_RUNNING_MARKERS)


def _pve_shutdown_or_die(cmd, *, what: str, name: str, hint: str,
                         container_dir: Path, mode: str):
    """Run a pct/qm stop command, tolerating the already-stopped case.

    On non-zero exit we distinguish two cases:
      - the instance is actually already down (the status query that drove us
        here merely timed out / returned non-zero, so is_running() assumed
        running) -> log "Already stopped" (info) and return None; OR
      - a genuine failure -> raise SubprocessError (branded message), as before.

    The non-fatal run is issued via subprocess_util.subprocess so it shares the
    same dispatch (and test patch point) as run_or_die. On a genuine failure
    the branded message mirrors run_or_die's format verbatim.

    Returns the CompletedProcess on success, or None if the instance was
    already stopped (caller should return without printing a stop action).
    """
    from kento import subprocess_util
    cmd = list(cmd)
    try:
        result = subprocess_util.subprocess.run(
            cmd, capture_output=True, text=True)
    except (FileNotFoundError, OSError):
        # Tool missing / not executable: defer to run_or_die for the branded
        # FileNotFoundError/OSError message + exit (it re-runs and hits the
        # same error deterministically).
        run_or_die(cmd, what=what, name=name, hint=hint)
        return None  # unreachable (run_or_die exits)

    if result.returncode == 0:
        return result

    combined = (result.stdout or "") + (result.stderr or "")
    # Re-query actual status; if the instance really is down (or its config is
    # gone), this was the already-stopped race, not a real failure.
    if _is_not_running_error(combined) or not is_running(container_dir, mode):
        logger.info("Already stopped: %s", name)
        return None

    # Genuine failure: raise SubprocessError (mirrors run_or_die's contract).
    label = f"{what} {name}" if name else what
    stderr = (result.stderr or "").strip()
    if len(stderr) > 500:
        stderr = stderr[:500] + "... (truncated)"
    msg = f"failed to {label} (exit {result.returncode})"
    if stderr:
        msg += f": {stderr}"
    if hint:
        logger.info("hint: %s", hint)
    raise SubprocessError(msg, cmd=list(cmd), returncode=result.returncode)


def shutdown(
    name: str,
    *,
    force: bool = False,
    container_dir: Path | None = None,
    mode: str | None = None,
    timeout: int | None = None,
    graceful_only: bool = False,
) -> None:
    require_root()

    if container_dir is None:
        container_dir = resolve_container(name)
    container_id = container_dir.name

    if mode is None:
        mode = read_mode(container_dir)

    # Mutually exclusive guards. timeout/graceful-only only meaningful
    # on the bounded-graceful pve-vm path; --force skips graceful entirely.
    if graceful_only and force:
        raise ValidationError(
            "--graceful-only and --force are mutually exclusive "
            "(one waits forever, the other kills now)."
        )
    if graceful_only and timeout is not None:
        raise ValidationError(
            "--timeout has no effect with --graceful-only "
            "(graceful-only drops --forceStop, so the timeout is meaningless)."
        )
    if force and timeout is not None:
        raise ValidationError(
            "--timeout has no effect with --force "
            "(force skips graceful shutdown entirely)."
        )

    # F15: idempotent stop — calling stop on an already-stopped instance
    # should be a no-op, not a traceback from lxc-stop/pct/qm exiting
    # non-zero because the target is already down.
    if not is_running(container_dir, mode):
        logger.info("Already stopped: %s", name)
        return

    if mode == "vm":
        from kento.vm import stop_vm
        # graceful_only (M6 never-hard-kill): SIGTERM but don't SIGKILL a
        # stubborn VM — leave it for the caller's re-probe. Pass no_kill ONLY
        # when set, so the default path's stop_vm() call is byte-identical to
        # before (existing callers/tests unchanged).
        if graceful_only:
            stop_vm(container_dir, force=force, no_kill=True)
        else:
            stop_vm(container_dir, force=force)
    elif mode == "pve-vm":
        vmid = (container_dir / "kento-vmid").read_text().strip()
        if force:
            result = _pve_shutdown_or_die(
                ["qm", "stop", vmid],
                what="stop PVE VM",
                name=name,
                hint=f"run 'qm stop {vmid}' directly for details.",
                container_dir=container_dir, mode=mode,
            )
            if result is None:
                return  # already stopped (message already printed)
        elif graceful_only:
            result = _pve_shutdown_or_die(
                ["qm", "shutdown", vmid],
                what="shut down PVE VM",
                name=name,
                hint=f"run 'qm shutdown {vmid}' directly for details.",
                container_dir=container_dir, mode=mode,
            )
            if result is None:
                return  # already stopped
        else:
            effective_timeout = (
                timeout if timeout is not None else DEFAULT_PVE_VM_SHUTDOWN_TIMEOUT
            )
            result = _pve_shutdown_or_die(
                [
                    "qm", "shutdown", vmid,
                    "--timeout", str(effective_timeout),
                    "--forceStop",
                ],
                what="shut down PVE VM",
                name=name,
                hint=f"run 'qm shutdown {vmid}' directly for details.",
                container_dir=container_dir, mode=mode,
            )
            if result is None:
                return  # already stopped
            combined = (result.stdout or "") + (result.stderr or "")
            lowered = combined.lower()
            if "still running" in lowered or "terminating" in lowered:
                logger.warning(
                    "VM %s did not honor ACPI shutdown within %ss, hard-stopped",
                    name, effective_timeout,
                )
    elif mode == "pve":
        if force:
            result = _pve_shutdown_or_die(
                ["pct", "stop", container_id],
                what="stop PVE container",
                name=name,
                hint=f"run 'pct stop {container_id}' directly for details.",
                container_dir=container_dir, mode=mode,
            )
        else:
            result = _pve_shutdown_or_die(
                ["pct", "shutdown", container_id],
                what="shut down PVE container",
                name=name,
                hint=f"run 'pct shutdown {container_id}' directly for details.",
                container_dir=container_dir, mode=mode,
            )
        if result is None:
            return  # already stopped (message already printed)
    else:
        cmd = ["lxc-stop", "-n", container_id]
        if force:
            cmd.append("-k")
        elif graceful_only:
            # M6 never-hard-kill: --nokill makes lxc-stop report failure rather
            # than SIGKILL the container after its grace window, leaving it up
            # for the caller's re-probe. Opt-in (typed graceful path only);
            # plain graceful keeps lxc-stop's default kill-after-grace.
            cmd.append("--nokill")
        run_or_die(
            cmd,
            what="stop LXC container",
            name=name,
            hint=f"run 'lxc-stop -n {container_id}' directly for details.",
        )

    action = "Stopped" if force else "Shut down"
    logger.info("%s: %s", action, name)


# Alias for backward compatibility
stop = shutdown
