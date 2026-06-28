"""Create an instance backed by an OCI image."""

import logging
import os
import shutil
import subprocess
from pathlib import Path

from kento import (LXC_BASE, VM_BASE, _scan_namespace, next_instance_name,
                   require_root, sanitize_image_name, upper_base, validate_name)
from kento.cloudinit import detect_cloudinit, write_seed
from kento.defaults import (APPARMOR_SYSTEMD_RULES, LXC_TTY, LXC_MOUNT_AUTO,
                            LXC_MOUNT_AUTO_NESTING, LXC_ARG_DENYLIST,
                            PVE_ARG_DENYLIST, QEMU_ARG_DENYLIST)
from kento.errors import (InstanceExistsError, ModeError, StateError,
                          SubprocessError, ValidationError)
from kento.hook import write_hook
from kento.inject import write_inject
from kento.layers import resolve_image_id, resolve_layers
from kento.locking import kento_lock

logger = logging.getLogger("kento")


def _apparmor_active() -> bool:
    """True if the kernel has AppArmor enabled as an active LSM.

    Reads the canonical sysfs flag. Kept tiny so tests can monkeypatch it
    rather than depending on the test host's real LSM state. A missing
    module/file (OSError) means AppArmor is not in play → `generated` is a
    harmless no-op, so we report False.
    """
    try:
        return Path("/sys/module/apparmor/parameters/enabled").read_text().strip() == "Y"
    except OSError:
        return False


def _apparmor_parser_present() -> bool:
    """True if `apparmor_parser` is on PATH (needed to load `generated`)."""
    return shutil.which("apparmor_parser") is not None


def _host_cpu_count() -> int | None:
    """Logical CPU count of this node, or None if undeterminable.

    Wraps ``os.cpu_count()``. kento runs locally on the node for both vm and
    pve-vm modes, so this is the correct vCPU ceiling. A None return (rare)
    means we can't tell — callers must SKIP the cores check and never clamp
    blindly. Kept as a module-level function so tests can monkeypatch it.
    """
    return os.cpu_count()


def _host_memory_mb() -> int | None:
    """Total RAM of this node in MB, or None if undeterminable.

    Parses ``/proc/meminfo`` (``MemTotal:   N kB``) and converts kB→MB. Any
    error (missing file, unexpected format) returns None so the caller SKIPs
    the memory warning. Kept module-level so tests can monkeypatch it.
    """
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                kb = int(line.split()[1])
                return kb // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _clamp_vm_capacity(cores: int, memory: int, *, mode: str) -> int:
    """Clamp VM vCPUs to node capacity (loud) and warn on memory overcommit.

    VM modes only (``vm``, ``pve-vm``): for any other mode this is a no-op and
    returns ``cores`` unchanged with no warning.

    Cores are firm — QEMU/PVE refuses more vCPUs than the host has, so an
    over-request would create a guest that fails to start. When the host CPU
    count is known and ``cores`` exceeds it, clamp down to the host count
    (floor 1) and warn loudly. If the host count is undeterminable, skip the
    check (never clamp blindly).

    Memory is soft — KVM permits overcommit/ballooning, so an over-request is
    legitimate. We only warn (never clamp) when the requested memory exceeds
    the node's total RAM. Returns the (possibly clamped) cores.
    """
    if mode not in ("vm", "pve-vm"):
        return cores

    host_cpus = _host_cpu_count()
    if host_cpus is not None and cores > host_cpus:
        clamped = max(1, host_cpus)
        logger.warning(
            "requested %d cores exceeds this node's CPU count (%d); "
            "clamping to %d.\n"
            "  QEMU/PVE refuses more vCPUs than the host has, so the VM "
            "would fail to start\n"
            "  otherwise. Pass --cores %d (or fewer) to silence this "
            "warning.",
            cores, host_cpus, clamped, clamped,
        )
        cores = clamped

    host_mb = _host_memory_mb()
    if host_mb is not None and memory > host_mb:
        logger.warning(
            "requested %d MB memory exceeds this node's total RAM (%d MB); "
            "proceeding\n"
            "  anyway (KVM permits overcommit), but the VM may be OOM-killed "
            "or fail to start\n"
            "  under memory pressure.",
            memory, host_mb,
        )

    return cores


def _perlayer_idmap_supported() -> bool:
    """True iff this kernel supports per-layer idmap for overlayfs.

    Tests the mainline (5.19+) path: idmap a lower directory via util-linux
    2.40+ ``X-mount.idmap``, then mount an overlayfs on top of the idmapped
    lower with ``userxattr,index=off,metacopy=off``. Both mounts succeeding
    means the per-layer idmap path is available (overlay-over-idmapped-lowers,
    approach 3a). This is the path kento uses for ``--unprivileged``.

    Returns False on ANY error or unsupported configuration and NEVER raises.
    Returns False if not root (mount requires CAP_SYS_ADMIN). Returns True on
    kernels >= 5.19 with util-linux >= 2.40.

    This is a functional (not version-sniffing) probe — it flips to True
    automatically once both capabilities are present with no kento change.
    All mounts are unmounted and the temp directory is removed in ``finally``;
    cleanup errors are ignored.
    """
    import tempfile
    tmp = None
    lower_id_mounted = False
    overlay_mounted = False
    try:
        if os.geteuid() != 0:
            return False
        tmp = tempfile.mkdtemp(prefix="kento-idmap-probe-")
        base = Path(tmp)
        lower = base / "lower"
        lower_id = base / "lower_id"
        upper = base / "upper"
        work = base / "work"
        merged = base / "merged"
        for d in (lower, lower_id, upper, work, merged):
            d.mkdir()
        (lower / "probe").write_text("x")

        # Step 1: idmap the lower directory (per-layer bind with X-mount.idmap).
        # util-linux >= 2.40 supports this option. On-disk uid 0 → presented
        # as uid 100000 to the overlay (same mapping as lxc.idmap u 0 100000 65536).
        r = subprocess.run(
            ["mount", "--bind", "-o",
             "X-mount.idmap=u:0:100000:1 g:0:100000:1",
             str(lower), str(lower_id)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return False
        lower_id_mounted = True

        # Step 2: overlay over the idmapped lower (mainline kernel >= 5.19).
        # userxattr avoids trusted.overlay.* xattr issues with podman layers;
        # index=off and metacopy=off are required for this overlay configuration.
        r = subprocess.run(
            ["mount", "-t", "overlay", "overlay", "-o",
             f"lowerdir={lower_id},upperdir={upper},workdir={work},"
             "userxattr,index=off,metacopy=off",
             str(merged)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return False
        overlay_mounted = True
        return True
    except Exception:  # noqa: BLE001 — probe must never raise
        return False
    finally:
        try:
            if overlay_mounted:
                subprocess.run(["umount", str(merged)],
                               capture_output=True, text=True)
            if lower_id_mounted:
                subprocess.run(["umount", str(lower_id)],
                               capture_output=True, text=True)
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass


# PVE's unprivileged container default idmap range. A fresh unprivileged
# container maps container uid/gid 0 onto host 100000 for 65536 ids unless the
# admin configures a custom range. kento doesn't expose custom ranges yet, so
# this default is the normal case — but _pve_idmap_range() honours a custom
# `lxc.idmap = u 0 B C` line if one is present (e.g. via --pve-arg) so the state
# file always records the EFFECTIVE range.
_PVE_DEFAULT_IDMAP_BASE = 100000
_PVE_DEFAULT_IDMAP_COUNT = 65536


def _pve_idmap_range(pve_conf_text: str) -> tuple[int, int]:
    """Return (BASE, COUNT) for the unprivileged idmap, following PVE.

    If ``pve_conf_text`` carries a custom ``lxc.idmap = u 0 <B> <C>`` line, use
    its BASE/COUNT; otherwise fall back to PVE's unprivileged default
    (100000/65536). Matches the uid (``u``) mapping for container id 0 — the
    range kento's hook idmaps every lowerdir with.

    The state file written from this lets the hook resolve the range
    independently of WHEN PVE populates lxc.idmap into the runtime config.
    """
    import re
    pat = re.compile(r"^\s*lxc\.idmap\s*=\s*u\s+0\s+(\d+)\s+(\d+)\s*$")
    for line in pve_conf_text.splitlines():
        m = pat.match(line)
        if m:
            return int(m.group(1)), int(m.group(2))
    return _PVE_DEFAULT_IDMAP_BASE, _PVE_DEFAULT_IDMAP_COUNT


def _run_start_or_rollback(cmd: list[str], *, name: str, scope: str) -> None:
    """Run a start command inside create()'s try block.

    On failure, raises RuntimeError instead of letting CalledProcessError
    propagate. The surrounding try/except in create() catches it and runs
    the rollback undos (which include the matching stop). We *don't* use
    run_or_die here because run_or_die raises SubprocessError which would
    also trigger the rollback but with a duplicate error message from the
    `Error during create:` line — explicit RuntimeError keeps a single
    clear message.
    """
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise RuntimeError(
            f"failed to start {name}: '{e.filename}' not found on PATH. "
            f"Instance created; run 'kento {scope} start {name}' to retry or "
            f"'kento {scope} destroy {name}' to remove."
        ) from e
    if result.returncode != 0:
        err = (result.stderr or "").strip() or f"(exit {result.returncode})"
        raise RuntimeError(
            f"failed to start {name}: {err}. "
            f"Instance created; run 'kento {scope} start {name}' to retry or "
            f"'kento {scope} destroy {name}' to remove."
        )


def _validate_qemu_args(qemu_args: list[str]) -> None:
    """Reject --qemu-arg values that clash with kento-managed QEMU flags.

    See QEMU_ARG_DENYLIST for the reserved substrings. Any match kills the
    create with an actionable error; the whole point of pass-through is to
    be an escape hatch, so the denylist is deliberately short.
    """
    for arg in qemu_args:
        for needle in QEMU_ARG_DENYLIST:
            if needle in arg:
                raise ValidationError(
                    f"kento manages {needle!r} directly — "
                    f"--qemu-arg {arg!r} would collide with kento's own "
                    "QEMU argv. Drop the flag or file an issue if you "
                    "need it overridable."
                )


def _validate_pve_args(pve_args: list[str]) -> None:
    """Reject --pve-arg values that duplicate kento-managed PVE config keys.

    See PVE_ARG_DENYLIST. Same escape-hatch reasoning as qemu-arg.
    """
    for arg in pve_args:
        for needle in PVE_ARG_DENYLIST:
            if needle in arg:
                raise ValidationError(
                    f"kento manages {needle!r} directly — "
                    f"--pve-arg {arg!r} would collide with kento's own "
                    "PVE config. Drop the flag or file an issue if you "
                    "need it overridable."
                )


def _validate_lxc_args(lxc_args: list[str]) -> None:
    """Reject --lxc-arg values that duplicate kento-managed plain-LXC keys.

    See LXC_ARG_DENYLIST. Same escape-hatch reasoning as qemu-arg/pve-arg:
    the denylist names only the structural keys generate_config() emits (plus
    the cgroup lines `kento set` manages); everything else is user-authored
    and passed through verbatim.
    """
    for arg in lxc_args:
        for needle in LXC_ARG_DENYLIST:
            if needle in arg:
                raise ValidationError(
                    f"kento manages {needle!r} directly — "
                    f"--lxc-arg {arg!r} would collide with kento's own "
                    "plain-LXC config. Drop the flag or file an issue if "
                    "you need it overridable."
                )


def _validate_env(env: list[str]) -> None:
    """Reject --env entries that aren't clean KEY=VALUE pairs.

    Each entry is written verbatim into three places: the cloud-init
    user-data ``content: |`` block scalar (cloudinit.py), /etc/environment,
    and ``lxc.environment = <e>``. An embedded newline (or other control
    char) would terminate the YAML block scalar early and silently drop
    later directives (ssh keys etc.), and corrupt the other targets too. The
    help text promises KEY=VALUE, so enforce it before any state is written:
    the key must be a valid shell-ish identifier, there must be an ``=``, and
    no control characters (including newline/tab/CR) may appear anywhere.
    """
    import re
    key_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    for e in env:
        if any(ord(c) < 0x20 or ord(c) == 0x7f for c in e):
            raise ValidationError(
                f"--env value contains a control character: {e!r}. "
                "Each --env must be a single-line KEY=VALUE pair."
            )
        if "=" not in e:
            raise ValidationError(
                f"--env value is not KEY=VALUE (missing '='): {e!r}."
            )
        key = e.split("=", 1)[0]
        if not key_re.match(key):
            raise ValidationError(
                f"--env key {key!r} is invalid in {e!r}; keys must "
                "match [A-Za-z_][A-Za-z0-9_]*."
            )


def _run_cleanup(undos: list[tuple[str, object]]) -> None:
    """Run cleanup callables in reverse order. Best-effort — log and continue on errors.

    Each entry is ``(label, callable)``. The callable takes no args and its
    return value is ignored. Exceptions are caught so one cleanup failure
    doesn't mask the others (or the original failure).
    """
    while undos:
        label, undo = undos.pop()
        try:
            undo()
        except Exception as cleanup_err:  # noqa: BLE001 — best-effort cleanup
            logger.warning("rollback step %r failed: %s", label, cleanup_err)


def generate_config(name: str, lxc_dir: Path, *, bridge: str | None = None,
                    net_type: str | None = None,
                    nesting: bool = False,
                    ip: str | None = None, gateway: str | None = None,
                    env: list[str] | None = None,
                    port: str | None = None,
                    memory: int | None = None,
                    cores: int | None = None,
                    unprivileged: bool = False,
                    mode: str = "lxc") -> str:
    hook = lxc_dir / "kento-hook"
    lines = [
        f"lxc.uts.name = {name}",
        f"lxc.rootfs.path = dir:{lxc_dir}/rootfs",
        "",
        "lxc.hook.version = 1",
        f"lxc.hook.pre-start = {hook}",
        f"lxc.hook.post-stop = {hook}",
    ]
    if port is not None:
        lines.append(f"lxc.hook.start-host = {hook}")
    # Network config based on net_type
    if net_type == "bridge" and bridge:
        lines += [
            "",
            "lxc.net.0.type = veth",
            f"lxc.net.0.link = {bridge}",
            "lxc.net.0.flags = up",
        ]
        if ip:
            lines.append(f"lxc.net.0.ipv4.address = {ip}")
            if gateway:
                lines.append(f"lxc.net.0.ipv4.gateway = {gateway}")
    elif net_type == "host":
        lines += [
            "",
            "lxc.net.0.type = none",  # shares host network
        ]
    elif bridge:  # backward compat: bridge passed without net_type
        lines += [
            "",
            "lxc.net.0.type = veth",
            f"lxc.net.0.link = {bridge}",
            "lxc.net.0.flags = up",
        ]
        if ip:
            lines.append(f"lxc.net.0.ipv4.address = {ip}")
            if gateway:
                lines.append(f"lxc.net.0.ipv4.gateway = {gateway}")
    # net_type == "none" or net_type is None with no bridge: no network lines
    mount_auto = LXC_MOUNT_AUTO_NESTING if nesting else LXC_MOUNT_AUTO
    lines += [
        "",
        f"lxc.mount.auto = {mount_auto}",
        f"lxc.tty.max = {LXC_TTY}",
    ]

    # Plain-LXC on modern OCI images (systemd 256+) needs AppArmor profile=generated.
    # The stock lxc-container-default-with-nesting profile blocks the credentials
    # tmpfs mount used by ImportCredential= directives, making systemd-journald,
    # systemd-networkd, systemd-tmpfiles-setup fail with status=243/CREDENTIALS.
    # profile=generated is a built-in LXC feature (not PVE-specific): LXC builds a
    # per-container profile that enforces the host/container boundary but labels
    # in-container processes :unconfined, so PAM helpers (unix_chkpwd) and other
    # setuid binaries still load glibc RELRO correctly. PVE-LXC takes the same
    # approach via pct's config.
    #
    # Escape hatch: KENTO_APPARMOR_PROFILE env var overrides the default. Set it
    # to "unconfined" when running kento inside an outer LXC (nested scenario) —
    # apparmor_parser calls needed to load `generated` are blocked in that case,
    # and `unconfined` is safe because the outer profile still enforces the
    # host/container boundary. Accepts only "generated" or "unconfined".
    #
    # common.conf must be included BEFORE nesting.conf so apparmor.profile ends up
    # set AFTER both includes (otherwise nesting.conf would override it).
    if mode == "lxc":
        lines.append("lxc.include = /usr/share/lxc/config/common.conf")
    if nesting:
        lines.append("lxc.include = /usr/share/lxc/config/nesting.conf")
        lines.append("lxc.mount.entry = /dev/fuse dev/fuse none bind,create=file,optional 0 0")
        lines.append("lxc.mount.entry = /dev/net/tun dev/net/tun none bind,create=file,optional 0 0")
    if mode == "lxc":
        profile = os.environ.get("KENTO_APPARMOR_PROFILE", "generated")
        if profile not in ("generated", "unconfined"):
            raise ValidationError(
                f"KENTO_APPARMOR_PROFILE must be 'generated' or "
                f"'unconfined', got {profile!r}"
            )
        # Fail-closed pre-flight: `generated` is loaded by apparmor_parser at
        # lxc-start time on a host whose kernel has AppArmor active as an LSM.
        # If the parser is absent the container HARD-FAILS at start ("Cannot
        # use generated profile: apparmor_parser not available") — it does not
        # degrade. Catch it here (config-gen time) with an actionable message
        # rather than writing a doomed config that fails confusingly later.
        # Only `generated` needs the parser; explicit `unconfined` is fine.
        if (profile == "generated" and _apparmor_active()
                and not _apparmor_parser_present()):
            raise StateError(
                "AppArmor is active in this kernel but 'apparmor_parser' is not\n"
                "installed, so LXC's default 'generated' profile cannot be loaded and the\n"
                "instance would fail to start. Fix one of:\n"
                "  - install the 'apparmor' package (provides apparmor_parser), or\n"
                "  - set KENTO_APPARMOR_PROFILE=unconfined (namespaces/cgroups still\n"
                "    enforce the host/container boundary; in-kernel MAC confinement off)."
            )
        lines.append(f"lxc.apparmor.profile = {profile}")
        lines.append("lxc.apparmor.allow_incomplete = 1")
        # Narrow AppArmor grant for modern systemd (replaces the old broad
        # allow_nesting=1). Only valid with a generated profile, and only needed
        # when nesting is off -- with --allow-nesting, nesting.conf already sets
        # allow_nesting=1. The unconfined escape hatch needs nothing (permits all).
        if profile == "generated" and not nesting:
            for rule in APPARMOR_SYSTEMD_RULES:
                lines.append(f"lxc.apparmor.raw = {rule}")
        # Unprivileged plain-LXC: map container UID/GID 0 onto an unprivileged
        # host range (100000:65536). The actual rootfs idmap is done per-layer
        # by kento's hook (kento-hook): for each lowerdir, the hook creates an
        # idmapped bind mount (X-mount.idmap=u:0:100000:65536 g:0:100000:65536)
        # and overlays over the idmapped lowers with userxattr,index=off,
        # metacopy=off. This uses the mainline kernel >= 5.19 per-layer idmap
        # path (overlay-over-idmapped-lowers), NOT the non-mainline merged-
        # overlay idmap (lxc.rootfs.options=idmap=container which requires an
        # unmerged kernel patch). create() gates this behind _perlayer_idmap_supported().
        if unprivileged:
            lines.append("lxc.idmap = u 0 100000 65536")
            lines.append("lxc.idmap = g 0 100000 65536")
    if env:
        for e in env:
            lines.append(f"lxc.environment = {e}")
    if memory is not None:
        lines.append(f"lxc.cgroup2.memory.max = {memory * 1048576}")
    if cores is not None:
        lines.append(f"lxc.cgroup2.cpu.max = {cores * 100000} 100000")

    # Pass-through lines (E1b): each non-empty line in kento-lxc-args is
    # appended verbatim AFTER kento's own lines. LXC's config parser is
    # last-value-wins, so appending lets the user override non-structural
    # defaults. The LXC_ARG_DENYLIST (checked in create.py / set_cmd.py)
    # already rejected the structural collisions.
    from kento.pve import _read_passthrough_lines
    lines.extend(_read_passthrough_lines(lxc_dir / "kento-lxc-args"))

    return "\n".join(lines) + "\n"


def _inject_network_config(state_dir: Path, ip: str,
                           gateway: str | None = None,
                           dns: str | None = None,
                           searchdomain: str | None = None,
                           mode: str = "lxc") -> None:
    """Write 05-kento-static.network into the overlayfs upper layer.

    The 05- prefix sorts before any image-baked drop-in (e.g. a generic
    Kind=veth Unmanaged=yes unit). In pve-lxc the guest eth0 presents
    Kind=veth, so such a unit would otherwise match and win; the 05- prefix
    makes kento's per-instance config authoritative.
    """
    # VM modes use predictable naming (e.g. enp0s2), so match by type.
    # LXC/PVE modes always have eth0 (configured by LXC veth).
    match_line = "Type=ether" if mode in ("vm", "pve-vm") else "Name=eth0"
    lines = [
        "[Match]",
        match_line,
        "",
        "[Network]",
        f"Address={ip}",
    ]
    if gateway:
        lines.append(f"Gateway={gateway}")
    if dns:
        lines.append(f"DNS={dns}")
    if searchdomain:
        lines.append(f"Domains={searchdomain}")
    lines.append("")

    net_dir = state_dir / "upper" / "etc" / "systemd" / "network"
    net_dir.mkdir(parents=True, exist_ok=True)
    (net_dir / "05-kento-static.network").write_text("\n".join(lines))


def _inject_hostname(state_dir: Path, hostname: str) -> None:
    """Write /etc/hostname into the overlayfs upper layer."""
    etc = state_dir / "upper" / "etc"
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "hostname").write_text(hostname + "\n")


def _inject_timezone(state_dir: Path, timezone: str) -> None:
    """Write timezone config into the overlayfs upper layer."""
    etc = state_dir / "upper" / "etc"
    etc.mkdir(parents=True, exist_ok=True)
    localtime = etc / "localtime"
    localtime.unlink(missing_ok=True)
    localtime.symlink_to(f"/usr/share/zoneinfo/{timezone}")
    (etc / "timezone").write_text(timezone + "\n")


def _inject_env(state_dir: Path, env_list: list[str]) -> None:
    """Write /etc/environment into the overlayfs upper layer."""
    etc = state_dir / "upper" / "etc"
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "environment").write_text("\n".join(env_list) + "\n")


def _generate_ssh_host_keys(dest_dir: Path) -> None:
    """Generate SSH host key pairs (rsa, ecdsa, ed25519) in dest_dir."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for key_type, extra_args in [("rsa", ["-b", "4096"]), ("ecdsa", []), ("ed25519", [])]:
        key_path = dest_dir / f"ssh_host_{key_type}_key"
        cmd = ["ssh-keygen", "-t", key_type] + extra_args + ["-f", str(key_path), "-N", ""]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except FileNotFoundError:
            raise SubprocessError(
                "ssh-keygen not found. Install openssh-client to use --ssh-host-keys.",
                cmd=cmd,
            )
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode("utf-8", "replace").strip()
            raise SubprocessError(
                f"ssh-keygen failed for {key_type} host key: {stderr}",
                cmd=cmd,
                returncode=e.returncode,
            )


def _copy_ssh_host_keys(src_dir: Path, dest_dir: Path) -> None:
    """Copy ssh_host_* files from src_dir into dest_dir."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for f in sorted(src_dir.iterdir()):
        if f.name.startswith("ssh_host_") and f.is_file():
            shutil.copy2(f, dest_dir / f.name)


def _normalize_port_specs(port: str | list[str] | None) -> list[str] | None:
    """Collapse the create() ``port`` arg to ``list[str] | None`` + validate.

    The un-re-pointed CLI passes a single ``str`` (``"8080:80"`` / ``"auto"``);
    the re-pointed CLI (Phase 6) will pass a ``list[str]``. Accept both:

    * ``None`` -> ``None`` (no forwarding requested).
    * ``str``  -> ``[str]``.
    * ``list`` -> the list, with empty/whitespace-only entries dropped; an
      all-empty list collapses to ``None`` (no forwards — there is no in-place
      clear at create time, unlike ``set``).

    Every concrete (non-``"auto"``) spec is validated and deduped EARLY via the
    Block-02 boundary parser (:func:`parse_forwards`) so a malformed/duplicate
    spec fails fast before any state mutation. ``"auto"`` entries are host-port
    placeholders the write site resolves under ``kento_lock``; they are not yet
    valid specs, so they are excluded from the pre-parse (each resolved spec is
    re-validated together at write time).
    """
    if port is None:
        return None
    if isinstance(port, str):
        specs = [port]
    else:
        specs = [p for p in port if p and p.strip()]
        if not specs:
            return None

    # Pre-validate + dedup the concrete specs now (fail fast). "auto" is a
    # placeholder, not a spec, so it cannot go through the grammar parser yet;
    # the write site validates the full resolved set together under the lock.
    from kento._network import parse_forwards
    concrete = [s for s in specs if s != "auto"]
    if concrete:
        parse_forwards(concrete)
    return specs


def _write_port_specs(container_dir: Path, port_specs: list[str]) -> list[str]:
    """Resolve any ``"auto"`` host ports and write N spec lines to kento-port.

    ``port_specs`` is the normalized list from :func:`_normalize_port_specs`
    (concrete specs already validated; ``"auto"`` placeholders not). Each
    ``"auto"`` entry is resolved to a free host port mapped to guest 22 (the
    historical default, tcp), and the full resolved set is validated/deduped
    together via :func:`parse_forwards` so an ``auto`` allocation cannot collide
    with a concrete forward (or with another freshly-allocated ``auto``).

    Allocation + write happen under a single ``kento_lock`` so two concurrent
    creates can't pick the same free port (the existing TOCTOU discipline,
    extended to N) and the on-disk file is only ever a complete, validated set.

    Returns the list of resolved spec strings (rendered §5.7A form) that were
    written — used by the caller for logging.
    """
    from kento._network import (parse_forward_spec, parse_forwards,
                                render_forward_spec)
    from kento.vm import allocate_port

    with kento_lock():
        resolved: list[str] = []
        allocated: set[int] = set()
        for spec in port_specs:
            if spec == "auto":
                # allocate_port(exclude=...) treats this batch's already-claimed
                # ports as taken, so two "auto" specs in one create never collide
                # even though neither is written to disk until the end. It also
                # excludes other instances' on-disk host ports + bind-tests.
                host_port = allocate_port(exclude=allocated)
                allocated.add(host_port)
                binding, target = parse_forward_spec(f"{host_port}:22")
            else:
                binding, target = parse_forward_spec(spec)
            resolved.append(render_forward_spec(binding, target))
        # Validate + dedup the fully-resolved set as one unit (catches an auto
        # port that lands on a concrete forward's host port).
        parse_forwards(resolved)
        (container_dir / "kento-port").write_text(
            "".join(line + "\n" for line in resolved))
    return resolved


def create(image: str, *, name: str | None = None, bridge: str | None = None,
           hostname: str | None = None,
           nesting: bool = False,
           start: bool = False, mode: str,
           pve: bool | None = None,
           vmid: int = 0, memory: int | None = None, cores: int | None = None,
           port: str | list[str] | None = None,
           ip: str | None = None, gateway: str | None = None,
           dns: str | None = None, searchdomain: str | None = None,
           timezone: str | None = None,
           env: list[str] | None = None,
           ssh_keys: list[str] | None = None,
           ssh_key_user: str = "root",
           ssh_host_keys: bool = False,
           ssh_host_key_dir: str | None = None,
           mac: str | None = None,
           config_mode: str = "auto",
           qemu_args: list[str] | None = None,
           pve_args: list[str] | None = None,
           lxc_args: list[str] | None = None,
           net_type: str | None = None,
           unprivileged: bool = False,
           force: bool = False) -> None:
    require_root()

    # Validate pass-through denylists before any state mutation. Failures
    # here are pure user-input errors; fail fast with a clear pointer at
    # the offending value.
    if qemu_args:
        _validate_qemu_args(qemu_args)
    if pve_args:
        _validate_pve_args(pve_args)
    if lxc_args:
        _validate_lxc_args(lxc_args)
    # Validate --env shape BEFORE any seed/config/env-file is written. A bad
    # entry (embedded newline, missing '=', bad key) would otherwise corrupt
    # the cloud-init YAML block scalar / /etc/environment / lxc.environment.
    if env:
        _validate_env(env)

    # Normalize --port to a list of forward specs (§5.7A). The un-re-pointed
    # CLI still passes a single string, so accept str | list[str] | None and
    # collapse to ``port`` = list[str] | None for the rest of create(). An
    # explicit empty list reads as "no forwards" (None) — there is no in-place
    # clear at create time. After this point ``port`` is always a list or None;
    # ``port is not None`` therefore still reads as "forwarding requested" for
    # the mode-validation and config-generation booleans below. The non-"auto"
    # specs are validated/deduped EARLY (fail fast, before any state mutation)
    # via the Block-02 boundary parser; "auto" entries are placeholders the
    # write site resolves under kento_lock, so they bypass this pre-parse.
    port = _normalize_port_specs(port)

    # Validate and read SSH key files early (before any filesystem changes)
    ssh_key_contents: str | None = None
    if ssh_keys:
        parts = []
        for key_path in ssh_keys:
            p = Path(key_path)
            if not p.is_file():
                raise ValidationError(f"SSH key file not found: {key_path}")
            parts.append(p.read_text())
        ssh_key_contents = "\n".join(parts)
        if not ssh_key_contents.endswith("\n"):
            ssh_key_contents += "\n"

    # Validate --ssh-host-key-dir early
    if ssh_host_key_dir is not None:
        src = Path(ssh_host_key_dir)
        if not src.is_dir():
            raise ValidationError(
                f"SSH host key directory not found: {ssh_host_key_dir}"
            )
        has_key = any(f.name.startswith("ssh_host_") and f.name.endswith("_key")
                      and f.is_file() for f in src.iterdir())
        if not has_key:
            raise ValidationError(
                f"no ssh_host_*_key files found in {ssh_host_key_dir}"
            )

    # Resolve PVE promotion
    from kento.pve import is_pve
    if pve is True:
        if not is_pve():
            raise ModeError("--pve specified but this is not a PVE host")
        if mode == "vm":
            mode = "pve-vm"
        else:
            mode = "pve"
    elif pve is False:
        pass
    else:
        if is_pve():
            if mode == "vm":
                mode = "pve-vm"
            else:
                mode = "pve"

    # --lxc-arg targets plain-LXC's native config ONLY. On a PVE host the
    # LXC config IS the PVE .conf (which carries raw lxc.* lines via
    # --pve-arg), and VM modes have no native LXC config at all. Reject here
    # — after PVE promotion so `mode` is the resolved one — rather than
    # silently writing kento-lxc-args that nothing would ever consume.
    if lxc_args:
        if mode == "pve":
            raise ModeError(
                "--lxc-arg is not supported on a PVE host. On PVE "
                "the LXC config is the PVE config; use --pve-arg, which "
                "carries raw lxc.* lines."
            )
        if mode in ("vm", "pve-vm"):
            raise ModeError(
                "--lxc-arg is not applicable to VM modes (no native "
                "LXC config)."
            )

    # --unprivileged: validate mode + run the fail-closed per-layer idmap probe
    # BEFORE any filesystem mutation (mirrors the apparmor pre-flight's
    # fail-closed placement). `mode` is the PVE-resolved mode here.
    # Supported: lxc (plain) and pve (pve-lxc). Not supported: vm/pve-vm.
    if unprivileged:
        if mode in ("vm", "pve-vm"):
            raise ModeError(
                "--unprivileged applies to LXC modes only (VMs have their "
                "own isolation)."
            )
        # For both lxc and pve (pve-lxc): probe the per-layer idmap capability
        # (kernel >= 5.19 overlay-over-idmapped-lowers + util-linux >= 2.40
        # X-mount.idmap). The hook idmaps each lowerdir; no merged-overlay
        # idmap (lxc.rootfs.options=idmap=container) is required or emitted.
        if not _perlayer_idmap_supported():
            raise StateError(
                "--unprivileged requires per-layer idmap support:\n"
                "  - kernel >= 5.19 (overlay-over-idmapped-lowers, mainline)\n"
                "  - util-linux >= 2.40 (X-mount.idmap mount option)\n"
                "This kernel or util-linux version does not meet the requirement.\n"
                "Create without --unprivileged to use the default (privileged) mode."
            )

    # Pre-validate PVE snippets storage (before any filesystem writes).
    # pve-vm always needs it; pve-lxc needs it when port/memory/cores set
    # (PVE strips lxc.hook.start-host, so we use hookscript: instead).
    _snippets_info = None
    if mode == "pve-vm":
        from kento.vm_hook import find_snippets_dir
        _snippets_info = find_snippets_dir()
    elif mode == "pve" and (port is not None or memory is not None
                             or cores is not None):
        from kento.vm_hook import find_snippets_dir
        _snippets_info = find_snippets_dir()

    # Resolve network configuration
    from kento import resolve_network
    network = resolve_network(net_type, bridge, mode, port)
    bridge = network["bridge"]
    port = network["port"]

    # Plain VM mode has no tap/bridge wiring in start_vm (QEMU would need a
    # tap device; only -netdev user is implemented). Reject bridge networking
    # up front so the VM doesn't boot with zero NICs and no warning.
    if mode == "vm" and network["type"] == "bridge":
        raise ModeError(
            "plain VM mode does not support bridge networking.\n"
            "  Use --network usermode (default) for outbound access and\n"
            "  port forwarding via --port, or run on a PVE host for\n"
            "  bridged VMs."
        )

    # Determine base directory for this mode
    base_dir = VM_BASE if mode in ("vm", "pve-vm") else LXC_BASE

    # Validate mode-specific flags (pure validation — no state mutation, so
    # no need to hold the lock around these).
    if vmid and mode not in ("pve", "pve-vm"):
        raise ModeError(f"--vmid cannot be used with {mode.upper()} mode")
    if port is not None and mode in ("lxc", "pve"):
        if network["type"] != "bridge":
            raise ValidationError(
                "--port requires bridge networking for LXC/PVE mode"
            )
    if port is not None and mode in ("vm", "pve-vm"):
        if network["type"] == "bridge":
            raise ValidationError(
                "--port cannot be used with bridge networking in VM mode"
            )
    if gateway and not ip:
        raise ValidationError("--gateway requires --ip")
    # F10: --ip / --gateway only make sense with bridge networking. Silent
    # acceptance with usermode/host/none produced broken configs: usermode
    # gets a conflicting DHCP lease from QEMU's built-in 10.0.2.x while
    # systemd-networkd fights for the static address; none/host have no
    # interface for the address to bind to.
    if network["type"] in ("none", "host", "usermode"):
        if ip:
            raise ValidationError(
                f"--ip requires bridge networking; got --network {network['type']}.\n"
                "  Use --network bridge (or bridge=<name>) for a static IP, "
                "or remove --ip."
            )
        if gateway:
            raise ValidationError(
                f"--gateway requires bridge networking; got --network {network['type']}."
            )

    # Resolve layers (validates image exists). Done BEFORE the lock and before
    # any directory is created: image resolution depends only on ``image``, not
    # on name/vmid/container_dir, so a missing image fails here with ZERO
    # filesystem side effects (no orphan instance dir left behind — F2). Staying
    # outside the lock also avoids serializing image pulls across concurrent
    # creates. resolve_layers either returns a non-empty string or raises
    # ImageNotFoundError on a missing image, so no defensive empty-string check
    # is needed here.
    layers = resolve_layers(image)

    # Fail closed on an over-deep overlay BEFORE any filesystem side effect.
    # The kernel caps classic mount(2) options at one 4096-byte page; an image
    # with more than MAX_OVERLAY_LAYERS layers would overrun it and be silently
    # truncated, failing the overlay mount with cryptic errors. The layer-count
    # cap is checked here (zero side effects); the exact-byte backstop runs once
    # state_dir is known (below). See layers.preflight_overlay_layers.
    from kento.layers import preflight_overlay_layers
    preflight_overlay_layers(layers)

    # detect_cloudinit() does filesystem I/O over every layer. ``layers`` is
    # resolved once and never reassigned, so probe once and reuse the boolean
    # at all three decision sites below (cloudinit-mode precondition, the
    # root-ssh advisory, and the effective config-mode selection).
    has_cloudinit = detect_cloudinit(layers)

    # F7 + F11: hold the cross-process kento lock across the entire
    # allocate-and-commit sequence. Two concurrent `kento create` processes
    # would otherwise race on next_instance_name / next_vmid / container_dir
    # exists check, potentially ending up with the same name, VMID, or
    # stomping each other's directory. Lock covers from just before name
    # resolution through the container_dir mkdir; slower work (resolve_layers,
    # image pulls, config writes) happens after release so we don't serialize
    # the hot path. Port allocation further down has its own narrower lock.
    with kento_lock():
        # Resolve container name
        if name is None:
            base_name = sanitize_image_name(image)
            other_dir = LXC_BASE if base_dir == VM_BASE else VM_BASE
            name = next_instance_name(base_name, base_dir, other_dir=other_dir)
            # Defend against pathological image refs that sanitize into something
            # unsafe (e.g. leading-dot or embedded slash after transformation).
            # The CLI validates explicit --name; this covers the auto-generated
            # path so downstream hook templates / path joins never see a bad name.
            validate_name(name, what="auto-generated name")
        else:
            # Scan both namespaces: for PVE-LXC/PVE-VM, container_id is the VMID
            # while `name` lives in kento-name, so a bare (base_dir / name).exists()
            # misses same-name duplicates across VMIDs. When --force is set, only
            # scan the current namespace — the user has opted in to duplicate names
            # across namespaces (bare shortcuts like `kento start foo` then require
            # explicit `kento lxc start foo` / `kento vm start foo`).
            if force:
                conflict = _scan_namespace(name, base_dir) is not None
            else:
                conflict = (_scan_namespace(name, LXC_BASE) is not None
                            or _scan_namespace(name, VM_BASE) is not None)
            if conflict:
                raise InstanceExistsError(f"instance name already taken: {name}")

        # Resolve container_id for directory paths
        if mode == "pve":
            from kento.pve import next_vmid, validate_vmid, generate_pve_config, write_pve_config
            if vmid:
                validate_vmid(vmid)
            else:
                vmid = next_vmid()
            container_id = str(vmid)
            logger.info("Mode: pve (VMID %s)", vmid)
        elif mode == "pve-vm":
            from kento.pve import next_vmid, validate_vmid, generate_qm_config, write_qm_config
            from kento.vm_hook import write_vm_hook, write_snippets_wrapper
            if vmid:
                validate_vmid(vmid)
            else:
                vmid = next_vmid()
            container_id = name  # VM_BASE uses name, not VMID
            logger.info("Mode: pve-vm (VMID %s)", vmid)
        elif mode == "vm":
            container_id = name
            logger.info("Mode: vm")
        else:
            container_id = name
            logger.info("Mode: lxc")

        container_dir = base_dir / container_id

        if container_dir.exists():
            raise InstanceExistsError(f"instance already exists: {container_id}")

        # Create container_dir inside the lock so a concurrent create() sees
        # it on its own .exists() check above. Slower post-setup happens
        # outside the lock; the try/undos below registers the rmtree cleanup
        # as the very first undo so any later failure rolls back this mkdir.
        (container_dir / "rootfs").mkdir(parents=True)

    # F14: explicit --config-mode cloudinit without cloud-init in the image
    # is a user error — the seed we'd write would never be consumed and the
    # guest would boot unconfigured. Reject up front rather than warning and
    # silently producing a broken instance. ``auto`` falls back to injection.
    if config_mode == "cloudinit" and not has_cloudinit:
        shutil.rmtree(container_dir, ignore_errors=True)
        raise ValidationError(
            f"--config-mode cloudinit requires cloud-init in the "
            f"image, but none was detected in {image}.\n"
            "  Drop --config-mode to auto-detect, or use "
            "--config-mode injection."
        )

    # Advisory (non-fatal): cloud images (Debian/Ubuntu cloud) lock root SSH
    # login and expect a distro login user (e.g. ``debian``). Injecting keys
    # for root on such an image is a footgun, so warn — but do NOT change
    # behavior or exit. Applies regardless of config_mode: the root-login
    # restriction affects both injection and cloudinit seeding.
    if (ssh_key_contents is not None and ssh_key_user == "root"
            and has_cloudinit):
        logger.warning(
            "injecting SSH keys for 'root' on a cloud-init image. Cloud images\n"
            "  usually disable root SSH login; if you can't connect, "
            "recreate with\n"
            "  --ssh-key-user <user> (e.g. 'debian' for Debian cloud "
            "images)."
        )

    # Accumulator of rollback actions for every side-effecting step past
    # this point. On exception, each undo runs in LIFO order — see F4 in
    # the edge-case audit for the original motivation. container-dir goes
    # first so it's the last thing unwound (after image-hold / state-dir).
    undos: list[tuple[str, object]] = [
        ("container-dir",
         lambda: shutil.rmtree(container_dir, ignore_errors=True)),
    ]

    try:
        from kento.layers import create_image_hold, remove_image_hold
        create_image_hold(image, name)
        undos.append(("image-hold", lambda: remove_image_hold(name)))

        # Compute state_dir — upper/work may be outside container_dir for sudo users
        state_dir = upper_base(container_id, base_dir if mode in ("vm", "pve-vm") else None)

        state_dir_existed_outside = state_dir != container_dir and state_dir.exists()
        state_dir.mkdir(parents=True, exist_ok=True)
        if state_dir != container_dir and not state_dir_existed_outside:
            # Only schedule removal of a state_dir we just created (don't
            # nuke a pre-existing directory that happened to share the path).
            undos.append(("state-dir",
                          lambda: shutil.rmtree(state_dir, ignore_errors=True)))
        (state_dir / "upper").mkdir(exist_ok=True)
        (state_dir / "work").mkdir(exist_ok=True)

        # Byte backstop: now that state_dir (hence upper/work paths) is known,
        # assert the exact overlay options string can't truncate. Runs before
        # any hook/config/PVE state is generated; a violation rolls back via
        # the undos registered above. See preflight_overlay_layers.
        preflight_overlay_layers(layers, state_dir)

        # Write image reference, layer paths, state dir, mode, and name
        (container_dir / "kento-image").write_text(image + "\n")
        # Record the resolved content-ID alongside the (floating) tag so a
        # later scrub/diagnose can detect when the tag has moved to a new
        # image. Skip if it can't be resolved (older podman) — drift detection
        # simply degrades to a no-op for that guest.
        _img_id = resolve_image_id(image)
        if _img_id:
            (container_dir / "kento-image-id").write_text(_img_id + "\n")
        (container_dir / "kento-layers").write_text(layers + "\n")
        (container_dir / "kento-state").write_text(str(state_dir) + "\n")
        (container_dir / "kento-mode").write_text(mode + "\n")
        (container_dir / "kento-name").write_text(name + "\n")
        # Persist the resolved hostname (the §11.0 † back-fill, authorized run
        # 33). Only when an explicit hostname was given — a None hostname keeps
        # the legacy no-write behavior, where the read path falls back to `name`
        # (and `set_cmd` writes kento-hostname on a later set, the same key).
        if hostname is not None:
            (container_dir / "kento-hostname").write_text(hostname + "\n")

        # Persist the resolved network identity so a future `kento set`
        # net-rewrite can faithfully re-emit network config without
        # re-deriving the type/bridge. Common to all four modes — every
        # instance has a resolved type (bridge/host/usermode/none). The
        # bridge name only exists for bridge modes, so write kento-bridge
        # only when present (consumers tolerate absence). Preserved verbatim
        # across scrub (reset.py never deletes unknown kento-* files).
        (container_dir / "kento-net-type").write_text(network["type"] + "\n")
        if network["bridge"] is not None:
            (container_dir / "kento-bridge").write_text(network["bridge"] + "\n")

        # Persist pass-through flags (v1.2.0 Phase B). Consumed by:
        #   - vm.py start_vm() : appends kento-qemu-args to QEMU argv (B2)
        #   - pve.py write_*_config() : appends kento-pve-args lines (B3)
        #   - info.py --verbose : surfaces both (B4)
        # Only create the file if flags were passed — consumers tolerate
        # absence. Preserved verbatim across scrub.
        if qemu_args:
            (container_dir / "kento-qemu-args").write_text(
                "\n".join(qemu_args) + "\n")
        if pve_args:
            (container_dir / "kento-pve-args").write_text(
                "\n".join(pve_args) + "\n")
        # --lxc-arg (E1b): raw lines into plain-LXC's native config. Written
        # BEFORE generate_config() so it can read the file back and append
        # the block verbatim after kento's own lines. Scope-guarded above to
        # plain lxc only; preserved verbatim across scrub.
        if lxc_args:
            (container_dir / "kento-lxc-args").write_text(
                "\n".join(lxc_args) + "\n")

        # Write static IP config if requested
        if ip or dns or searchdomain:
            net_parts = []
            if ip:
                net_parts.append(f"ip={ip}")
            if gateway:
                net_parts.append(f"gateway={gateway}")
            if dns:
                net_parts.append(f"dns={dns}")
            if searchdomain:
                net_parts.append(f"searchdomain={searchdomain}")
            (container_dir / "kento-net").write_text("\n".join(net_parts) + "\n")
            if ip:
                _inject_network_config(state_dir, ip, gateway, dns, searchdomain,
                                       mode=mode)
            elif dns or searchdomain:
                resolved_dir = state_dir / "upper" / "etc" / "systemd" / "resolved.conf.d"
                resolved_dir.mkdir(parents=True, exist_ok=True)
                lines = ["[Resolve]"]
                if dns:
                    lines.append(f"DNS={dns}")
                if searchdomain:
                    lines.append(f"Domains={searchdomain}")
                lines.append("")
                (resolved_dir / "90-kento.conf").write_text("\n".join(lines))

        # Write hostname into guest. Use an explicit hostname when given (so the
        # guest gets the real UTS name BEFORE start, where start=True boots
        # before any post-create fixup), else fall back to `name` (legacy,
        # byte-identical when hostname is None).
        _inject_hostname(state_dir, hostname or name)

        # Write timezone config if requested
        if timezone:
            (container_dir / "kento-tz").write_text(timezone + "\n")
            _inject_timezone(state_dir, timezone)

        # Write environment variables if requested
        if env:
            (container_dir / "kento-env").write_text("\n".join(env) + "\n")
            _inject_env(state_dir, env)

        # Write SSH authorized_keys metadata if requested. Hook copies this into
        # the guest's ~/.ssh/authorized_keys on every start (target user controlled
        # by kento-ssh-user, defaulting to root).
        if ssh_key_contents is not None:
            (container_dir / "kento-authorized-keys").write_text(ssh_key_contents)
        if ssh_key_user != "root":
            (container_dir / "kento-ssh-user").write_text(ssh_key_user + "\n")

        # Generate or copy SSH host keys
        if ssh_host_keys:
            _generate_ssh_host_keys(container_dir / "ssh-host-keys")
        elif ssh_host_key_dir is not None:
            _copy_ssh_host_keys(Path(ssh_host_key_dir), container_dir / "ssh-host-keys")

        # Determine config mode (injection vs cloud-init). The
        # --config-mode=cloudinit without detected cloud-init case is
        # rejected earlier (F14); here we only choose between valid modes.
        if config_mode == "auto":
            if has_cloudinit:
                effective_config_mode = "cloudinit"
            else:
                effective_config_mode = "injection"
        else:
            effective_config_mode = config_mode

        # Write config mode metadata
        (container_dir / "kento-config-mode").write_text(effective_config_mode + "\n")

        # Generate cloud-init seed if in cloudinit mode
        if effective_config_mode == "cloudinit":
            host_key_dir = container_dir / "ssh-host-keys"
            write_seed(
                container_dir, name=name,
                ip=ip, gateway=gateway, dns=dns, searchdomain=searchdomain,
                timezone=timezone, env=env,
                ssh_keys=ssh_key_contents, ssh_key_user=ssh_key_user,
                ssh_host_key_dir=host_key_dir if host_key_dir.is_dir() else None,
            )

        if mode in ("vm", "pve-vm"):
            # Resolve MAC address for VM modes: user override wins, otherwise
            # auto-generate a stable deterministic MAC from the container name
            # (plain VM) or VMID (PVE-VM). Writing the result to kento-mac means
            # scrub/recreate keep the same MAC (external DHCP reservations work).
            from kento.vm import generate_mac
            if mac is None:
                if mode == "pve-vm":
                    mac_value = generate_mac(str(vmid))
                else:
                    mac_value = generate_mac(name)
            else:
                mac_value = mac
            (container_dir / "kento-mac").write_text(mac_value + "\n")

            # Resolve memory/cores: CLI > config file > hardcoded defaults
            from kento.defaults import get_vm_defaults
            vm_defaults = get_vm_defaults()
            effective_memory = memory if memory is not None else vm_defaults["memory"]
            effective_cores = cores if cores is not None else vm_defaults["cores"]
            # Clamp vCPUs to node capacity (loud) + warn on memory overcommit
            # BEFORE persisting/handing to qm: an over-request would otherwise
            # create an unstartable guest (qm refuses > node CPUs).
            effective_cores = _clamp_vm_capacity(
                effective_cores, effective_memory, mode=mode)
            (container_dir / "kento-memory").write_text(str(effective_memory) + "\n")
            (container_dir / "kento-cores").write_text(str(effective_cores) + "\n")
            (container_dir / "kento-nesting").write_text(
                "1\n" if nesting else "0\n")

            # Write port mapping (usermode networking only).
            # Hold kento_lock around allocate_port so two concurrent creates
            # can't both pick the same host port (the allocator reads all
            # existing kento-port files and bind-tests, but without a lock
            # the scan and the write are a classic TOCTOU race).
            if network["type"] == "usermode":
                # N forwards (§5.7A): resolve any "auto" host ports + write one
                # spec line each, under kento_lock (TOCTOU discipline). With no
                # --port at all, usermode still wants the default SSH forward, so
                # synthesize a single "auto" (host:22) — preserving prior
                # behavior where a portless usermode VM got an allocated SSH port.
                _specs = port if port is not None else ["auto"]
                _resolved = _write_port_specs(container_dir, _specs)
                host_port, guest_port = _resolved[0].split("/")[0].split(":")

            if mode == "pve-vm":
                # Generate VM hookscript + inject.sh (hookscript invokes inject.sh
                # in its pre-start phase after overlayfs mount, before virtiofsd).
                write_vm_hook(container_dir, layers, name, state_dir)
                write_inject(container_dir)

                # Write snippets wrapper and get PVE reference
                hookscript_ref = write_snippets_wrapper(
                    vmid, container_dir / "kento-hook",
                    snippets_dir=_snippets_info[0],
                    storage_name=_snippets_info[1],
                )
                from kento.vm_hook import delete_snippets_wrapper
                undos.append(("vm-snippets-wrapper",
                              lambda v=vmid: delete_snippets_wrapper(v)))

                # Write VMID reference
                (container_dir / "kento-vmid").write_text(str(vmid) + "\n")

                # Generate and write QM config
                qm_conf = write_qm_config(
                    vmid,
                    generate_qm_config(
                        name, vmid, container_dir,
                        hookscript_ref=hookscript_ref,
                        memory=effective_memory,
                        cores=effective_cores,
                        machine=vm_defaults["machine"],
                        kvm=vm_defaults["kvm"],
                        bridge=bridge,
                        net_type=network.get("type"),
                        mac=mac_value,
                    ),
                )
                from kento.pve import delete_qm_config
                undos.append(("qm-config",
                              lambda v=vmid: delete_qm_config(v)))

                logger.info("\nVM created: %s", name)
                logger.info("  Image:   %s", image)
                logger.info("  VMID:    %s", vmid)
                if network["type"] == "usermode":
                    logger.info("  Port:    %s:%s", host_port, guest_port)
                elif network["type"] == "bridge":
                    logger.info("  Bridge:  %s", bridge)
                logger.info("  Config:  %s", qm_conf)
                logger.info("  Nesting: %s", 'allowed' if nesting else 'disabled')
                logger.info("  Dir:     %s", container_dir)
            else:
                # Plain VM mode (no PVE)
                write_inject(container_dir)
                logger.info("\nContainer created: %s", name)
                logger.info("  Image:   %s", image)
                if network["type"] == "usermode":
                    logger.info("  Port:    %s:%s", host_port, guest_port)
                logger.info("  Nesting: %s", 'allowed' if nesting else 'disabled')
                logger.info("  Dir:     %s", container_dir)
        else:
            # Port forwarding for LXC/PVE modes. Resolve any "auto" host ports
            # and write one §5.7A spec line each, holding kento_lock across
            # allocate + write so concurrent creates don't collide on the same
            # free port (same race as the VM-mode branch above), extended to N.
            if port is not None:
                _resolved = _write_port_specs(container_dir, port)
                host_port, guest_port = _resolved[0].split("/")[0].split(":")

            # Persist memory/cores so the start-host hook can propagate the limit
            # into the inner ns cgroup on PVE-LXC (outer cgroup gets the ceiling
            # from PVE's `memory:`/`cpulimit:`, but processes live in ns/ and
            # read "max" without this).
            if memory is not None:
                (container_dir / "kento-memory").write_text(str(memory) + "\n")
            if cores is not None:
                (container_dir / "kento-cores").write_text(str(cores) + "\n")
            (container_dir / "kento-nesting").write_text(
                "1\n" if nesting else "0\n")
            if unprivileged:
                (container_dir / "kento-unprivileged").write_text("1\n")

            # Generate hook (LXC/PVE only) + inject.sh (shared with VM/PVE-VM modes)
            write_hook(container_dir, layers, name, state_dir)
            write_inject(container_dir)

            # Generate config
            if mode == "pve":
                hookscript_ref = None
                if _snippets_info is not None:
                    from kento.lxc_hook import (write_lxc_snippets_wrapper,
                                                delete_lxc_snippets_wrapper)
                    hookscript_ref = write_lxc_snippets_wrapper(
                        vmid, container_dir / "kento-hook",
                        snippets_dir=_snippets_info[0],
                        storage_name=_snippets_info[1],
                    )
                    undos.append(("lxc-snippets-wrapper",
                                  lambda v=vmid: delete_lxc_snippets_wrapper(v)))
                pve_conf_text = generate_pve_config(
                    name, vmid, container_dir, bridge=bridge,
                    net_type=network.get("type"),
                    nesting=nesting, ip=ip,
                    gateway=gateway, nameserver=dns,
                    searchdomain=searchdomain,
                    timezone=timezone, env=env,
                    port=port,
                    memory=memory, cores=cores,
                    hookscript_ref=hookscript_ref,
                    unprivileged=unprivileged)
                # Record the effective idmap range for the unprivileged hook.
                # At PVE pre-start time the hook idmaps each lowerdir, but PVE
                # may not have populated lxc.idmap into the runtime config yet
                # (that timing is PVE-internal). Persisting the range here makes
                # the hook independent of that ordering. Follow PVE: honour a
                # custom `lxc.idmap = u 0 B C` line if present, else PVE's
                # unprivileged default (100000 65536). See _pve_idmap_range().
                if unprivileged:
                    base, count = _pve_idmap_range(pve_conf_text)
                    (container_dir / "kento-idmap-range").write_text(
                        f"{base} {count}\n")
                pve_conf = write_pve_config(vmid, pve_conf_text)
                from kento.pve import delete_pve_config
                undos.append(("pve-config",
                              lambda v=vmid: delete_pve_config(v)))
                config_path = str(pve_conf)
            else:
                (container_dir / "config").write_text(
                    generate_config(name, container_dir, bridge=bridge,
                                    net_type=network.get("type"),
                                    nesting=nesting,
                                    ip=ip, gateway=gateway, env=env,
                                    port=port,
                                    memory=memory, cores=cores,
                                    unprivileged=unprivileged,
                                    mode=mode)
                )
                config_path = f"{container_dir}/config"

            logger.info("\nContainer created: %s", name)
            logger.info("  Image:   %s", image)
            logger.info("  Bridge:  %s", bridge)
            if mode == "pve":
                logger.info("  VMID:    %s", vmid)
            if port is not None:
                logger.info("  Port:    %s:%s", host_port, guest_port)
            logger.info("  Nesting: %s", 'allowed' if nesting else 'disabled')
            logger.info("  Config:  %s", config_path)

        if start:
            logger.info("\nStarting...")
            # Register the stop undo BEFORE issuing start: if the start call
            # succeeds partially (container registers, then crashes), we still
            # want to attempt the stop on rollback.
            if mode == "pve-vm":
                undos.append(("qm-stop",
                              lambda v=vmid: subprocess.run(
                                  ["qm", "stop", str(v)],
                                  capture_output=True, check=False)))
                _run_start_or_rollback(
                    ["qm", "start", str(vmid)], name=name, scope="vm",
                )
            elif mode == "vm":
                from kento.vm import start_vm, stop_vm
                undos.append(("vm-stop",
                              lambda d=container_dir:
                                  stop_vm(d, force=True)))
                start_vm(container_dir, name)
            elif mode == "pve":
                undos.append(("pct-stop",
                              lambda v=vmid: subprocess.run(
                                  ["pct", "stop", str(v)],
                                  capture_output=True, check=False)))
                _run_start_or_rollback(
                    ["pct", "start", str(vmid)], name=name, scope="lxc",
                )
            else:
                undos.append(("lxc-stop",
                              lambda n=name: subprocess.run(
                                  ["lxc-stop", "-n", n],
                                  capture_output=True, check=False)))
                _run_start_or_rollback(
                    ["lxc-start", "-n", name], name=name, scope="lxc",
                )
            logger.info("  Status: running")
        else:
            logger.info("  Status: stopped (use 'kento start %s' to boot)", name)
    except BaseException as exc:
        # Rollback every side-effect we successfully made before re-raising.
        # Use BaseException so KentoError/KeyboardInterrupt also trigger cleanup.
        logger.info("\nError during create: %s", exc)
        logger.info("Rolling back partial state...")
        _run_cleanup(undos)
        raise
