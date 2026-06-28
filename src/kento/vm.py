"""Gemet VM mode — QEMU + virtiofs VM management."""

import hashlib
import logging
import os
import re
import signal
import socket
import subprocess
import time
from pathlib import Path

from kento import VM_BASE
from kento.defaults import VM_MEMORY, VM_CORES, VM_KVM, VM_MACHINE
from kento.errors import StateError
from kento.subprocess_util import run_or_die

logger = logging.getLogger("kento")

# Locally-administered MAC prefix (QEMU's standard block).
MAC_PREFIX = "52:54:00"

_MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")


def is_valid_mac(mac: str) -> bool:
    """Return True if mac is a valid colon-separated 6-pair hex MAC."""
    return bool(_MAC_RE.match(mac))


def generate_mac(identifier: str) -> str:
    """Return a stable deterministic MAC address for the given identifier.

    The identifier is the container name (plain VM) or VMID (PVE-VM).
    Uses 52:54:00 prefix + 3 bytes from sha256(identifier).
    """
    digest = hashlib.sha256(identifier.encode()).digest()[:3]
    suffix = ":".join(f"{b:02x}" for b in digest)
    return f"{MAC_PREFIX}:{suffix}"

# virtiofsd is often installed outside PATH (e.g. /usr/libexec/virtiofsd on Debian)
_VIRTIOFSD_SEARCH = ["/usr/libexec/virtiofsd", "/usr/lib/qemu/virtiofsd",
                     "/usr/lib/virtiofsd", "/usr/bin/virtiofsd"]


def _find_virtiofsd() -> str:
    """Locate the virtiofsd binary."""
    import shutil
    path = shutil.which("virtiofsd")
    if path:
        return path
    for candidate in _VIRTIOFSD_SEARCH:
        if Path(candidate).is_file():
            return candidate
    raise StateError("virtiofsd not found. Install virtiofsd or check PATH.")


def _find_qemu() -> str:
    """Locate the qemu-system-x86_64 binary.

    Mirrors _find_virtiofsd so we fail cleanly (before spawning virtiofsd or
    mounting anything) when QEMU is absent, rather than leaking a virtiofsd
    process and mount on a Popen FileNotFoundError.
    """
    import shutil
    path = shutil.which("qemu-system-x86_64")
    if path:
        return path
    raise StateError("qemu-system-x86_64 not found. Install QEMU or check PATH.")


_PORT_MIN = 10022
_PORT_MAX = 10999


def _port_is_free(port: int) -> bool:
    """Check if a TCP port is available on localhost."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
            return True
    except OSError:
        return False


def allocate_port(exclude: set[int] | None = None) -> int:
    """Return the next free host port in range 10022-10999.

    Scans kento-port files in both LXC and VM base directories to find used
    ports, then verifies the candidate port is actually free on the host.

    ``kento-port`` now holds N forward specs (one per line, §5.7A), so EVERY
    line's host port is collected as used — not just the first. ``exclude`` is
    an optional set of host ports to treat as already-taken; the caller uses it
    to allocate several ``auto`` forwards in one create without writing each to
    disk between allocations (the within-batch dedup the on-disk scan can't see).
    """
    from kento import LXC_BASE
    used_ports: set[int] = set(exclude or ())
    for base in (VM_BASE, LXC_BASE):
        if base.is_dir():
            for d in base.iterdir():
                if d.is_dir():
                    port_file = d / "kento-port"
                    if port_file.is_file():
                        for line in port_file.read_text().splitlines():
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                used_ports.add(int(line.split(":")[0]))
                            except (ValueError, IndexError):
                                continue
    for port in range(_PORT_MIN, _PORT_MAX + 1):
        if port not in used_ports and _port_is_free(port):
            return port
    raise StateError("no free port in range 10022-10999")


def _read_hostfwds(container_dir: Path) -> list[str]:
    """Read kento-port and return QEMU slirp ``hostfwd=`` fragments (§5.7A).

    Each non-empty line is a §5.7A spec parsed via the Block-02 boundary parser;
    one fragment ``<proto>:127.0.0.1:<hport>-:<gport>`` is emitted per forward,
    protocol-aware (tcp/udp). Address stays ``127.0.0.1`` (current slirp
    behavior; host_addr/guest_addr are ``None`` in 1.0). A 1:1 mirror of QEMU's
    own ``hostfwd=proto:hostaddr:hostport-guestaddr:guestport`` grammar.

    Absent file -> ``[]`` (no NIC emitted). A malformed line raises at the
    boundary rather than silently dropping a forward (start fails loudly — the
    file was written by a validated create/set path).
    """
    from kento._network import parse_forward_spec
    port_file = container_dir / "kento-port"
    if not port_file.is_file():
        return []
    fragments: list[str] = []
    for line in port_file.read_text().splitlines():
        spec = line.strip()
        if not spec:
            continue
        (protocol, _haddr, hport), (_gaddr, gport) = parse_forward_spec(spec)
        fragments.append(f"{protocol.value}:127.0.0.1:{hport}-:{gport}")
    return fragments


def is_vm_running(container_dir: Path) -> bool:
    """Check if a VM is running by verifying the QEMU PID file."""
    pid_file = container_dir / "kento-qemu-pid"
    if not pid_file.is_file():
        return False
    try:
        pid = int(pid_file.read_text().strip())
        return Path(f"/proc/{pid}").is_dir()
    except (ValueError, OSError):
        return False


def _is_mountpoint(path: Path) -> bool:
    """Check if a path is a mountpoint."""
    return subprocess.run(
        ["mountpoint", "-q", str(path)], capture_output=True,
    ).returncode == 0


def _umount_with_retry(path: Path, force: bool) -> bool:
    """Umount ``path`` with a busy-mount escape hatch.

    Two-stage strategy:

    1. Plain ``umount <path>``. If it succeeds, return True.
    2. On failure, try to free up the mount: invoke ``fuser -km <path>``
       to kill any holders (best-effort — fuser may not be installed and
       any failure here is swallowed), then retry with ``umount -l``
       (lazy unmount, which detaches the mount on the last reference).

    If the lazy umount also fails:

    * ``force=True``: log a warning and return True so the caller (e.g.
      ``destroy -f``) can still proceed with rmtree. The lazy umount,
      even on failure path, is best-effort cleanup; the kernel detaches
      whenever the last reference drops.
    * ``force=False``: return False and let the caller raise.
    """
    # First attempt — plain umount.
    result = subprocess.run(["umount", str(path)], capture_output=True, text=True)
    if result.returncode == 0:
        return True

    stderr = (result.stderr or "").strip()
    logger.warning("umount %s failed (exit %d): %s", path, result.returncode, stderr)

    # Best-effort: kill processes holding the mount. fuser is in psmisc,
    # which is not always installed; tolerate either FileNotFoundError or
    # a non-zero return.
    try:
        subprocess.run(
            ["fuser", "-km", str(path)],
            capture_output=True,
        )
    except (FileNotFoundError, OSError):
        pass

    # Retry with lazy umount.
    lazy = subprocess.run(["umount", "-l", str(path)], capture_output=True, text=True)
    if lazy.returncode == 0:
        return True

    lazy_stderr = (lazy.stderr or "").strip()
    if force:
        logger.warning(
            "lazy umount %s also failed (exit %d): %s; proceeding anyway "
            "(rmtree will detach on last ref).",
            path, lazy.returncode, lazy_stderr,
        )
        return True
    return False


def mount_rootfs(container_dir: Path, layers: str, state_dir: Path) -> None:
    """Mount overlayfs at container_dir/rootfs on the host.

    Builds the lowerdir from short chdir-relative ``l/<short>`` symlinks
    (Docker/podman parity) so a deeply layered image's mount(2) options stay
    under the kernel's 4096-byte page limit. The mount subprocess is given
    ``cwd=<overlay_root>`` so the relative lowerdir resolves; the subprocess
    cwd is scoped (no chdir leaks into this process). upper/work/rootfs stay
    absolute. If the short form can't be derived, to_overlay_lowerdir returns
    the absolute layers + an empty root → cwd=None (current absolute behavior).
    """
    from kento.layers import to_overlay_lowerdir
    rootfs = container_dir / "rootfs"
    if _is_mountpoint(rootfs):
        raise StateError(f"rootfs already mounted at {rootfs}")
    upper = state_dir / "upper"
    work = state_dir / "work"
    overlay_root, rel_layers = to_overlay_lowerdir(layers)
    opts = f"lowerdir={rel_layers},upperdir={upper},workdir={work}"
    env = {**os.environ, "LIBMOUNT_FORCE_MOUNT2": "always"}
    run_or_die(
        ["mount", "-t", "overlay", "overlay", "-o", opts, str(rootfs)],
        what="mount overlayfs",
        hint=f"common causes: upperdir on overlayfs (set KENTO_STATE_DIR), missing overlay module, stale mount at {rootfs}.",
        env=env,
        cwd=overlay_root or None,
    )


def unmount_rootfs(container_dir: Path) -> None:
    """Unmount overlayfs at container_dir/rootfs."""
    rootfs = container_dir / "rootfs"
    run_or_die(
        ["umount", str(rootfs)],
        what="unmount rootfs",
        hint="check for open file handles or active processes with 'lsof +D ...'.",
    )


def start_vm(container_dir: Path, name: str) -> None:
    """Start a VM: mount rootfs, launch virtiofsd + QEMU, write PID files."""
    # F15: idempotent — callers in start.py already guard, but this
    # protects direct callers (create --start flow) too. Match CLI
    # wording so users see the same message regardless of entry point.
    if is_vm_running(container_dir):
        logger.info("Already running: %s", name)
        return

    layers = (container_dir / "kento-layers").read_text().strip()
    state_dir = Path((container_dir / "kento-state").read_text().strip())

    # Mount overlayfs
    mount_rootfs(container_dir, layers, state_dir)

    rootfs = container_dir / "rootfs"

    # Validate kernel and initramfs exist
    kernel = rootfs / "boot" / "vmlinuz"
    initramfs = rootfs / "boot" / "initramfs.img"
    if not kernel.is_file():
        unmount_rootfs(container_dir)
        raise StateError(f"kernel not found at {kernel}")
    if not initramfs.is_file():
        unmount_rootfs(container_dir)
        raise StateError(f"initramfs not found at {initramfs}")

    # Inject guest-side config (hostname/network/tz/env/ssh-key) into the
    # mounted rootfs before virtiofsd starts. inject.sh reads kento metadata
    # files and writes them into the overlayfs upper layer. A failing inject
    # is a start failure — don't boot a misconfigured VM.
    inject_script = container_dir / "kento-inject.sh"
    if not inject_script.is_file():
        unmount_rootfs(container_dir)
        raise StateError(f"inject script not found at {inject_script}")
    run_or_die(
        ["sh", str(inject_script), str(rootfs), str(container_dir)],
        what="inject guest config",
        name=name,
        hint=f"run 'sh {inject_script} {rootfs} {container_dir}' manually to debug.",
    )

    # Resolve binaries up-front so an absent QEMU fails cleanly (before we
    # spawn virtiofsd or leak the mount). _find_virtiofsd / _find_qemu both
    # raise StateError on miss; at this point only the mount is held, and the
    # caller-agnostic rollback below has not yet been armed (nothing to undo
    # beyond the mount, which the missing-kernel/initramfs guards above also
    # leave to their own unmount — keep that behaviour for the binary checks).
    virtiofsd_bin = _find_virtiofsd()
    _find_qemu()
    socket_path = container_dir / "virtiofsd.sock"

    # From here on virtiofsd is running and the rootfs is mounted, so any
    # failure must roll both back. start_vm is called directly by start.py
    # (`kento start` / `kento vm start`) with NO surrounding undo, so it has
    # to be self-cleaning regardless of caller. (create(--start) registers its
    # own vm-stop undo; stop_vm is idempotent, so a double cleanup is safe.)
    virtiofsd = None
    try:
        # Start virtiofsd
        virtiofsd = subprocess.Popen(
            [virtiofsd_bin,
             f"--socket-path={socket_path}",
             f"--shared-dir={rootfs}",
             "--cache=auto"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        (container_dir / "kento-virtiofsd-pid").write_text(str(virtiofsd.pid) + "\n")

        # Wait for socket to appear
        for _ in range(50):
            if socket_path.exists():
                break
            time.sleep(0.1)

        # Abort if the socket never appeared or virtiofsd died: launching QEMU
        # against a dead vhost-user chardev produces a broken VM and leaks the
        # mount + the virtiofsd process. Mirror the abort-and-unmount logic in
        # vm_hook.py's pre-start phase. The single except below does the cleanup.
        if not socket_path.exists() or virtiofsd.poll() is not None:
            raise StateError(f"virtiofsd socket did not appear at {socket_path}")

        qemu = _launch_qemu(container_dir, name, rootfs, socket_path)
    except BaseException:
        # Any failure in this block (the socket-abort StateError, qemu Popen
        # FileNotFoundError, OSError reading kento-memory/kento-cores, ...) must
        # roll back virtiofsd + mount + pid files exactly once, then re-raise so
        # the caller sees the original error. Cleanup is idempotent.
        _cleanup_failed_start(container_dir, virtiofsd)
        raise

    (container_dir / "kento-qemu-pid").write_text(str(qemu.pid) + "\n")

    logger.info("Started: %s", name)
    port_file = container_dir / "kento-port"
    if port_file.is_file():
        host_port = port_file.read_text().strip().split(":")[0]
        logger.info("  SSH: ssh -p %s root@localhost", host_port)


def _cleanup_failed_start(container_dir: Path, virtiofsd) -> None:
    """Roll back a partially-started VM: kill virtiofsd, unmount, drop pid files.

    Idempotent and tolerant of a half-built state (virtiofsd may be None, the
    mount may be absent). Used by start_vm's failure paths so it is self-cleaning
    for callers without their own rollback (start.py).
    """
    if virtiofsd is not None:
        try:
            virtiofsd.terminate()
            virtiofsd.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                virtiofsd.kill()
                virtiofsd.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                pass
        except (OSError, ValueError):
            pass
    (container_dir / "kento-virtiofsd-pid").unlink(missing_ok=True)
    (container_dir / "kento-qemu-pid").unlink(missing_ok=True)
    rootfs = container_dir / "rootfs"
    if _is_mountpoint(rootfs):
        _umount_with_retry(rootfs, force=True)
    (container_dir / "virtiofsd.sock").unlink(missing_ok=True)


def _launch_qemu(container_dir: Path, name: str, rootfs: Path,
                 socket_path: Path):
    """Build the QEMU argv and launch it detached. Returns the Popen object."""
    kernel = rootfs / "boot" / "vmlinuz"
    initramfs = rootfs / "boot" / "initramfs.img"

    # Read port mappings (usermode networking) — N forwards (§5.7A), one slirp
    # hostfwd per spec, protocol-aware (tcp/udp). _read_hostfwds returns the
    # list of "<proto>:127.0.0.1:<hport>-:<gport>" fragments for the -netdev.
    hostfwds = _read_hostfwds(container_dir)

    memory_file = container_dir / "kento-memory"
    if memory_file.is_file():
        memory = memory_file.read_text().strip()
    else:
        memory = str(VM_MEMORY)
    cores_file = container_dir / "kento-cores"
    if cores_file.is_file():
        cores = cores_file.read_text().strip()
    else:
        cores = str(VM_CORES)
    # Nesting (v1.3.0): kento-nesting holds "1" (expose vmx/svm so the guest
    # can run accelerated nested VMs) or "0"/absent (mask them). Same read
    # pattern as kento-memory/kento-cores above.
    nesting_file = container_dir / "kento-nesting"
    nesting_on = nesting_file.is_file() and nesting_file.read_text().strip() == "1"
    # Serial + QMP unix sockets (v1.4.0 VM-interactive). Mirror the
    # virtiofsd.sock naming: Path under container_dir, str()'d into the argv.
    # serial.sock carries the guest console (console=ttyS0 in -append below);
    # a later `attach` command relays a tty to it. qmp.sock exposes QEMU's
    # monitor protocol (item-1 suspend/resume prep). Both use server=on so
    # QEMU is the listener, wait=off so it does NOT block at boot waiting for
    # a client (QEMU is started detached).
    serial_socket_path = container_dir / "serial.sock"
    qmp_socket_path = container_dir / "qmp.sock"
    qemu_cmd = ["qemu-system-x86_64",
         "-kernel", str(kernel),
         "-initrd", str(initramfs),
         "-m", memory,
         "-smp", cores,
         "-machine", VM_MACHINE,
    ]
    if VM_KVM:
        # CPU model is always `host`. Nesting OFF deterministically strips the
        # hardware virt extensions (vmx/svm) even on a nesting-enabled host, so
        # the guest cannot start its own accelerated VMs unless --allow-nesting
        # was set. No KVM → no -cpu (TCG default); nesting needs KVM anyway.
        # Emitted before the kento-qemu-args pass-through below so a user
        # --qemu-arg '-cpu ...' can still override (QEMU honours the last -cpu).
        cpu = "host" if nesting_on else "host,vmx=off,svm=off"
        qemu_cmd += ["-enable-kvm", "-cpu", cpu]
    qemu_cmd += [
         # -display none (not -nographic): suppress any graphical display
         # without aliasing the guest serial to QEMU's stdio (QEMU is
         # detached). Serial is attached explicitly to serial.sock below.
         "-display", "none",
         "-serial", f"unix:{serial_socket_path},server=on,wait=off",
         "-qmp", f"unix:{qmp_socket_path},server=on,wait=off",
         "-chardev", f"socket,id=vfs,path={socket_path}",
         "-device", "vhost-user-fs-pci,chardev=vfs,tag=rootfs",
         "-object", f"memory-backend-memfd,id=mem,size={memory}M,share=on",
         "-numa", "node,memdev=mem",
    ]
    if hostfwds:
        # Include MAC if available (kento-mac written at create time for VM modes)
        mac_file = container_dir / "kento-mac"
        device = "virtio-net-pci,netdev=net0"
        if mac_file.is_file():
            mac = mac_file.read_text().strip()
            if mac:
                device = f"virtio-net-pci,netdev=net0,mac={mac}"
        # One netdev with N comma-joined hostfwd= options (QEMU accepts repeated
        # hostfwd= on a single -netdev user).
        netdev = "user,id=net0," + ",".join(
            f"hostfwd={hf}" for hf in hostfwds)
        qemu_cmd += [
             "-netdev", netdev,
             "-device", device,
        ]
    qemu_cmd += [
         "-append", "console=ttyS0 rootfstype=virtiofs root=rootfs",
    ]

    # Pass-through flags (v1.2.0 Phase B). kento-qemu-args is written at
    # create time by --qemu-arg; each non-empty line becomes one argv
    # element. Appended AFTER kento's own argv so QEMU's last-occurrence
    # semantics lets users override kento defaults (e.g. --qemu-arg '-m 2048'
    # overrides the -m <memory> emitted above). One line = one argv element —
    # no shell splitting. For a flag with a separate value, users pass two
    # --qemu-arg flags (or use the -flag=value form). Tolerate missing file.
    # Skip blank AND whitespace-only lines: a lone whitespace token would
    # otherwise become an empty/positional argv element that QEMU rejects.
    # Matches the stricter PVE path (pve.py rejects whitespace-only args).
    passthrough_file = container_dir / "kento-qemu-args"
    if passthrough_file.is_file():
        for raw in passthrough_file.read_text().splitlines():
            line = raw.strip()
            if line:
                qemu_cmd.append(line)

    # Return the Popen; start_vm owns the kento-qemu-pid write and the
    # "Started:"/SSH output so the pid file is written exactly once and the
    # cleanup-on-failure path in start_vm stays the sole owner of that state.
    return subprocess.Popen(
        qemu_cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _kill_and_wait(pid_file: Path, timeout: float = 5.0, *, force: bool = False,
                   no_kill: bool = False) -> None:
    """Send SIGTERM (or SIGKILL if force) to a process and wait for it to exit.

    ``no_kill`` (graceful-only, M6): SIGTERM the process and wait, but do NOT
    escalate to SIGKILL if it is still alive at the deadline — the still-running
    process is left for the caller's typed re-probe to detect (so the typed
    ``stop(force=False)`` can raise ``StopTimeout`` instead of the runtime
    hard-killing). Defaults to ``False`` = today's behavior (escalate to
    SIGKILL); only the typed graceful path opts in. Mutually distinct from
    ``force`` (which sends SIGKILL up front); ``no_kill`` only suppresses the
    deadline-fallback SIGKILL on the SIGTERM path.
    """
    if not pid_file.is_file():
        return
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        pid_file.unlink(missing_ok=True)
        return
    try:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        pid_file.unlink(missing_ok=True)
        return
    # Wait for process to exit
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not Path(f"/proc/{pid}").is_dir():
            break
        time.sleep(0.1)
    else:
        # Still alive at the deadline. Graceful-only (no_kill) LEAVES it running
        # for the typed re-probe (M6 "never hard-kills"); otherwise escalate to
        # SIGKILL (today's default) and drop the pid file.
        if no_kill:
            return
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    pid_file.unlink(missing_ok=True)


def stop_vm(container_dir: Path, *, force: bool = False,
            no_kill: bool = False) -> None:
    """Stop a VM: kill QEMU + virtiofsd, unmount rootfs, clean up.

    ``no_kill`` (M6 graceful-only): SIGTERM QEMU/virtiofsd but do NOT SIGKILL a
    stubborn process — leave it running for the typed re-probe (defaults to
    ``False`` = today's kill-on-timeout behavior; opt-in from the typed graceful
    stop only). Forwarded to ``_kill_and_wait`` only when set, so the default
    path's call shape is byte-identical to before (existing tests untouched).
    """
    extra = {"no_kill": True} if no_kill else {}
    _kill_and_wait(container_dir / "kento-qemu-pid", force=force, **extra)
    _kill_and_wait(container_dir / "kento-virtiofsd-pid", force=force, **extra)

    # Unmount rootfs. Use the busy-mount-hardened helper so a wedged QEMU
    # or virtiofsd that left a stale handle doesn't permanently block stop.
    # stop_vm only flips to lazy/force semantics when called with force=True
    # (the destroy -f path); a plain stop preserves the strict failure mode.
    rootfs = container_dir / "rootfs"
    if _is_mountpoint(rootfs):
        if not _umount_with_retry(rootfs, force=force):
            raise StateError(f"failed to unmount {rootfs}. Is the instance still running?")

    # Clean up sockets (virtiofsd + serial/qmp from VM-interactive wiring).
    (container_dir / "virtiofsd.sock").unlink(missing_ok=True)
    (container_dir / "serial.sock").unlink(missing_ok=True)
    (container_dir / "qmp.sock").unlink(missing_ok=True)
