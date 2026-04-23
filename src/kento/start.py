"""Start a kento-managed instance."""

from pathlib import Path

from kento import is_running, read_mode, require_root, resolve_container
from kento.subprocess_util import run_or_die


def start(name: str, *, container_dir: Path | None = None, mode: str | None = None) -> None:
    require_root()

    if container_dir is None:
        container_dir = resolve_container(name)
    container_id = container_dir.name

    if mode is None:
        mode = read_mode(container_dir)

    # F15: idempotent start — calling start on a running instance should
    # be a no-op that reports the current state, not a traceback from
    # lxc-start/pct/qm complaining the container is already up.
    if is_running(container_dir, mode):
        print(f"Already running: {name}")
        return

    if mode == "vm":
        from kento.vm import start_vm
        start_vm(container_dir, name)
        return
    elif mode == "pve-vm":
        vmid = (container_dir / "kento-vmid").read_text().strip()
        run_or_die(
            ["qm", "start", vmid],
            what="start PVE VM",
            name=name,
            hint=f"check /var/log/pve/tasks/ or run 'qm start {vmid}' directly.",
        )
    elif mode == "pve":
        run_or_die(
            ["pct", "start", container_id],
            what="start PVE container",
            name=name,
            hint=f"run 'pct start {container_id}' directly for details.",
        )
    else:
        run_or_die(
            ["lxc-start", "-n", container_id],
            what="start LXC container",
            name=name,
            hint=f"run 'lxc-start -F -n {container_id}' in the foreground for details.",
        )

    print(f"Started: {name}")
