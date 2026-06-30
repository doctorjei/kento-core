"""Kento — compose OCI images into LXC system containers via overlayfs."""

import logging
import os
import pwd
import re
from pathlib import Path

try:
    from importlib.metadata import version as _pkg_version
    __version__ = _pkg_version("kento-core")
except Exception:
    __version__ = "unknown"

logging.getLogger("kento").addHandler(logging.NullHandler())
logger = logging.getLogger("kento")

from kento.errors import (  # noqa: F401  (public re-export)
    KentoError, ValidationError, InstanceNotFoundError, InstanceExistsError,
    ImageNotFoundError, ModeError, StateError, StopTimeout, SubprocessError,
)
from kento._references import (  # noqa: F401  (public re-export)
    MalformedReference, Endpoint, Digest, SourceReference, OciReference,
    UrlReference,
)
from kento._network import (  # noqa: F401  (public re-export)
    NetworkMode, ForwardProtocol, NetworkConnection,
    HostBinding, GuestTarget, ForwardAddressNotImplemented,
    parse_forward_spec, render_forward_spec, parse_forwards, parse_cidr,
)
from kento._diagnosis import (  # noqa: F401  (public re-export)
    DiagnosisDomain, CheckLevel, PruneScope, Finding, Diagnosis, ReclaimReport,
)
from kento._platform import (  # noqa: F401  (public re-export)
    PlatformMode, PlatformProfile, Status,
)
from kento._storage import (  # noqa: F401  (public re-export)
    StorageMode,
)
from kento._images import (  # noqa: F401  (public re-export)
    DiskFormat, Layer, Hold, ManagedStatus, ImageRecord, Image, LayeredImage,
    OciImage, VolumeImage, CompositeImage,
)
from kento._instances import (  # noqa: F401  (public re-export)
    Instance, SystemContainer, VirtualMachine,
)
from kento._result import (  # noqa: F401  (public re-export)
    Result, Ok, Warning, Error, Condition, Severity, ConditionKind,
    ResultError,
)

# Curated public surface. The source-reference value types are re-exported
# flat (canonical paths kento.OciReference etc.); the `_references` module is
# internal. Errors are re-exported from kento.errors. The remaining names are
# the long-standing module-level helpers defined below.
__all__ = [
    # exception hierarchy (kento.errors)
    "KentoError", "ValidationError", "InstanceNotFoundError",
    "InstanceExistsError", "ImageNotFoundError", "ModeError", "StateError",
    "StopTimeout", "SubprocessError",
    # source-reference value types (Block 01 — kento._references)
    "MalformedReference", "Endpoint", "Digest", "SourceReference",
    "OciReference", "UrlReference",
    # network value types (Block 02 — kento._network)
    "NetworkMode", "ForwardProtocol", "NetworkConnection",
    "HostBinding", "GuestTarget", "ForwardAddressNotImplemented",
    "parse_forward_spec", "render_forward_spec", "parse_forwards", "parse_cidr",
    # diagnosis & report value types (Block 04 — kento._diagnosis)
    "DiagnosisDomain", "CheckLevel", "PruneScope", "Finding", "Diagnosis",
    "ReclaimReport",
    # platform / lifecycle-status value types (Block 03 — kento._platform)
    "PlatformMode", "PlatformProfile", "Status",
    # root-storage value type (Block 03 — kento._storage)
    "StorageMode",
    # image family value types (Block 05 / SD1 — kento._images)
    "DiskFormat", "Layer", "Image", "LayeredImage", "OciImage", "VolumeImage",
    "CompositeImage",
    # hold pin value type (storage-depth SD2 — kento._images)
    "Hold",
    # typed managed-image ledger (storage-depth SD3, JC1 — kento._images)
    "ManagedStatus", "ImageRecord",
    # instance family handles (Block 08 — kento._instances)
    "Instance", "SystemContainer", "VirtualMachine",
    # Result value family (Block R1 — kento._result)
    "Result", "Ok", "Warning", "Error", "Condition", "Severity",
    "ConditionKind", "ResultError",
    # module-level helpers (defined in this module)
    "validate_name", "detect_bridge", "resolve_network", "read_mode",
    "require_root", "detect_mode", "upper_base", "sanitize_image_name",
    "next_instance_name", "pve_config_exists", "is_running",
    "resolve_container", "resolve_in_namespace", "resolve_any",
    "check_name_conflict", "LXC_BASE", "VM_BASE",
    # module-level diagnosis entry point (Block 10 — global host+images+instances)
    "diagnose",
    # module-level library version (Block 13, M26 — a property of the library)
    "version",
]

LXC_BASE = Path("/var/lib/lxc")
VM_BASE = Path("/var/lib/kento/vm")

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def validate_name(name: str, *, what: str = "instance name") -> None:
    """Reject names that would enable injection or path traversal.

    Accepts: ASCII alphanumerics plus `_`, `.`, `-`. Must start with
    alphanumeric. Max MAX_INSTANCE_NAME (64) chars -- the name becomes the
    guest hostname (HOST_NAME_MAX) and is the last otherwise-uncapped
    contributor to the overlay mount-options budget (see layers.py).
    Rejects: empty, whitespace, shell metacharacters, `/`, `..`, NUL.

    Raises ValidationError on rejection. what is used in the message for
    context (e.g. "instance name", "auto-generated name").
    """
    from kento.defaults import MAX_INSTANCE_NAME

    if not isinstance(name, str) or not name:
        raise ValidationError(f"{what} cannot be empty")
    if len(name) > MAX_INSTANCE_NAME:
        raise ValidationError(
            f"{what} {name!r} is {len(name)} characters; the maximum is "
            f"{MAX_INSTANCE_NAME} (it becomes the guest hostname and bounds "
            f"the overlay mount options)."
        )
    if "\x00" in name:
        raise ValidationError(f"{what} contains NUL byte: {name!r}")
    if not _NAME_RE.match(name):
        raise ValidationError(
            f"invalid {what}: {name!r}. Names must start with a letter "
            f"or digit and contain only [A-Za-z0-9_.-] "
            f"(max {MAX_INSTANCE_NAME} chars)."
        )


def _bridge_exists(name: str) -> bool:
    """Check if a network bridge interface exists."""
    return Path(f"/sys/class/net/{name}").is_dir()


def detect_bridge() -> str | None:
    """Detect the first available network bridge.

    Checks vmbr0 (PVE default), then lxcbr0 (LXC default).
    Returns the bridge name or None if no bridge found.
    """
    for name in ("vmbr0", "lxcbr0"):
        if _bridge_exists(name):
            return name
    return None


def resolve_network(net_type: str | None, bridge_name: str | None,
                    mode: str, port: str | None = None) -> dict:
    """Resolve network configuration for container/VM creation.

    Returns dict with keys: type, bridge, port
    - type: "bridge", "host", "usermode", or "none"
    - bridge: bridge name (str) or None
    - port: "host:guest" (str) or None
    """
    # Port implies usermode if no explicit network set (VM/PVE-VM only).
    # For LXC/PVE, port forwarding uses iptables DNAT which requires bridge.
    if port is not None and net_type is None:
        if mode in ("vm", "pve-vm"):
            net_type = "usermode"

    # Auto-detect if no network type specified
    if net_type is None:
        if mode == "vm":
            # Plain VM has no bridge support in start_vm (QEMU would need a tap
            # device). Auto-detecting bridge here silently produces a VM with no
            # network at all. Default to usermode instead; user can still pass
            # --network bridge=<name> explicitly (pve-vm handles bridge via qm).
            net_type = "usermode"
            logger.info("Network: using usermode networking (plain VM default)")
        else:
            bridge = detect_bridge()
            if bridge:
                net_type = "bridge"
                bridge_name = bridge
                logger.info("Network: using bridge %s", bridge)
            elif mode == "pve-vm":
                net_type = "usermode"
                logger.info("Network: no bridge found, using usermode networking")
            else:
                net_type = "none"
                logger.info("Network: no bridge found, networking disabled")
    elif net_type == "bridge" and bridge_name is None:
        # --network bridge without name: auto-detect bridge
        bridge_name = detect_bridge()
        if bridge_name is None:
            raise ValidationError(
                "--network bridge specified but no bridge interface found "
                "(checked vmbr0, lxcbr0)"
            )
        logger.info("Network: using bridge %s", bridge_name)

    return {
        "type": net_type,
        "bridge": bridge_name,
        "port": port,
    }


def read_mode(container_dir: Path, default: str = "lxc") -> str:
    """Read the kento-mode file from a container directory."""
    mode_file = container_dir / "kento-mode"
    return mode_file.read_text().strip() if mode_file.is_file() else default


def require_root() -> None:
    if os.getuid() != 0:
        raise StateError("must run as root. Re-run with sudo (e.g. 'sudo kento ...').")


def detect_mode(force: str | None = None) -> str:
    """Return 'pve', 'lxc', or 'vm' based on environment or explicit override.

    When force is set (e.g. 'vm'), returns it directly.
    Otherwise auto-detects PVE vs plain LXC (VM is never auto-detected).
    """
    if force:
        return force
    from kento.pve import is_pve
    return "pve" if is_pve() else "lxc"


def upper_base(name: str, base: Path | None = None) -> Path:
    """Return the base directory for a container's upper and work dirs.

    Resolution order:
    1. If ``KENTO_STATE_DIR`` is set and non-empty, use it as the base
       (``~`` is expanded). Takes precedence over sudo/root detection.
       Useful when the default location sits on an overlayfs (e.g.
       nested-LXC rootfs), which the kernel refuses as an upperdir.
    2. When run via sudo, uses the invoking user's XDG data directory
       (~user/.local/share/kento/<name>/) so writable state is per-user.
    3. When run as root directly, uses the provided base (or LXC_BASE)/<name>/.
    """
    override = os.environ.get("KENTO_STATE_DIR")
    if override:
        if override.startswith("~"):
            override = os.path.expanduser(override)
        return Path(override) / name
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            home = Path(pwd.getpwnam(sudo_user).pw_dir)
        except KeyError:
            raise StateError(
                f"SUDO_USER={sudo_user!r} is not a known user; "
                f"set KENTO_STATE_DIR or run directly as root."
            )
        return home / ".local" / "share" / "kento" / name
    return (base or LXC_BASE) / name


def sanitize_image_name(image: str) -> str:
    """Convert an OCI image reference to a filesystem-safe name.

    Substitution order: '-' → '--',  '/' → '-',  '_' → '__',  ':' → '_'

    The transformation is injective for typical OCI image references but not
    bijective in the general case — adjacent '_:' and ':_' sequences produce
    collisions (e.g. 'a_:b' and 'a:_b' both map to 'a___b').
    """
    s = image.replace("-", "--")
    s = s.replace("/", "-")
    s = s.replace("_", "__")
    s = s.replace(":", "_")
    return s


def next_instance_name(base_name: str, scan_dir: Path,
                       other_dir: Path | None = None) -> str:
    """Return the next available auto-generated instance name.

    Appends -0, -1, -2, ... to base_name until an unused name is found.
    Checks both directory names and kento-name files in scan_dir.
    When other_dir is provided, also checks that directory for name conflicts
    so that auto-generated names are unique across both namespaces.
    """
    used_names: set[str] = set()
    for d_root in (scan_dir, other_dir):
        if d_root is not None and d_root.is_dir():
            for d in d_root.iterdir():
                if d.is_dir():
                    used_names.add(d.name)
                    name_file = d / "kento-name"
                    if name_file.is_file():
                        used_names.add(name_file.read_text().strip())
    n = 0
    while True:
        candidate = f"{base_name}-{n}"
        if candidate not in used_names:
            return candidate
        n += 1


def pve_config_exists(vmid: str, mode: str) -> bool:
    """Return whether the PVE config file for vmid/mode exists on this node.

    A missing config means the instance is GONE (destroyed/lost out-of-band),
    leaving kento's state dir orphaned. Callers use this to distinguish that
    case from a transient status-query failure.

    Path construction mirrors delete_qm_config / delete_pve_config in pve.py:
      - pve-vm: PVE_DIR/nodes/<node>/qemu-server/<vmid>.conf
      - pve:    PVE_DIR/nodes/<node>/lxc/<vmid>.conf

    Defensive: if the node name can't be resolved (no /etc/pve/local and no
    hostname), fall back to True so callers keep their existing behavior
    rather than crashing or wrongly declaring an instance gone.
    """
    from kento.pve import PVE_DIR, _pve_node_name
    try:
        node = _pve_node_name()
    except Exception:
        return True
    subdir = "qemu-server" if mode == "pve-vm" else "lxc"
    conf_path = PVE_DIR / "nodes" / node / subdir / f"{vmid}.conf"
    return conf_path.is_file()


def is_running(container_dir: Path, mode: str) -> bool:
    """Check if a container is running, using the mode-appropriate method.

    For PVE modes (pve, pve-vm) we wrap the status query with a 5-second
    timeout. An unreachable PVE node or hung pmxcfs would otherwise make
    `kento stop` hang indefinitely. On timeout or non-zero rc we ASSUME
    RUNNING (return True) — skipping a stop on a still-running instance
    leaks state, so the conservative choice is to attempt the stop.

    The cost of that conservatism is that a stop may then be issued on an
    instance that is in fact already stopped (the status query merely
    failed). stop.py's PVE/pve-vm shutdown path tolerates this: it issues
    the pct/qm shutdown non-fatally and treats a "not running" result as
    "Already stopped" rather than hard-exiting. (A missing PVE config is
    handled separately above as not-running, since that means the instance
    is gone, not merely unreachable.)
    """
    import subprocess
    if mode == "vm":
        from kento.vm import is_vm_running
        return is_vm_running(container_dir)
    elif mode == "pve-vm":
        vmid_file = container_dir / "kento-vmid"
        if not vmid_file.is_file():
            return False
        vmid = vmid_file.read_text().strip()
        # A missing PVE config means the instance is GONE (destroyed
        # out-of-band), leaving our state dir orphaned. Treat as not-running
        # so `stop` no-ops and `destroy -f` skips the stop. Only the
        # config-PRESENT, status-failed case is a transient "assume running".
        if not pve_config_exists(vmid, "pve-vm"):
            return False
        try:
            result = subprocess.run(
                ["qm", "status", vmid],
                capture_output=True, text=True, timeout=5,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "qm status timed out; assuming instance may be running"
            )
            return True
        if result.returncode != 0:
            logger.warning(
                "qm status returned non-zero; assuming instance may be running"
            )
            return True
        return "running" in result.stdout
    elif mode == "pve":
        # Missing PVE config => instance gone (see pve-vm branch above).
        if not pve_config_exists(container_dir.name, "pve"):
            return False
        try:
            result = subprocess.run(
                ["pct", "status", container_dir.name],
                capture_output=True, text=True, timeout=5,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "pct status timed out; assuming instance may be running"
            )
            return True
        if result.returncode != 0:
            logger.warning(
                "pct status returned non-zero; assuming instance may be running"
            )
            return True
        return "running" in result.stdout
    else:
        result = subprocess.run(
            ["lxc-info", "-n", container_dir.name, "-sH"],
            capture_output=True, text=True,
        )
        return result.returncode == 0 and "RUNNING" in result.stdout


def resolve_container(name: str, scan_dir: Path | None = None) -> Path:
    """Resolve a container name to its directory path.

    For LXC mode, the name IS the directory name (fast path).
    For PVE mode, scans kento-name files to find the matching directory.
    When scan_dir is None, searches both LXC_BASE and VM_BASE.
    Returns the container directory path, or exits with error if not found.
    """
    validate_name(name)
    bases = [scan_dir] if scan_dir else [LXC_BASE, VM_BASE]

    for base in bases:
        # Fast path: directory name matches
        direct = base / name
        if direct.is_dir() and (direct / "kento-image").is_file():
            return direct

        # Scan kento-name files
        if base.is_dir():
            for d in sorted(base.iterdir()):
                if not d.is_dir():
                    continue
                name_file = d / "kento-name"
                if name_file.is_file() and name_file.read_text().strip() == name:
                    if (d / "kento-image").is_file():
                        return d

    raise InstanceNotFoundError(
        f"no instance named '{name}'. "
        f"Run 'kento list' to see available instances."
    )


def _scan_namespace(name: str, base: Path) -> Path | None:
    """Scan a single base directory for a container/VM by name.

    Returns the directory path if found, None otherwise.
    """
    # Fast path: directory name matches
    direct = base / name
    if direct.is_dir() and (direct / "kento-image").is_file():
        return direct

    # Scan kento-name files
    if base.is_dir():
        for d in sorted(base.iterdir()):
            if not d.is_dir():
                continue
            name_file = d / "kento-name"
            if name_file.is_file() and name_file.read_text().strip() == name:
                if (d / "kento-image").is_file():
                    return d
    return None


def resolve_in_namespace(name: str, namespace: str) -> Path:
    """Resolve a name within a specific namespace ('lxc'/'container' or 'vm').

    Searches only LXC_BASE (for 'lxc'/'container') or VM_BASE (for 'vm').
    Exits with error if not found.
    """
    validate_name(name)
    base = LXC_BASE if namespace in ("container", "lxc") else VM_BASE
    result = _scan_namespace(name, base)
    if result is not None:
        return result
    list_cmd = "kento vm list" if namespace == "vm" else "kento lxc list"
    raise InstanceNotFoundError(
        f"no {namespace} named '{name}'. "
        f"Run '{list_cmd}' to see available instances."
    )


def resolve_any(name: str, namespace: str | None = None) -> tuple[Path, str]:
    """Resolve a name, optionally constrained to a single namespace.

    Returns (container_dir, mode) where mode is read from the kento-mode file.

    When ``namespace`` is 'lxc'/'container' or 'vm', the search is confined to
    that namespace's base directory (mirroring resolve_in_namespace): there is
    no cross-namespace ambiguity check, and a miss exits with a branded
    "instance not found" error. This is how callers honor an explicit
    ``kento lxc <cmd>`` / ``kento vm <cmd>`` scope so duplicate names created
    via ``create --force`` can be disambiguated.

    When ``namespace`` is None (the default — unchanged from prior behavior),
    both namespaces are searched and an ambiguous name (present in both) exits
    with an error directing the user to pick a scope.
    """
    validate_name(name)

    if namespace in ("container", "lxc"):
        hit = _scan_namespace(name, LXC_BASE)
        if hit is not None:
            return hit, read_mode(hit)
        raise InstanceNotFoundError(
            f"no lxc named '{name}'. "
            f"Run 'kento lxc list' to see available instances."
        )
    if namespace == "vm":
        hit = _scan_namespace(name, VM_BASE)
        if hit is not None:
            return hit, read_mode(hit, "vm")
        raise InstanceNotFoundError(
            f"no vm named '{name}'. "
            f"Run 'kento vm list' to see available instances."
        )

    lxc_hit = _scan_namespace(name, LXC_BASE)
    vm_hit = _scan_namespace(name, VM_BASE)

    if lxc_hit and vm_hit:
        raise KentoError(
            f"ambiguous name '{name}' — exists as both LXC and VM "
            f"instance. Use 'kento lxc <cmd>' or 'kento vm <cmd>'."
        )

    if lxc_hit:
        return lxc_hit, read_mode(lxc_hit)

    if vm_hit:
        return vm_hit, read_mode(vm_hit, "vm")

    raise InstanceNotFoundError(
        f"no instance named '{name}'. "
        f"Run 'kento list' to see available instances."
    )


def check_name_conflict(name: str, target_namespace: str) -> bool:
    """Check if a name already exists in the OTHER namespace.

    Returns True if a conflict exists, False otherwise.
    Does not error — the caller decides what to do.
    """
    validate_name(name)
    if target_namespace in ("container", "lxc"):
        other_base = VM_BASE
    else:
        other_base = LXC_BASE
    return _scan_namespace(name, other_base) is not None


# --------------------------------------------------------------------------- #
# Module-level diagnosis entry point — ``kento.diagnose()`` (Block 10, §11.8 D3).
#
# The global, host-wide diagnostic op: HOST checks + every image + every
# instance (both namespaces), mirroring the future ``kento.version()`` — a
# property of the library, not of a single handle. It is what ``kento diagnose``
# (no name) maps to. Companions ``instance.diagnose()`` / ``image.diagnose()``
# narrow to one domain/subject; this returns ALL findings (all three domains).
#
# THE NAME-COLLISION FOOT-GUN (gate C) — and why the import order below matters.
# There is a sibling SUBMODULE ``kento/diagnose.py`` (the procedural runtime;
# the CLI does ``from kento.diagnose import run_diagnostics``). On first import,
# Python binds the submodule as the parent package's ``diagnose`` attribute.
# Defining a top-level ``def diagnose`` here would normally be CLOBBERED right
# back by any later ``from kento.diagnose import ...`` that triggers a FRESH
# import of the submodule (a fresh import re-sets the parent attr to the module).
#
# Resolution (verified against real CPython, both import orders): import the
# submodule into ``sys.modules`` FIRST (the line below), THEN bind the function.
# Once the submodule is cached in ``sys.modules``, a subsequent
# ``from kento.diagnose import run_diagnostics`` finds the cached module and does
# NOT re-run the import machinery, so it does NOT re-set ``kento.diagnose`` — the
# function binding survives. ``kento.diagnose`` is the function; ``kento.diagnose
# .run_diagnostics`` still resolves through the cached submodule. The regression
# test (test_diagnose_module / test_instances) pins BOTH orders.
# --------------------------------------------------------------------------- #
from kento import diagnose as _diagnose_submodule  # noqa: E402,F401  (cache it FIRST)


def diagnose(name: str | None = None) -> "Diagnosis":  # noqa: F821  (Diagnosis re-exported above)
    """Run the host-wide (or named) diagnostic scan (§11.8 D3 b).

    Runs the existing ``kento.diagnose.run_diagnostics(name)`` and projects ALL
    flat findings — UNFILTERED — into a typed :class:`Diagnosis`. Two modes,
    distinguished only by the optional ``name``:

    * ``name=None`` (default) — the whole-host scan: HOST pre-flight checks +
      every image + every instance across both namespaces (all three domains —
      INSTANCE / IMAGE / HOST), read-only / silent (it REPORTS, never reaps).
      This is what ``kento diagnose`` (no instance argument) maps to.
    * ``name=<str>`` — the same scan narrowed by ``run_diagnostics`` to the HOST
      checks plus the ONE resolved instance's checks (raising
      :class:`InstanceNotFoundError` on a miss, propagated unchanged). The
      findings are projected UNFILTERED, preserving today's named-``diagnose``
      wire (host findings + that instance's findings). This is DELIBERATELY
      different from ``instance.diagnose()`` (M11), which filters to the
      INSTANCE domain + itself and drops the host findings; the module-level
      function is the legacy named-wire preserver, the handle method is the
      narrowed per-instance view.

    The module-level companion to ``instance.diagnose()`` (one instance's
    INSTANCE findings) and ``image.diagnose()`` (the IMAGE domain); it mirrors
    the future ``kento.version()`` as a property of the library.

    Performs I/O (the scan) via an explicit module-level call (§2 principle 2);
    the returned ``Diagnosis`` is an inert value type.
    """
    from kento._diagnosis import diagnosis_from_report

    report = _diagnose_submodule.run_diagnostics(name)
    return diagnosis_from_report(report)


def version() -> str:
    """Return the installed ``kento-core`` version string (§11.7 M26).

    A module-level function (NOT a method — it is a property of the library, not
    of any one instance, mirroring ``kento.diagnose()``). Minimal by design: it
    returns the ``__version__`` resolved at import from ``importlib.metadata``
    (the installed distribution's version), or ``"unknown"`` if the package
    metadata is unavailable (e.g. running straight from a source tree without an
    installed dist). A richer capability/feature-flag struct can come later,
    non-breakingly, if a consumer needs one (§11.7).
    """
    return __version__
