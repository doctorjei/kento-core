"""Shut down a kento-managed instance."""

from pathlib import Path

from kento import is_running, read_mode, require_root, resolve_container
from kento.subprocess_util import run_or_die


def shutdown(name: str, *, force: bool = False, container_dir: Path | None = None, mode: str | None = None) -> None:
    require_root()

    if container_dir is None:
        container_dir = resolve_container(name)
    container_id = container_dir.name

    if mode is None:
        mode = read_mode(container_dir)

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
        else:
            # qm shutdown's default timeout is short and relies on ACPI.
            # Guests without acpid (or that ignore the power button) hang
            # until PVE aborts with "got timeout". --timeout extends the
            # graceful window; --forceStop 1 falls through to qm stop
            # once the timeout elapses so the VM reliably stops.
            run_or_die(
                ["qm", "shutdown", vmid, "--timeout", "60", "--forceStop", "1"],
                what="shut down PVE VM",
                name=name,
                hint=f"run 'qm shutdown {vmid}' directly for details.",
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
