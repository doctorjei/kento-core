"""Shut down a kento-managed instance."""

import sys
from pathlib import Path

from kento import is_running, read_mode, require_root, resolve_container
from kento.subprocess_util import run_or_die

DEFAULT_PVE_VM_SHUTDOWN_TIMEOUT = 30


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
        print(
            "Error: --graceful-only and --force are mutually exclusive "
            "(one waits forever, the other kills now).",
            file=sys.stderr,
        )
        sys.exit(1)
    if graceful_only and timeout is not None:
        print(
            "Error: --timeout has no effect with --graceful-only "
            "(graceful-only drops --forceStop, so the timeout is meaningless).",
            file=sys.stderr,
        )
        sys.exit(1)
    if force and timeout is not None:
        print(
            "Error: --timeout has no effect with --force "
            "(force skips graceful shutdown entirely).",
            file=sys.stderr,
        )
        sys.exit(1)

    # F15: idempotent stop — calling stop on an already-stopped instance
    # should be a no-op, not a traceback from lxc-stop/pct/qm exiting
    # non-zero because the target is already down.
    if not is_running(container_dir, mode):
        print(f"Already stopped: {name}")
        return

    if mode == "vm":
        from kento.vm import stop_vm
        stop_vm(container_dir, force=force)
    elif mode == "pve-vm":
        vmid = (container_dir / "kento-vmid").read_text().strip()
        if force:
            run_or_die(
                ["qm", "stop", vmid],
                what="stop PVE VM",
                name=name,
                hint=f"run 'qm stop {vmid}' directly for details.",
            )
        elif graceful_only:
            run_or_die(
                ["qm", "shutdown", vmid],
                what="shut down PVE VM",
                name=name,
                hint=f"run 'qm shutdown {vmid}' directly for details.",
            )
        else:
            effective_timeout = (
                timeout if timeout is not None else DEFAULT_PVE_VM_SHUTDOWN_TIMEOUT
            )
            result = run_or_die(
                [
                    "qm", "shutdown", vmid,
                    "--timeout", str(effective_timeout),
                    "--forceStop",
                ],
                what="shut down PVE VM",
                name=name,
                hint=f"run 'qm shutdown {vmid}' directly for details.",
            )
            combined = (result.stdout or "") + (result.stderr or "")
            lowered = combined.lower()
            if "still running" in lowered or "terminating" in lowered:
                print(
                    f"kento: warning: VM {name} did not honor ACPI shutdown "
                    f"within {effective_timeout}s, hard-stopped",
                    file=sys.stderr,
                )
    elif mode == "pve":
        if force:
            run_or_die(
                ["pct", "stop", container_id],
                what="stop PVE container",
                name=name,
                hint=f"run 'pct stop {container_id}' directly for details.",
            )
        else:
            run_or_die(
                ["pct", "shutdown", container_id],
                what="shut down PVE container",
                name=name,
                hint=f"run 'pct shutdown {container_id}' directly for details.",
            )
    else:
        cmd = ["lxc-stop", "-n", container_id]
        if force:
            cmd.append("-k")
        run_or_die(
            cmd,
            what="stop LXC container",
            name=name,
            hint=f"run 'lxc-stop -n {container_id}' directly for details.",
        )

    action = "Stopped" if force else "Shut down"
    print(f"{action}: {name}")


# Alias for backward compatibility
stop = shutdown
