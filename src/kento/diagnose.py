"""kento diagnose — read-only health/triage of kento-managed instances.

This is library code (post-split): it is SILENT (no print / sys.exit / stderr),
raises only from the KentoError hierarchy, and RETURNS its results. The CLI
catches errors, prints the formatted report, and sets the exit code.

`run_diagnostics(name=None)` runs a host-wide (or single-instance) read-only
scan and returns a structured report; `format_diagnostics(report)` renders it
as a human string. Both are pure — diagnose NEVER mutates state (stale holds,
orphans, leaked mounts are REPORTED, not reaped).

Detection reuses the existing modules wherever possible (orphan check from the
same logic list.py uses, apparmor pre-flight helpers from create.py, hold
enumeration from images.py, vmid allocation from pve.py, cloud-init detection
from cloudinit.py) so diagnose and the live code paths agree.
"""

import logging
import os
import shutil
from pathlib import Path

# Bases + resolution. Bound at module level so tests can patch
# kento.diagnose.LXC_BASE / VM_BASE (mirrors test_list / test_images style).
from kento import (LXC_BASE, VM_BASE, InstanceNotFoundError, _scan_namespace,
                   is_running, pve_config_exists, read_mode, validate_name)
from kento.cloudinit import detect_cloudinit
from kento.create import _apparmor_active, _apparmor_parser_present
from kento.images import _guest_names, _holds
from kento.info import _read_meta
from kento.pve import _kento_recorded_vmids, is_pve, next_vmid
from kento.reconcile import _is_orphan, _orphan_vmid

logger = logging.getLogger("kento")


# --- finding helper ------------------------------------------------------


def _finding(category, severity, scope, message, remediation=None):
    """Build one finding dict in the canonical shape."""
    return {
        "category": category,
        "severity": severity,
        "scope": scope,
        "message": message,
        "remediation": remediation,
    }


# --- enumeration (mirrors list.list_containers) -------------------------


def _enumerate_instances(name=None):
    """Return [(container_dir, mode, display_name), ...].

    Host-wide when name is None (glob both bases, exactly like
    list.list_containers). When name is given, resolve that one instance
    (raises InstanceNotFoundError on a miss — same as the rest of the lib).
    """
    if name is not None:
        validate_name(name)
        # Resolve against the diagnose-bound bases (so tests that patch
        # kento.diagnose.LXC_BASE/VM_BASE resolve correctly). _scan_namespace
        # takes the base explicitly.
        hit = _scan_namespace(name, LXC_BASE)
        mode_default = "lxc"
        if hit is None:
            hit = _scan_namespace(name, VM_BASE)
            mode_default = "vm"
        if hit is None:
            raise InstanceNotFoundError(
                f"no instance named '{name}'. "
                f"Run 'kento list' to see available instances.")
        mode = read_mode(hit, mode_default)
        display = _read_meta(hit, "kento-name") or hit.name
        return [(hit, mode, display)]

    out = []
    image_files = []
    if LXC_BASE.is_dir():
        image_files.extend(LXC_BASE.glob("*/kento-image"))
    if VM_BASE.is_dir():
        image_files.extend(VM_BASE.glob("*/kento-image"))
    for image_file in sorted(image_files, key=lambda f: f.parent.name):
        try:
            container_dir = image_file.parent
            mode = read_mode(container_dir)
            display = (_read_meta(container_dir, "kento-name")
                       or container_dir.name)
            out.append((container_dir, mode, display))
        except OSError:
            # Survive a destroy race between glob and read (mirrors list.py).
            continue
    return out


# --- host-level checks ---------------------------------------------------


def _check_apparmor():
    """AppArmor pre-flight (host). Mirrors create.py's fail-closed gate.

    error when: apparmor active + effective profile 'generated' + parser
    absent (plain-lxc create would fail closed). Otherwise ok.
    """
    try:
        active = _apparmor_active()
    except (PermissionError, OSError):
        return [_finding("apparmor", "info", "host",
                         "could not determine AppArmor state (needs root?)",
                         None)]
    if not active:
        return [_finding("apparmor", "ok", "host",
                         "AppArmor not active on this kernel (no-op)", None)]

    profile = os.environ.get("KENTO_APPARMOR_PROFILE", "generated")
    if profile != "generated":
        return [_finding("apparmor", "ok", "host",
                         f"AppArmor active; KENTO_APPARMOR_PROFILE={profile} "
                         f"(generated pre-flight does not apply)", None)]
    try:
        parser = _apparmor_parser_present()
    except (PermissionError, OSError):
        return [_finding("apparmor", "info", "host",
                         "could not check for apparmor_parser", None)]
    if not parser:
        return [_finding(
            "apparmor", "error", "host",
            "AppArmor is active and the 'generated' profile is in effect, "
            "but apparmor_parser is not on PATH — plain-lxc create will "
            "fail closed.",
            "install apparmor_parser (apparmor package) or set "
            "KENTO_APPARMOR_PROFILE=unconfined")]
    return [_finding("apparmor", "ok", "host",
                     "AppArmor active and apparmor_parser present", None)]


def _check_stale_holds():
    """Stale image holds (host): a hold whose guest no longer exists.

    READ ONLY — mirrors the prune stale logic in images.py (orphan = hold
    whose held-for name is not in the live guest set) but never removes.
    """
    try:
        holds = _holds()
        guests = _guest_names()
    except (PermissionError, OSError):
        return [_finding("hold", "info", "host",
                         "could not enumerate image holds (podman/root?)",
                         None)]
    findings = []
    stale = [(n, img) for n, img in holds if n not in guests]
    if not stale:
        findings.append(_finding(
            "hold", "ok", "host",
            f"{len(holds)} image hold(s), none stale", None))
        return findings
    for n, img in stale:
        findings.append(_finding(
            "hold", "warn", "host",
            f"stale image hold 'kento-hold.{n}' pins {img or '?'} but guest "
            f"'{n}' no longer exists",
            "kento prune"))
    return findings


def _check_vmid_health():
    """VMID allocation health (host, pve only).

    Reports the next free vmid (pve.next_vmid) and how many recorded vmids
    belong to orphans (reserved-but-not-reaped — ties to the known open
    orphan-vmid item). warn if any reserved-orphan vmids exist.
    """
    try:
        if not is_pve():
            return []
    except (PermissionError, OSError):
        return []
    try:
        nxt = next_vmid()
        recorded = _kento_recorded_vmids()
    except (PermissionError, OSError):
        return [_finding("vmid", "info", "host",
                         "could not compute vmid allocation (needs root?)",
                         None)]

    # A recorded vmid whose PVE .conf is gone is reserved-but-orphaned.
    reserved_orphans = []
    for vmid in sorted(recorded):
        # Determine the mode: a recorded vmid could be pve-lxc (dir name) or
        # pve-vm (kento-vmid file). Check both config kinds defensively.
        try:
            lxc_gone = not pve_config_exists(str(vmid), "pve")
            vm_gone = not pve_config_exists(str(vmid), "pve-vm")
        except (PermissionError, OSError):
            continue
        if lxc_gone and vm_gone:
            reserved_orphans.append(vmid)

    if reserved_orphans:
        return [_finding(
            "vmid", "warn", "host",
            f"next free vmid is {nxt}; "
            f"{len(reserved_orphans)} recorded vmid(s) are reserved by "
            f"orphaned kento state and will not be reassigned: "
            f"{', '.join(str(v) for v in reserved_orphans)}",
            "kento adopt <name> to heal, or kento destroy -f <name> to "
            "discard, the orphan(s), then re-check")]
    return [_finding("vmid", "info", "host",
                     f"next free vmid is {nxt}; "
                     f"{len(recorded)} vmid(s) recorded by kento", None)]


# --- per-instance checks -------------------------------------------------


def _check_orphan(container_dir, mode, display):
    """Orphan check (pve modes): state present but PVE .conf gone.

    Detection is shared with list.list_containers via reconcile._is_orphan
    (pve-lxc uses the dir name as the vmid; pve-vm reads kento-vmid). The
    diagnose-bound pve_config_exists is passed in so the predicate probes
    through the same name diagnose's tests patch.
    """
    if mode not in ("pve", "pve-vm"):
        return []
    check_vmid = _orphan_vmid(container_dir, mode)
    gone = _is_orphan(container_dir, mode, pve_config_exists)
    if gone is None:
        # Indeterminate config probe (PermissionError/OSError).
        return [_finding("orphan", "info", display,
                         "could not check PVE config (needs root?)", None)]
    if gone:
        return [_finding(
            "orphan", "warn", display,
            f"instance '{display}' has kento state but its PVE config "
            f"(vmid {check_vmid or '?'}) is gone — orphaned",
            f"kento adopt {display} to heal it, or "
            f"kento destroy -f {display} to discard it")]
    return [_finding("orphan", "ok", display,
                     f"PVE config present (vmid {check_vmid})", None)]


def _check_portfwd(container_dir, display):
    """Port-forward state (per instance) from the hook's marker files."""
    err = container_dir / "kento-portfwd-error"
    active = container_dir / "kento-portfwd-active"
    backend = container_dir / "kento-portfwd-backend"
    try:
        if err.is_file():
            contents = err.read_text().strip()
            return [_finding(
                "portfwd", "error", display,
                f"port-forward setup error: {contents}",
                "check the host firewall (nft/iptables) then restart the "
                "instance")]
        if active.is_file():
            be = backend.read_text().strip() if backend.is_file() else "?"
            mapping = active.read_text().strip()
            return [_finding(
                "portfwd", "ok", display,
                f"port-forward active ({mapping}) via {be}", None)]
    except (PermissionError, OSError):
        return [_finding("portfwd", "info", display,
                         "could not read port-forward markers", None)]
    # No port forwarding configured for this instance.
    return []


def _check_mounts(container_dir, mode, display, running):
    """Leaked/broken mounts (per instance), for STOPPED instances.

    LXC: rootfs still a mountpoint => overlay leak.
    VM:  virtiofsd pid still alive => leaked helper.
    Only meaningful when stopped; a running instance is expected to hold its
    mounts / virtiofsd.
    """
    if running:
        return []
    findings = []

    if mode in ("vm", "pve-vm"):
        pid_file = container_dir / "kento-virtiofsd-pid"
        try:
            if pid_file.is_file():
                pid = int(pid_file.read_text().strip())
                alive = _pid_alive(pid)
                if alive:
                    findings.append(_finding(
                        "mount", "warn", display,
                        f"instance is stopped but virtiofsd (pid {pid}) is "
                        f"still alive — leaked helper process",
                        f"kento scrub {display} or kento destroy -f {display}"))
        except (PermissionError, OSError, ValueError):
            findings.append(_finding(
                "mount", "info", display,
                "could not check virtiofsd pid (needs root?)", None))
        return findings

    # LXC family: check rootfs mountpoint leak. The rootfs lives next to the
    # state dir; also check the state-dir merged mount if present.
    candidates = [container_dir / "rootfs"]
    state_text = _read_meta(container_dir, "kento-state")
    if state_text:
        candidates.append(Path(state_text) / "rootfs")
    seen = set()
    for rootfs in candidates:
        key = str(rootfs)
        if key in seen:
            continue
        seen.add(key)
        try:
            if os.path.ismount(rootfs):
                findings.append(_finding(
                    "mount", "warn", display,
                    f"instance is stopped but {rootfs} is still a mountpoint "
                    f"— leaked overlay mount",
                    f"kento scrub {display} or kento destroy -f {display}"))
        except (PermissionError, OSError):
            findings.append(_finding(
                "mount", "info", display,
                "could not check rootfs mount (needs root?)", None))
    return findings


def _pid_alive(pid):
    """True if pid is alive (signal 0). EPERM means it exists but is owned
    by another user — treat as alive. Wrapped so tests/no-root degrade."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _check_network(container_dir, mode, display, running):
    """networkd drop-ins / veth (per instance, best-effort).

    Verify kento's expected guest drop-ins exist in the overlay upper for
    static / nested instances. Host-veth-on-bridge (H-B) is a runtime check
    needing networkctl/ip — only flag it as a manual check.
    """
    findings = []
    try:
        state_text = _read_meta(container_dir, "kento-state")
        state_dir = Path(state_text) if state_text else container_dir
        net_dir = state_dir / "upper" / "etc" / "systemd" / "network"

        # kento-net (when present) holds the static network lines written at
        # create time (ip=/gateway=/dns=/...); a static *IP* is configured
        # exactly when it carries an `ip=` line — which is also the condition
        # under which create injects 05-kento-static.network.
        net = _read_meta(container_dir, "kento-net")
        has_static_ip = bool(net) and any(
            line.startswith("ip=") for line in net.splitlines())
        if has_static_ip:
            dropin = net_dir / "05-kento-static.network"
            if not dropin.exists():
                findings.append(_finding(
                    "network", "warn", display,
                    "static network configured but the kento drop-in "
                    "05-kento-static.network is missing from the overlay "
                    "upper — guest may not get its static address",
                    f"kento scrub {display} and recreate, or re-run create"))
            else:
                findings.append(_finding(
                    "network", "ok", display,
                    "static network drop-in present", None))

        nesting = _read_meta(container_dir, "kento-nesting")
        if nesting == "1":
            dropin = net_dir / "10-kento-nested-veth.network"
            if not dropin.exists():
                findings.append(_finding(
                    "network", "info", display,
                    "nesting enabled but 10-kento-nested-veth.network is not "
                    "in the overlay upper (may be image-baked or injected at "
                    "start)", None))
    except (PermissionError, OSError):
        return [_finding("network", "info", display,
                         "could not inspect network drop-ins", None)]

    # H-B host-veth enslavement is a runtime check; only attempt when running
    # and a tool is present, otherwise note it as a manual check.
    if running:
        tool = shutil.which("networkctl") or shutil.which("ip")
        if tool is None:
            findings.append(_finding(
                "network", "info", display,
                "host-veth-on-bridge is a runtime check; networkctl/ip not "
                "on PATH so it was skipped", None))
        else:
            findings.append(_finding(
                "network", "info", display,
                f"host-veth-on-bridge can be checked manually with "
                f"{os.path.basename(tool)} (networkctl status <hostveth>)",
                None))
    return findings


def _check_cloudinit(container_dir, display):
    """Cloud-init root-ssh footgun (per instance), advisory.

    If created from a cloud-init image AND a root ssh key was injected →
    advisory. Defensive: skip silently if layers can't be resolved.
    """
    authorized = container_dir / "kento-authorized-keys"
    try:
        if not authorized.is_file():
            return []
        ssh_user = _read_meta(container_dir, "kento-ssh-user") or "root"
        if ssh_user != "root":
            return []
        layers = _read_meta(container_dir, "kento-layers")
        if not layers:
            return []
        if not detect_cloudinit(layers):
            return []
    except (PermissionError, OSError):
        return []
    return [_finding(
        "cloudinit", "warn", display,
        f"SSH keys were injected for 'root' on a cloud-init image. Cloud "
        f"images typically disable root SSH login, so the key may not take "
        f"effect.",
        "recreate with --ssh-key-user <user> (e.g. 'debian') if root login "
        "does not work")]


# --- public API ----------------------------------------------------------


def run_diagnostics(name=None):
    """Run a read-only health/triage scan and return a structured report.

    name=None → host-wide scan of all instances (both namespaces) plus
    host-level checks. name=<instance> → host-level checks plus the
    instance checks for that one resolved instance (raises
    InstanceNotFoundError on a miss).

    Returns:
        {"checks": [<finding>, ...], "problem_count": int,
         "instances_scanned": int}
    where each finding is
        {"category", "severity" ("ok"|"info"|"warn"|"error"),
         "scope" ("host" | "<instance-name>"), "message", "remediation"}.

    Pure / read-only / silent. Per-check failures degrade to "info"
    findings rather than raising (resolution misses still raise).
    """
    instances = _enumerate_instances(name)

    checks = []

    # Host-level checks run regardless of name.
    checks.extend(_check_apparmor())
    checks.extend(_check_stale_holds())
    checks.extend(_check_vmid_health())

    for container_dir, mode, display in instances:
        try:
            running = is_running(container_dir, mode)
        except (PermissionError, OSError):
            running = False
            checks.append(_finding(
                "status", "info", display,
                "could not determine running state (needs root?)", None))
        checks.extend(_check_orphan(container_dir, mode, display))
        checks.extend(_check_portfwd(container_dir, display))
        checks.extend(_check_mounts(container_dir, mode, display, running))
        checks.extend(_check_network(container_dir, mode, display, running))
        checks.extend(_check_cloudinit(container_dir, display))

    problem_count = sum(1 for f in checks if f["severity"] in ("warn", "error"))

    return {
        "checks": checks,
        "problem_count": problem_count,
        "instances_scanned": len(instances),
    }


_SEVERITY_LABEL = {
    "ok": "OK",
    "info": "INFO",
    "warn": "WARN",
    "error": "ERROR",
}


def format_diagnostics(report):
    """Render a diagnostics report as a human-readable multi-line string.

    Host-level findings first, then per-instance. "ok" checks are summarized
    compactly; warn/error findings are shown in detail with remediation.
    Returns the joined string (the caller prints it).
    """
    checks = report.get("checks", [])
    problem_count = report.get("problem_count", 0)
    scanned = report.get("instances_scanned", 0)

    host = [f for f in checks if f["scope"] == "host"]
    # Stable per-instance grouping in first-seen order.
    inst_order = []
    by_inst = {}
    for f in checks:
        if f["scope"] == "host":
            continue
        if f["scope"] not in by_inst:
            by_inst[f["scope"]] = []
            inst_order.append(f["scope"])
        by_inst[f["scope"]].append(f)

    lines = []

    # Summary header.
    if problem_count == 0:
        lines.append(f"All clear — {len(checks)} checks, 0 problems "
                     f"({scanned} instance(s) scanned).")
    else:
        lines.append(f"{len(checks)} checks, {problem_count} problem(s) "
                     f"({scanned} instance(s) scanned).")

    def _emit_group(title, group):
        if not group:
            return
        problems = [f for f in group if f["severity"] in ("warn", "error")]
        oks = [f for f in group if f["severity"] == "ok"]
        infos = [f for f in group if f["severity"] == "info"]
        lines.append("")
        lines.append(title)
        for f in problems:
            lines.append(f"  [{_SEVERITY_LABEL[f['severity']]}] "
                         f"{f['category']}: {f['message']}")
            if f["remediation"]:
                lines.append(f"      fix: {f['remediation']}")
        for f in infos:
            lines.append(f"  [INFO] {f['category']}: {f['message']}")
        if oks:
            cats = ", ".join(sorted({f["category"] for f in oks}))
            lines.append(f"  [OK] {len(oks)} check(s) passed: {cats}")

    _emit_group("Host:", host)
    for scope in inst_order:
        _emit_group(f"Instance {scope}:", by_inst[scope])

    return "\n".join(lines)
