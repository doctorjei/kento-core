"""Orphan reconcile — shared detection for orphaned kento PVE instances.

An **orphan** is a kento-managed PVE instance (mode ``pve`` or ``pve-vm``)
whose state dir survives but whose PVE ``.conf`` was destroyed out-of-band
(``pct``/``qm destroy``, a pmxcfs glitch, or a crash mid-operation). Plain
``lxc``/``vm`` instances have no PVE config and therefore can never orphan.

This module single-sources the gone-check that was previously duplicated
inline in ``list.list_containers``, ``diagnose._check_orphan``, and
``diagnose._check_vmid_health``. Those call sites delegate here so the
detection logic lives in one place.

Library code: silent (no print / sys.exit / stderr), best-effort, read-only.
Nothing here reaps or mutates state — that is Phase 2 (``reap_orphans``).

Safety invariant: an instance is classified as an orphan ONLY when
``pve_config_exists(...)`` returns a *definitive* ``False``. If that probe
raises ``PermissionError``/``OSError`` (e.g. it needs root) the result is
*indeterminate* and the instance is NEVER classified as an orphan.
"""

import logging
from pathlib import Path
from typing import Callable

from kento import (LXC_BASE, VM_BASE, pve_config_exists, read_mode,
                  require_root, resolve_any)
from kento.errors import ModeError, StateError
from kento.info import _read_meta
from kento.locking import kento_lock

logger = logging.getLogger("kento")


def _orphan_vmid(container_dir: Path, mode: str) -> str | None:
    """Return the vmid used for the PVE-config gone-check, or None.

    Mirrors list.py / diagnose.py exactly:
      - pve-lxc (``pve``): the container DIR NAME is the vmid.
      - pve-vm  (``pve-vm``): the vmid is recorded in the ``kento-vmid`` file.

    Returns None for a pve-vm whose ``kento-vmid`` file is missing/empty (the
    caller treats a None vmid as gone, matching the existing inline logic).
    """
    if mode == "pve":
        return container_dir.name
    return _read_meta(container_dir, "kento-vmid")


def _is_orphan(container_dir: Path, mode: str,
               config_exists: Callable[[str, str], bool] | None = None,
               ) -> bool | None:
    """Tri-state orphan predicate for one PVE instance.

    Returns:
      - ``True``  — definitively orphaned (PVE config gone / no vmid).
      - ``False`` — definitively healthy (PVE config present).
      - ``None``  — indeterminate (the config probe raised; needs root?).

    Non-PVE modes return ``False`` (cannot orphan). ``config_exists`` is
    injected so each call site can pass its own (test-patchable) binding of
    ``pve_config_exists`` — preserving the existing per-module patch points.
    Defaults (None) to this module's ``pve_config_exists``, looked up at call
    time so ``kento.reconcile.pve_config_exists`` stays patchable.
    """
    if config_exists is None:
        config_exists = pve_config_exists
    if mode not in ("pve", "pve-vm"):
        return False
    check_vmid = _orphan_vmid(container_dir, mode)
    if check_vmid is None:
        # No vmid recorded => the existing inline logic treats this as gone.
        return True
    try:
        return not config_exists(check_vmid, mode)
    except (PermissionError, OSError):
        # Indeterminate: never classify as orphan on uncertainty.
        return None


def find_orphans(scope: str | None = None) -> list[dict]:
    """Enumerate kento PVE instances whose PVE ``.conf`` is definitively gone.

    Returns a list of dicts::

        {"name", "vmid", "mode", "container_dir", "image"}

    where ``mode`` is the raw kento mode (``"pve"`` or ``"pve-vm"``),
    ``vmid`` is an ``int`` when it parses (else the raw string, or None),
    ``container_dir`` is a ``Path``, ``name`` is the display name
    (``kento-name`` file or the dir name), and ``image`` is the recorded
    image reference (or None if unreadable).

    Only modes ``pve``/``pve-vm`` can orphan; plain ``lxc``/``vm`` are never
    returned. ``scope=None`` scans both namespaces; ``scope="lxc"`` /
    ``scope="vm"`` narrows to one base (mirroring how list/diagnose scope).

    Safety: an instance is included ONLY when its PVE config is definitively
    gone. An indeterminate probe (``PermissionError``/``OSError``) → skipped,
    never returned. Best-effort and read-only: missing base dirs, non-integer
    dir names, missing metadata files, and per-instance ``OSError`` are all
    tolerated, never fatal.
    """
    image_files = []
    if scope in (None, "lxc"):
        if LXC_BASE.is_dir():
            image_files.extend(LXC_BASE.glob("*/kento-image"))
    if scope in (None, "vm"):
        if VM_BASE.is_dir():
            image_files.extend(VM_BASE.glob("*/kento-image"))

    orphans: list[dict] = []
    for image_file in sorted(image_files, key=lambda f: f.parent.name):
        # Mirror list.py: a concurrent destroy can race between the glob and
        # the reads below. Skip the bad entry rather than aborting.
        try:
            container_dir = image_file.parent
            mode = read_mode(container_dir)
            if mode not in ("pve", "pve-vm"):
                continue

            if _is_orphan(container_dir, mode) is not True:
                # False (healthy) or None (indeterminate) → not an orphan.
                continue

            display = _read_meta(container_dir, "kento-name") or container_dir.name
            image = _read_meta(container_dir, "kento-image")
            raw_vmid = _orphan_vmid(container_dir, mode)
            vmid: int | str | None
            if raw_vmid is None:
                vmid = None
            else:
                try:
                    vmid = int(raw_vmid)
                except (TypeError, ValueError):
                    vmid = raw_vmid

            orphans.append({
                "name": display,
                "vmid": vmid,
                "mode": mode,
                "container_dir": container_dir,
                "image": image,
            })
        except OSError:
            continue

    return orphans


def reap_orphans(reap: bool = False, scope: str | None = None) -> list[dict]:
    """Discard orphaned PVE instances (state dir survives, PVE .conf gone).

    ``reap=False`` (default): dry-run — enumerate orphans, reap nothing.
    ``reap=True``: ``destroy(force=True)`` each orphan.

    Returns a list of dicts::

        {"name", "vmid", "mode", "reaped": bool, "error": str | None}

    one entry per orphan found, in the order ``find_orphans`` returns them.
    ``reaped``/``error`` reflect what happened: on a dry-run every entry has
    ``reaped=False`` / ``error=None``; under ``reap=True`` a destroyed orphan
    has ``reaped=True`` / ``error=None`` and a failed one ``reaped=False`` with
    the exception text in ``error``.

    Safety: this acts ONLY on what ``find_orphans(scope)`` returns (definitively
    orphaned instances). It performs no independent enumeration, so the
    "never reap a healthy/indeterminate instance" invariant lives entirely in
    ``find_orphans``. Per-orphan failure is isolated — an exception from
    ``destroy`` is caught, recorded in ``error``, and reaping continues to the
    next orphan. This never raises for a single failure.

    Library code: silent (no print / sys.exit / stderr). The CLI renders the
    result via ``format_reap``.
    """
    orphans = find_orphans(scope)

    # Import lazily (mirrors how destroy is pulled in across the lib), but
    # ONCE before the loop — not per-orphan. Inside the loop body a failed
    # import would be swallowed by the per-orphan `except Exception` and
    # mis-reported once per orphan; hoisting it makes such a failure surface
    # cleanly. The per-orphan try/except still isolates each destroy() call.
    if reap:
        from kento.destroy import destroy

    results: list[dict] = []
    for o in orphans:
        entry = {
            "name": o["name"],
            "vmid": o["vmid"],
            "mode": o["mode"],
            "reaped": False,
            "error": None,
        }
        if reap:
            # Pass the already-resolved container_dir/mode so destroy does
            # not have to re-resolve the name.
            try:
                destroy(o["name"], force=True,
                        container_dir=o["container_dir"], mode=o["mode"])
                entry["reaped"] = True
            except Exception as e:  # isolate per-orphan failure
                entry["error"] = str(e) or e.__class__.__name__
                logger.warning("failed to reap orphan %s (vmid %s): %s",
                               o["name"], o["vmid"], e)
        results.append(entry)

    return results


def format_reap(results: list[dict], reaped: bool) -> str:
    """Render a ``reap_orphans`` result list as a human-readable string.

    ``reaped`` mirrors the ``reap`` argument passed to ``reap_orphans``:
    ``False`` renders the dry-run plan (what WOULD be destroyed, with a
    ``--yes`` hint); ``True`` reports each orphan as reaped or failed.
    Returns the joined string (no trailing newline); the caller prints it.
    """
    if not results:
        return "Orphans: none found."

    lines = ["Orphans:"]
    if not reaped:
        lines.append(f"  Dry run — nothing destroyed. {len(results)} orphaned "
                     f"instance(s) WOULD be destroyed (state discarded):")
        for r in results:
            lines.append(f"    {r['name']}  (vmid {r['vmid']}, {r['mode']})")
        lines.append("  Run 'kento prune --orphans --yes' to destroy them.")
        return "\n".join(lines)

    reaped_n = sum(1 for r in results if r["reaped"])
    failed = [r for r in results if not r["reaped"]]
    for r in results:
        if r["reaped"]:
            lines.append(f"  reaped {r['name']}  (vmid {r['vmid']}, {r['mode']})")
        else:
            lines.append(f"  FAILED {r['name']}  (vmid {r['vmid']}, {r['mode']}): "
                         f"{r['error']}")
    lines.append(f"Destroyed {reaped_n} orphan(s)"
                 + (f", {len(failed)} failed." if failed else "."))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Adopt: heal one orphan by regenerating its missing PVE config from state.
# ---------------------------------------------------------------------------

def _adopt_vmid(container_dir: Path, mode: str) -> int:
    """Recover the integer vmid for an orphan from surviving state.

    pve-lxc: the container DIR NAME is the vmid (mirrors create/destroy).
    pve-vm:  the vmid is recorded in the ``kento-vmid`` file.

    Raises StateError if the vmid is missing or non-integer — adopt cannot
    rebuild a config without knowing which slot to write.
    """
    if mode == "pve":
        raw = container_dir.name
    else:
        raw = _read_meta(container_dir, "kento-vmid")
    if raw is None:
        raise StateError(
            f"cannot recover vmid for '{container_dir.name}' from surviving "
            "state; nothing to adopt."
        )
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise StateError(
            f"recorded vmid {raw!r} is not an integer; cannot adopt."
        )


def _parse_net_meta(container_dir: Path) -> dict:
    """Parse kento-net (ip=/gateway=/dns=/searchdomain=) into a dict.

    Mirrors set_cmd._parse_kento_net / reset.py: each non-empty key=value
    line, only the four recognized keys retained. Absent file -> all None.
    """
    out = {"ip": None, "gateway": None, "dns": None, "searchdomain": None}
    p = container_dir / "kento-net"
    if not p.is_file():
        return out
    for line in p.read_text().strip().splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k in out:
            out[k] = v or None
    return out


def emit_pve_config(container_dir: Path, mode: str) -> int:
    """Regenerate the snippets wrapper + hook + PVE ``.conf`` for one instance.

    This is the heart of ``adopt``: it rebuilds the *derived* PVE artifacts
    (the snippets-wrapper hookscript, the kento hook, and the ``.conf``
    itself) from kento's surviving on-disk metadata, producing a config
    byte-compatible with what ``create`` originally wrote. It does NOT mount
    the rootfs, start the instance, or touch the writable layer.

    ``mode`` must be ``"pve"`` (pve-lxc) or ``"pve-vm"``. Returns the vmid
    written. Pure regeneration — assumes the caller has already validated
    that the instance is an adoptable orphan (see ``adopt``).

    Library code: silent (no print / sys.exit / stderr); raises KentoError.
    """
    name = _read_meta(container_dir, "kento-name") or container_dir.name
    layers_file = container_dir / "kento-layers"
    layers = layers_file.read_text().strip() if layers_file.is_file() else ""
    state = _read_meta(container_dir, "kento-state")
    state_dir = Path(state) if state else container_dir
    vmid = _adopt_vmid(container_dir, mode)

    if mode == "pve-vm":
        from kento.vm_hook import write_vm_hook, write_snippets_wrapper
        from kento.pve import generate_qm_config, write_qm_config
        from kento.defaults import get_vm_defaults

        # Regenerate the VM hook (overlay assembly + virtiofsd) and the
        # snippets-wrapper that PVE's hookscript: field points at (always
        # required for pve-vm).
        write_vm_hook(container_dir, layers, name, state_dir)
        hookscript_ref = write_snippets_wrapper(
            vmid, container_dir / "kento-hook")

        # machine/kvm are not persisted -> best-available from defaults.
        vm_defaults = get_vm_defaults()
        mem_raw = _read_meta(container_dir, "kento-memory")
        cores_raw = _read_meta(container_dir, "kento-cores")
        memory = int(mem_raw) if mem_raw else vm_defaults["memory"]
        cores = int(cores_raw) if cores_raw else vm_defaults["cores"]
        net_type = _read_meta(container_dir, "kento-net-type")
        bridge = _read_meta(container_dir, "kento-bridge")
        mac = _read_meta(container_dir, "kento-mac")

        write_qm_config(
            vmid,
            generate_qm_config(
                name, vmid, container_dir,
                hookscript_ref=hookscript_ref,
                memory=memory, cores=cores,
                machine=vm_defaults["machine"],
                kvm=vm_defaults["kvm"],
                bridge=bridge, net_type=net_type, mac=mac,
            ),
        )
        return vmid

    # pve-lxc
    from kento.hook import write_hook
    from kento.pve import generate_pve_config, write_pve_config
    from kento.create import _pve_idmap_range

    write_hook(container_dir, layers, name, state_dir)

    # The snippets-wrapper is only needed when the instance carries
    # port/memory/cores metadata (the same condition reset.py / create use).
    # Otherwise the hook is referenced directly and hookscript_ref is None.
    hookscript_ref = None
    if any((container_dir / f).is_file()
           for f in ("kento-port", "kento-memory", "kento-cores")):
        from kento.lxc_hook import write_lxc_snippets_wrapper
        hookscript_ref = write_lxc_snippets_wrapper(
            vmid, container_dir / "kento-hook")

    net = _parse_net_meta(container_dir)
    nesting_raw = _read_meta(container_dir, "kento-nesting")
    nesting = nesting_raw == "1"
    unprivileged = _read_meta(container_dir, "kento-unprivileged") == "1"
    mem_raw = _read_meta(container_dir, "kento-memory")
    cores_raw = _read_meta(container_dir, "kento-cores")

    pve_conf_text = generate_pve_config(
        name, vmid, container_dir,
        bridge=_read_meta(container_dir, "kento-bridge"),
        net_type=_read_meta(container_dir, "kento-net-type"),
        nesting=nesting,
        ip=net["ip"], gateway=net["gateway"],
        nameserver=net["dns"], searchdomain=net["searchdomain"],
        timezone=_read_meta(container_dir, "kento-tz"),
        env=(env.splitlines()
             if (env := _read_meta(container_dir, "kento-env")) else None),
        port=_read_meta(container_dir, "kento-port"),
        memory=int(mem_raw) if mem_raw else None,
        cores=int(cores_raw) if cores_raw else None,
        hookscript_ref=hookscript_ref,
        unprivileged=unprivileged,
    )

    # Refresh the idmap range for the unprivileged hook from the freshly
    # generated text (mirrors create.py:1129-1132).
    if unprivileged:
        base, count = _pve_idmap_range(pve_conf_text)
        (container_dir / "kento-idmap-range").write_text(f"{base} {count}\n")

    write_pve_config(vmid, pve_conf_text)
    return vmid


def adopt(name: str, *, container_dir: Path | None = None,
          mode: str | None = None) -> dict:
    """Heal an orphaned PVE instance by regenerating its missing ``.conf``.

    An orphan is a kento-managed pve-lxc / pve-vm instance whose state dir
    survives but whose PVE config was destroyed out-of-band. ``adopt``
    rebuilds the derived PVE artifacts (snippets wrapper + hook + ``.conf``)
    from surviving state, bringing the instance back as a known instance. It
    does NOT auto-start or re-mount the rootfs — run ``kento start`` after.

    Returns ``{"name", "vmid", "mode"}`` for the caller to render.

    Refuses (raising the appropriate KentoError subclass) when:
      - the mode is not a PVE mode (ModeError);
      - the instance is NOT an orphan — its PVE config already exists
        (StateError);
      - the vmid is now occupied by a *different* instance (either config
        kind) — a collision (StateError);
      - required network metadata is missing (a pre-1.6.0 instance whose
        config is not faithfully recoverable) — fail closed (StateError).

    Library code: silent (no print / sys.exit / stderr); raises KentoError.
    """
    require_root()

    if container_dir is None or mode is None:
        container_dir, mode = resolve_any(name)

    if mode not in ("pve", "pve-vm"):
        raise ModeError(
            "adopt only applies to PVE instances (pve-lxc/pve-vm); "
            f"'{name}' is a plain {mode} instance with no PVE config."
        )

    vmid = _adopt_vmid(container_dir, mode)

    # Fail closed on un-recoverable network metadata. A pre-1.6.0 orphan may
    # predate kento-net-type; we refuse rather than emit a config that differs
    # from what create would have written. (No --flags fill path by design.)
    # Pure read; done outside the lock so we don't widen the critical section.
    if _read_meta(container_dir, "kento-net-type") is None:
        raise StateError(
            f"network config not recoverable from surviving state for "
            f"'{name}' (instance predates 1.6.0 metadata); use "
            f"'kento destroy -f {name}' to discard, or recreate."
        )

    # Hold the SAME lock create() holds across the check->write critical
    # section: the orphan-check, the vmid-occupied scan, and emit_pve_config
    # must be atomic w.r.t. a concurrent `kento create`. Without it, a create
    # could write a fresh config at this vmid between our check and our write,
    # and adopt would clobber it (TOCTOU). Scope mirrors create's: only the
    # check+validate+emit region — name resolution / metadata reads stay out.
    with kento_lock():
        # Not an orphan: its PVE config already exists -> nothing to adopt.
        # An indeterminate probe (PermissionError/OSError — e.g. /etc/pve
        # unreadable) must NEVER act: refuse cleanly as a KentoError rather
        # than let a raw OSError escape (matches find_orphans/_is_orphan,
        # which treat indeterminate as "not classified").
        try:
            already_exists = pve_config_exists(str(vmid), mode)
        except (PermissionError, OSError) as e:
            raise StateError(
                f"cannot determine whether '{name}' (vmid {vmid}) is an "
                f"orphan: {e}. Re-run as root or retry when /etc/pve is "
                f"accessible."
            ) from e
        if already_exists:
            raise StateError(
                f"instance '{name}' is not an orphan; its PVE config exists "
                f"(vmid {vmid}). Nothing to adopt."
            )

        # vmid now occupied by a DIFFERENT instance in EITHER config namespace
        # (a pve-vm may have taken an orphaned pve-lxc's vmid, or vice versa).
        # Both probes above/here are for THIS vmid; since this instance's own
        # config is gone (checked above), any hit here is a foreign occupant.
        for other_mode in ("pve", "pve-vm"):
            try:
                occupied = pve_config_exists(str(vmid), other_mode)
            except (PermissionError, OSError) as e:
                raise StateError(
                    f"cannot determine vmid {vmid} occupancy: {e}. Retry "
                    f"when /etc/pve is accessible."
                ) from e
            if occupied:
                raise StateError(
                    f"cannot adopt '{name}': vmid {vmid} is already occupied "
                    f"by another PVE instance. Resolve the collision first "
                    f"(e.g. 'kento destroy -f {name}' to discard this orphan)."
                )

        emit_pve_config(container_dir, mode)

    display = _read_meta(container_dir, "kento-name") or container_dir.name
    logger.info("Adopted: %s (vmid %s)", display, vmid)
    return {"name": display, "vmid": vmid, "mode": mode}
