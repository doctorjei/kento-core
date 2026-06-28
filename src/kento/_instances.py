"""The ``Instance`` family — typed handles over kento-managed instances.

An ``Instance`` is a **coherent cached snapshot** of one kento instance (§10.2):
its fields are loaded once from the persisted ``kento-*`` state files and the
live status probe, typed with the Phase-1/2 value types (``SourceReference`` /
``NetworkConnection`` / ``PlatformProfile`` / ``Status`` / ``StorageMode`` / the
``forwards`` map). Properties return those cached values — a property NEVER
performs I/O (§2 principle 2); ``refresh()`` re-reads the whole snapshot.

Two layers, two rules (§2 principle 2). The §11.0 fields are inert cached data.
I/O happens only at named moments — the ``get``/``list`` classmethods that
eager-load a snapshot, and ``refresh()`` that re-reads it. This block is
**READ-ONLY**: lifecycle mutation (``start``/``stop``/``set``/...) is later
blocks; ``create``/``transient`` are the abstract contract only (Phase 4 builds
the bodies).

ADDITIVE (Phase 3): this module READS the ``kento-*`` keys and WRAPS the
existing runtime functions (``kento.resolve_any`` / ``resolve_in_namespace`` /
``read_mode`` / ``is_running`` / ``pve_config_exists`` and the ``LXC_BASE`` /
``VM_BASE`` enumeration). It does NOT modify ``create.py`` / ``info.py`` /
``list.py`` / the lifecycle modules / the CLI — that live re-point is Phase 6.

The public surface (``Instance``, ``SystemContainer``, ``VirtualMachine``) is
re-exported flat from ``kento`` — refer to ``kento.SystemContainer``, not
``kento._instances.SystemContainer``.

Spec: ``~/workspace/kento-core-api-design.md`` §2, §10.1/§10.2 (entry points +
handle=snapshot+refresh), §11.0 (the base field set, incl. the † hostname
back-fill and the ‡ apparmor notes), §11.1 (M1 get / M2 list), §11.2 M10
refresh; §6 (PlatformProfile) and §7 (Status).
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from kento._network import (
    NetworkConnection,
    NetworkMode,
    parse_cidr,
    parse_forward_spec,
)
from kento._platform import PlatformMode, PlatformProfile, Status
from kento._references import OciReference, SourceReference
from kento._storage import StorageMode
from kento.errors import InstanceNotFoundError, KentoError

if TYPE_CHECKING:
    from kento._network import GuestTarget, HostBinding

_instances_logger = logging.getLogger("kento")

__all__ = [
    "Instance",
    "SystemContainer",
    "VirtualMachine",
]


# --------------------------------------------------------------------------- #
# Mode <-> family / platform mapping.
#
# The persisted ``kento-mode`` is one of the legacy flat four: ``lxc`` / ``pve``
# (pve-lxc, stored bare as "pve") / ``vm`` / ``pve-vm``. The typed model splits
# that flat string into TWO orthogonal axes (§6, §10.1):
#   * BACKEND/WORKLOAD -> the concrete CLASS (SystemContainer / VirtualMachine).
#   * PLATFORM         -> PlatformProfile.mode (STANDARD / PVE).
# This is the inverse of the legacy ``{SystemContainer, VirtualMachine} x
# {STANDARD, PVE}`` flattening (§6).
# --------------------------------------------------------------------------- #

# VM family modes (everything else is the LXC/SystemContainer family). Mirrors
# the ``mode in ("vm", "pve-vm")`` family split info.py / list.py use.
_VM_MODES = frozenset({"vm", "pve-vm"})
# PVE-platform modes (pve-lxc is stored bare as "pve"; §6.3).
_PVE_MODES = frozenset({"pve", "pve-vm"})


def _is_vm_mode(mode: str) -> bool:
    """True iff ``mode`` is a VM-family mode (vm / pve-vm)."""
    return mode in _VM_MODES


# --------------------------------------------------------------------------- #
# kento-net-type (bridge/host/usermode/none) -> NetworkMode.
#
# The persisted ``kento-net-type`` is the create-resolved transport string
# (``resolve_network``: bridge/host/usermode/none), NOT the typed NetworkMode
# value. We map it faithfully: a BRIDGE attachment is DHCP unless a static
# kento-net (ip=) is present, in which case it is STATIC (§5.1/§5.5). host ->
# HOST, usermode -> USER, none -> DISABLED. An unrecognized string falls back to
# DISABLED with a log (total — one odd value must not blow up list()).
# --------------------------------------------------------------------------- #

_NET_TYPE_TO_MODE = {
    "host": NetworkMode.HOST,
    "usermode": NetworkMode.USER,
    "none": NetworkMode.DISABLED,
}


# --------------------------------------------------------------------------- #
# Instance — the abstract base (§11.0 field set).
# --------------------------------------------------------------------------- #


class Instance(ABC):
    """A coherent cached snapshot of one kento-managed instance (§10.2, §11.0).

    The base holds the shared §11.0 field set, each typed with a Phase-1/2 value
    type and backed by a persisted ``kento-*`` key (so ``get``/``list``/
    ``refresh`` load it faithfully — a property reflects recoverable state,
    never a guess, §2 principle 2):

    ``name`` / ``hostname`` / ``sources`` / ``storage`` / ``network`` /
    ``forwards`` / ``status`` / ``resources`` / ``platform_profile`` /
    ``nesting`` / ``created`` / ``environment``.

    The fields are **cached snapshot data** read once at construction. Reading a
    field returns the cached value (cheap, internally consistent across fields);
    it does NOT re-query the backend (re-query-on-every-access was considered and
    rejected, §10.2). ``refresh()`` (M10) re-reads the whole snapshot from the
    source of truth (state files + status probe).

    Concrete kinds (``SystemContainer`` / ``VirtualMachine``) derive from this
    base; ``Instance`` is **abstract and never instantiated directly** — the
    ``create``/``transient`` classmethods are abstract here (§10.1), so calling
    ``Instance()`` / ``Instance.create(...)`` is impossible. Read entry points
    (``get``/``list``) are polymorphic: on the base they return whichever kind a
    name is / all kinds; on a subclass they narrow to that kind (§10.1).

    This block is READ-ONLY (Phase 3): no lifecycle mutation, no settable
    properties (those are later blocks). The handle wraps the existing runtime
    read functions additively — it does not touch their live callers (Phase 6).
    """

    # The private source-of-truth pointers a refresh re-reads from. Set by the
    # snapshot loader; NOT part of the public typed surface. ``_dir`` is the
    # on-disk container directory; ``_mode`` is the raw persisted kento-mode
    # ("lxc"/"pve"/"vm"/"pve-vm") — kept verbatim so the (test-patchable)
    # runtime ``is_running``/``pve_config_exists`` get exactly the mode string
    # they expect (info.py / list.py pass the raw mode, not the normalized one).
    _dir: Path
    _mode: str

    # Cached snapshot fields (§11.0). Plain cached attributes set by the loader:
    # access is variable-like (``inst.status``) and reads return the cached
    # value with no I/O (§10.2). Settable PROPERTIES (M9) are a LATER block and
    # convert specific fields non-breakingly; this read-only block does not
    # pre-empt that shape.
    name: str
    hostname: str
    sources: tuple[SourceReference, ...]
    storage: StorageMode
    network: NetworkConnection
    forwards: dict["HostBinding", "GuestTarget"]
    status: Status
    resources: dict[str, int]
    platform_profile: PlatformProfile
    nesting: bool
    created: datetime
    environment: dict[str, str]

    # ------------------------------------------------------------------- #
    # create / transient — the abstract contract (§10.1, §11.0).
    #
    # Declared abstract on the base so (a) every concrete kind MUST provide
    # them and (b) the base stays genuinely uninstantiable. Bodies land in
    # Phase 4; the concrete kinds here override with a NotImplementedError stub
    # so they are themselves instantiable FROM A SNAPSHOT now (§11.0 — resolve
    # the abstract-create-vs-instantiable tension by overriding, see the
    # subclasses). Signatures stay open (``*args, **kwargs``) because the real
    # parameter lists (M15/M16) are Phase 4 — pinning them here would invent
    # surface this block does not own.
    # ------------------------------------------------------------------- #
    @classmethod
    @abstractmethod
    def create(cls, *args, **kwargs) -> "Instance":
        """Create a new instance (contract only; bodies are Phase 4, §10.1).

        Abstract on the base so the base stays uninstantiable and every concrete
        kind MUST provide it; the concrete kinds add their own params (M15/M16).
        The body RAISES rather than silently returning ``None`` — Python lets you
        *call* a classmethod marked ``abstractmethod`` directly on the abstract
        class (``abstractmethod`` only blocks *instantiation*), so ``Instance``'s
        own create must fail loudly rather than return a non-``Instance`` (gate C;
        §10.1 "calling ``Instance.create()`` hits the abstract body and raises").
        """
        raise NotImplementedError(
            "Instance is abstract — call SystemContainer.create(...) or "
            "VirtualMachine.create(...); you cannot create an abstract Instance."
        )

    @classmethod
    @abstractmethod
    def transient(cls, *args, **kwargs) -> "AbstractContextManager[Instance]":
        """Context-manager create for a throwaway instance (contract; Phase 4).

        Abstract on the base (mirrors ``create``); the body RAISES for the same
        reason ``create`` does. The concrete kinds build the ``with``-scoped,
        guaranteed-torn-down handle in Phase 4 (M27).
        """
        raise NotImplementedError(
            "Instance is abstract — call SystemContainer.transient(...) or "
            "VirtualMachine.transient(...); you cannot create an abstract "
            "Instance."
        )

    # ------------------------------------------------------------------- #
    # M1 — get: resolve one name to the right concrete handle (§11.1).
    # ------------------------------------------------------------------- #
    @classmethod
    def get(cls, name: str) -> "Instance":
        """Resolve one instance name to its typed handle (M1, §11.1).

        Scans both namespaces (LXC + VM) via the existing runtime resolver.
        Returns the CONCRETE kind for ``name`` (``SystemContainer`` /
        ``VirtualMachine``), loaded as a coherent snapshot.

        Polymorphic (§10.1): called on the **base** it returns whichever kind
        ``name`` is; called on a **subclass** it NARROWS — resolving a name that
        is a DIFFERENT kind raises a kind-mismatch ``InstanceNotFoundError``
        whose message names the actual kind (NOT a ``None`` return, §2 principle
        5). Raises ``InstanceNotFoundError`` when no such instance exists.
        """
        from kento import resolve_any

        container_dir, mode = resolve_any(name)
        inst = _load_snapshot(container_dir, mode)
        cls._reject_kind_mismatch(inst, name)
        return inst

    @classmethod
    def _reject_kind_mismatch(cls, inst: "Instance", name: str) -> None:
        """Raise if ``inst`` is not an instance of the calling class (§10.1).

        On the base ``Instance`` this is a no-op (every concrete kind passes
        ``isinstance``). On a subclass it enforces the narrowing: a name that
        resolves to a different kind raises ``InstanceNotFoundError`` naming the
        actual kind, rather than silently returning the wrong-typed handle.
        """
        if not isinstance(inst, cls):
            raise InstanceNotFoundError(
                f"no {cls.__name__} named {name!r}: it is a "
                f"{type(inst).__name__}. Use {type(inst).__name__}.get({name!r})"
                f" or Instance.get({name!r})."
            )

    # ------------------------------------------------------------------- #
    # M2 — list: enumerate cls's kind across both namespaces (§11.1).
    # ------------------------------------------------------------------- #
    @classmethod
    def list(cls) -> "list[Instance]":
        """Enumerate instances of ``cls``'s kind across both namespaces (M2).

        Globs ``*/kento-image`` in ``LXC_BASE`` and ``VM_BASE`` (the same
        enumeration source ``list.py`` uses) and loads each as a snapshot.
        Polymorphic (§10.1): the **base** returns ALL kinds; a **subclass**
        NARROWS to its own kind (``VirtualMachine.list()`` -> only VMs). No
        filter params in 1.0 (callers filter the typed list, §11.9).

        TOTAL OVER THE STORE: a corrupt / mid-destroy / unresolvable entry is
        SKIPPED WITH A LOG, never fatal to the whole listing — one bad instance
        must not hide every healthy one (mirrors ``list.py``'s per-entry
        ``except OSError: continue`` and the ``Status.UNKNOWN`` totality
        rationale, §7.2). The status probe itself is already total (a failed
        probe yields ``Status.UNKNOWN``, not an exception).
        """
        from kento import LXC_BASE, VM_BASE

        instances: list[Instance] = []
        for base in (LXC_BASE, VM_BASE):
            if not base.is_dir():
                continue
            for image_file in sorted(
                base.glob("*/kento-image"), key=lambda f: f.parent.name
            ):
                container_dir = image_file.parent
                try:
                    inst = _load_snapshot(container_dir, _read_raw_mode(container_dir))
                except (OSError, KentoError) as exc:
                    # Total over the store: skip+log a corrupt/raced entry.
                    _instances_logger.warning(
                        "skipping unreadable instance %s: %s",
                        container_dir.name, exc,
                    )
                    continue
                if isinstance(inst, cls):
                    instances.append(inst)
        return instances

    # ------------------------------------------------------------------- #
    # M10 — refresh: re-read this handle's full snapshot from source (§11.2).
    # ------------------------------------------------------------------- #
    def refresh(self) -> None:
        """Re-read the full snapshot from the source of truth (M10, §10.2).

        Re-loads every §11.0 field (state files + the live status probe) from
        this handle's container directory, replacing the cached values in place.
        The handle identity is unchanged; only the cached snapshot is updated —
        the conventional ``.reload()`` / ``.refresh_from_db()`` pattern (§10.2).
        """
        fresh = _load_snapshot(self._dir, self._mode)
        # Copy the fresh snapshot's cached state into THIS handle (in place), so
        # any existing reference observes the updated values. _populate is the
        # single field-set used by both the loader and refresh.
        self.__dict__.update(fresh.__dict__)


# --------------------------------------------------------------------------- #
# SystemContainer — the LXC backend (§11.0).
# --------------------------------------------------------------------------- #


class SystemContainer(Instance):
    """An LXC system container — full-init backend (§11.0).

    Adds the LXC backend-specific cached fields to the base §11.0 set:

    * ``unprivileged`` — ``kento-unprivileged``; create-time (immutable).
    * ``lxc_args`` — ``kento-lxc-args`` (``--lxc-arg``); settable in a later
      block (read-only here).

    ``nesting`` lives on the BASE (run 30). ``apparmor`` is intentionally NOT a
    field — it is the ambient ``KENTO_APPARMOR_PROFILE`` env hatch, already
    inspectable via ``diagnose`` (§11.0 ‡).
    """

    unprivileged: bool
    lxc_args: tuple[str, ...]

    @classmethod
    def create(cls, *args, **kwargs) -> "SystemContainer":
        """Create a new LXC system container — NOT YET BUILT (Phase 4, M15).

        Overrides the abstract base contract so ``SystemContainer`` is a
        concrete, instantiable class (``get``/``list`` build it from a
        snapshot). The actual create body — with the M15 parameter list — lands
        in Phase 4; calling it now raises rather than pretending.
        """
        raise NotImplementedError(
            "SystemContainer.create is built in Phase 4 (M15); this Phase-3 "
            "block is read-only (get/list/refresh)."
        )

    @classmethod
    def transient(cls, *args, **kwargs) -> "AbstractContextManager[SystemContainer]":
        """Context-manager create — NOT YET BUILT (Phase 4, M27)."""
        raise NotImplementedError(
            "SystemContainer.transient is built in Phase 4 (M27); this Phase-3 "
            "block is read-only (get/list/refresh)."
        )


# --------------------------------------------------------------------------- #
# VirtualMachine — the QEMU/KVM backend (§11.0).
# --------------------------------------------------------------------------- #


class VirtualMachine(Instance):
    """A QEMU/KVM virtual machine — full-system backend (§11.0).

    Adds the VM backend-specific cached field to the base §11.0 set:

    * ``qemu_args`` — ``kento-qemu-args`` (``--qemu-arg``); settable in a later
      block (read-only here).

    ``nesting`` lives on the BASE (run 30; VM nested-virt CPU features).
    ``kernel``/``initramfs``/``machine`` are image-contract constants, NOT fields
    (M16, §11.0).
    """

    qemu_args: tuple[str, ...]

    @classmethod
    def create(cls, *args, **kwargs) -> "VirtualMachine":
        """Create a new VM — NOT YET BUILT (Phase 4, M16).

        Overrides the abstract base contract so ``VirtualMachine`` is a
        concrete, instantiable class (``get``/``list`` build it from a
        snapshot). The actual create body — with the M16 parameter list — lands
        in Phase 4; calling it now raises rather than pretending.
        """
        raise NotImplementedError(
            "VirtualMachine.create is built in Phase 4 (M16); this Phase-3 "
            "block is read-only (get/list/refresh)."
        )

    @classmethod
    def transient(cls, *args, **kwargs) -> "AbstractContextManager[VirtualMachine]":
        """Context-manager create — NOT YET BUILT (Phase 4, M27)."""
        raise NotImplementedError(
            "VirtualMachine.transient is built in Phase 4 (M27); this Phase-3 "
            "block is read-only (get/list/refresh)."
        )


# --------------------------------------------------------------------------- #
# The snapshot loader — read kento-* keys into one coherent typed snapshot.
#
# A single point where a container directory + raw mode becomes a fully-typed
# Instance handle. WRAPS the existing read primitives (the kento-* file reads,
# is_running, pve_config_exists). ADDITIVE — does not modify info.py/list.py.
# --------------------------------------------------------------------------- #


def _read_meta(container_dir: Path, filename: str) -> str | None:
    """Read a ``kento-*`` metadata file; return stripped content or None.

    Same shape as ``info._read_meta`` (a one-line read used pervasively in the
    runtime); re-stated here so the loader has no import dependency on info.py
    and stays purely additive.
    """
    f = container_dir / filename
    return f.read_text().strip() if f.is_file() else None


def _read_raw_mode(container_dir: Path) -> str:
    """Read the raw persisted ``kento-mode`` (default 'lxc'), via the runtime.

    Wraps ``kento.read_mode`` — the same source ``list.py`` uses — so the loader
    sees exactly the runtime's mode string ("lxc"/"pve"/"vm"/"pve-vm").
    """
    from kento import read_mode

    return read_mode(container_dir)


def _load_snapshot(container_dir: Path, mode: str) -> Instance:
    """Load one coherent typed snapshot from ``container_dir`` (§10.2, §11.0).

    Reads the persisted ``kento-*`` keys + the live status probe ONCE and builds
    the concrete ``Instance`` (``SystemContainer`` / ``VirtualMachine`` chosen
    by ``mode``), with every §11.0 field typed via the landed value types. WRAPS
    the existing read primitives; reimplements none of their logic. ``mode`` is
    the RAW persisted kento-mode (kept verbatim for the runtime probes).
    """
    cls = VirtualMachine if _is_vm_mode(mode) else SystemContainer
    inst = cls.__new__(cls)
    inst._dir = container_dir
    inst._mode = mode

    name = _read_meta(container_dir, "kento-name") or container_dir.name
    inst.name = name
    # † hostname: load the hostname key, fallback to name. The create-WRITE
    # back-fill is Phase 6 (a live-path change); the read-fallback is correct
    # now — a pre-back-fill instance has no hostname key, so name is the honest
    # value (§11.0 †).
    inst.hostname = _read_meta(container_dir, "hostname") or name
    inst.sources = _load_sources(container_dir)
    inst.storage = _load_storage(container_dir)
    inst.network = _load_network(container_dir)
    inst.forwards = _load_forwards(container_dir)
    inst.status = _resolve_status(container_dir, mode)
    inst.resources = _load_resources(container_dir)
    inst.platform_profile = _load_platform_profile(container_dir, mode)
    inst.nesting = (_read_meta(container_dir, "kento-nesting") == "1")
    inst.created = _load_created(container_dir)
    inst.environment = _load_environment(container_dir)

    # Subclass-specific fields.
    if isinstance(inst, SystemContainer):
        inst.unprivileged = (_read_meta(container_dir, "kento-unprivileged") == "1")
        inst.lxc_args = _load_passthrough(container_dir, "kento-lxc-args")
    else:  # VirtualMachine
        inst.qemu_args = _load_passthrough(container_dir, "kento-qemu-args")

    return inst


def _load_sources(container_dir: Path) -> tuple[SourceReference, ...]:
    """Build the ``sources`` tuple from ``kento-image`` (§11.0, §3.8/§4).

    1.0 is a single ``oci://`` boot source, so this is a 1-element tuple of one
    ``OciReference`` parsed from the recorded ``kento-image`` ref. The image ref
    is parsed FAITHFULLY (``OciReference.parse`` — §2 principle 3, never re-split
    by hand). An absent ``kento-image`` would mean the dir is not a kento
    instance, which the enumeration/resolution already excludes (every kento dir
    has a ``kento-image``); if it is genuinely missing here we yield an empty
    tuple rather than fabricate a ref. ``kento-image-id`` is the resolved content
    pin, surfaced via the ``Image`` family (§4), not a second ``source``.
    """
    image = _read_meta(container_dir, "kento-image")
    if not image:
        return ()
    return (OciReference.parse(image),)


def _load_storage(container_dir: Path) -> StorageMode:
    """Map ``kento-storage`` to ``StorageMode``; absent => OVERLAY (§8, §11.0).

    ``kento-storage`` is not written by create today (reserved; the default is
    fs-overlay), so an absent file is faithfully ``OVERLAY``. A present but
    unrecognized value is a typed-domain fallback to ``OVERLAY`` with a log
    (total — a forward/garbage value must not blow up ``list()``).
    """
    raw = _read_meta(container_dir, "kento-storage")
    if not raw:
        return StorageMode.OVERLAY
    try:
        return StorageMode(raw)
    except ValueError:
        _instances_logger.warning(
            "unrecognized kento-storage %r in %s; treating as OVERLAY",
            raw, container_dir.name,
        )
        return StorageMode.OVERLAY


def _load_network(container_dir: Path) -> NetworkConnection:
    """Build ``NetworkConnection`` from kento-net-type/-bridge/-mac/-net (§5.5).

    Maps the create-resolved ``kento-net-type`` transport string
    (bridge/host/usermode/none — ``resolve_network``) to the typed
    ``NetworkMode``: a BRIDGE attachment is ``STATIC`` when a static
    ``kento-net`` (``ip=``) is present, else ``DHCP``; host -> HOST, usermode ->
    USER, none -> DISABLED (§5.1). The ``kento-net`` key=value lines
    (``ip``/``gateway``/``dns``/``searchdomain``) become ``ip_config`` with the
    typed key names (``ip`` -> ``address`` + CIDR split into ``subnet``; ``dns``
    -> ``dns1``); ``searchdomain`` is dropped from the typed model (§5.3). MAC ->
    ``link_config[mac]``. Total: an unrecognized net-type falls back to DISABLED
    with a log.
    """
    net_type = _read_meta(container_dir, "kento-net-type")
    bridge = _read_meta(container_dir, "kento-bridge")
    mac = _read_meta(container_dir, "kento-mac")
    ip_config = _load_ip_config(container_dir)

    link_config: dict[str, str] = {}
    if bridge:
        link_config["bridge"] = bridge
    if mac:
        link_config["mac"] = mac

    mode = _resolve_net_mode(net_type, ip_config, container_dir)
    # ip_config is meaningful only for STATIC (§5.2); for the other modes the
    # lease/slirp/host supplies L3, so we keep it empty even if a stale kento-net
    # lingered — the typed model says ip_config is populated ONLY for STATIC.
    if mode is not NetworkMode.STATIC:
        ip_config = {}
    return NetworkConnection(
        mode=mode, link_config=link_config, ip_config=ip_config,
    )


def _resolve_net_mode(
    net_type: str | None, ip_config: dict[str, str], container_dir: Path,
) -> NetworkMode:
    """Map the persisted ``kento-net-type`` transport to a ``NetworkMode``.

    BRIDGE -> STATIC iff a static ``kento-net`` address was recorded, else DHCP
    (§5.1: Dhcp/Static are peer bridged modes, distinguished by whether L3 was
    pinned by hand). host/usermode/none map directly. An absent or unrecognized
    type is a total fallback to DISABLED with a log (§7.2 totality posture).
    """
    if net_type == "bridge":
        return NetworkMode.STATIC if ip_config.get("address") else NetworkMode.DHCP
    mapped = _NET_TYPE_TO_MODE.get(net_type or "")
    if mapped is not None:
        return mapped
    if net_type:
        _instances_logger.warning(
            "unrecognized kento-net-type %r in %s; treating as DISABLED",
            net_type, container_dir.name,
        )
    return NetworkMode.DISABLED


def _load_ip_config(container_dir: Path) -> dict[str, str]:
    """Parse the ``kento-net`` key=value lines into a typed ``ip_config`` dict.

    ``kento-net`` is written by create as ``key=value`` lines
    (``ip=``/``gateway=``/``dns=``/``searchdomain=``; create.py). We translate to
    the typed ``ip_config`` key names (§5.2): ``ip`` -> ``address`` (a CIDR
    ``10.0.0.5/24`` is decomposed at the boundary into ``address`` + ``subnet``
    via ``parse_cidr`` — same parse-at-the-boundary discipline as the locator);
    ``gateway`` -> ``gateway``; ``dns`` -> ``dns1``. ``searchdomain`` is dropped
    (not in the typed model, §5.3). A malformed CIDR is tolerated (kept as a bare
    address) so one bad state file cannot blow up ``list()``.
    """
    raw = _read_meta(container_dir, "kento-net")
    if not raw:
        return {}
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        key, sep, value = line.partition("=")
        if sep and value:
            fields[key.strip()] = value.strip()

    ip_config: dict[str, str] = {}
    ip = fields.get("ip")
    if ip:
        try:
            address, subnet = parse_cidr(ip)
        except KentoError:
            # A malformed recorded address must not fail the whole snapshot;
            # keep the raw value as the address, no subnet.
            address, subnet = ip, None
        ip_config["address"] = address
        if subnet is not None:
            ip_config["subnet"] = subnet
    if fields.get("gateway"):
        ip_config["gateway"] = fields["gateway"]
    if fields.get("dns"):
        ip_config["dns1"] = fields["dns"]
    return ip_config


def _load_forwards(container_dir: Path) -> dict["HostBinding", "GuestTarget"]:
    """Build the ``forwards`` map from ``kento-port`` (§5.5, §5.7).

    Today ``kento-port`` stores a single ``HOST:GUEST`` line (TCP-only) — which
    is exactly the valid 2-element case of the §5.7A spec grammar, so it parses
    with ``host_addr``/``guest_addr`` = ``None`` and no migration branch. We
    parse each non-empty line via ``parse_forward_spec`` (reusing Block 02's
    boundary parser — never re-split by hand). The multi-line/protocol/address
    rework is Phase 5; here we just load what is on disk into the dict shape.
    Total: a malformed line is skipped with a log rather than failing the
    snapshot.
    """
    raw = _read_meta(container_dir, "kento-port")
    if not raw:
        return {}
    forwards: dict[HostBinding, GuestTarget] = {}
    for line in raw.splitlines():
        spec = line.strip()
        if not spec:
            continue
        try:
            binding, target = parse_forward_spec(spec)
        except KentoError as exc:
            _instances_logger.warning(
                "skipping unparseable kento-port entry %r in %s: %s",
                spec, container_dir.name, exc,
            )
            continue
        forwards[binding] = target
    return forwards


def _resolve_status(container_dir: Path, mode: str) -> Status:
    """Resolve the live lifecycle ``Status`` (§7) — TOTAL.

    Wraps the runtime ``is_running`` probe and the ``pve_config_exists`` orphan
    check; maps to the typed ``Status`` enum:

    * PVE config gone (pve/pve-vm) => ``ORPHAN`` (§7.1 — config-presence state).
    * ``is_running`` True => ``RUNNING``; False => ``STOPPED``.
    * the probe itself raising => ``UNKNOWN`` (a genuine domain state — an
      unreachable node has unobservable status — NOT error-as-data, §7.2). This
      is what keeps ``list()`` total over an unreachable instance.

    SUSPENDED is NOT resolved here (disclosed): the wrapped ``is_running`` is a
    plain bool that collapses a paused VM into RUNNING (it matches the ``qm
    status`` substring, which reports ``running`` when paused — §7.3). Detecting
    SUSPENDED needs ``qm status --verbose`` / QMP ``query-status``, which this
    additive read path does not yet add; a suspended VM therefore reads RUNNING
    for now. The ``Status`` enum carries SUSPENDED so the deeper resolver lands
    non-breakingly later.
    """
    from kento import is_running, pve_config_exists

    # ORPHAN first: for PVE modes a definitively-gone backing .conf is the
    # ORPHAN state regardless of the run probe (§7.1). Mirror list.py's vmid
    # source: pve-lxc uses the dir name as the vmid, pve-vm reads kento-vmid.
    if mode in _PVE_MODES:
        vmid = _orphan_vmid(container_dir, mode)
        if vmid is None:
            # No vmid recorded for a PVE instance => state is orphaned (mirrors
            # reconcile._is_orphan: a missing vmid is treated as gone).
            return Status.ORPHAN
        try:
            if not pve_config_exists(vmid, mode):
                return Status.ORPHAN
        except (PermissionError, OSError):
            # Indeterminate config probe — unobservable, not orphan (§7.2).
            return Status.UNKNOWN

    try:
        running = is_running(container_dir, mode)
    except (OSError, KentoError):
        # The probe failed in a way that leaves the run-state unobservable.
        return Status.UNKNOWN
    return Status.RUNNING if running else Status.STOPPED


def _orphan_vmid(container_dir: Path, mode: str) -> str | None:
    """The vmid used for the PVE orphan probe (mirrors reconcile._orphan_vmid).

    pve-vm reads ``kento-vmid``; pve-lxc uses the container directory name (which
    IS the vmid for a pve-lxc instance). Returns None when pve-vm has no recorded
    vmid.
    """
    if mode == "pve-vm":
        return _read_meta(container_dir, "kento-vmid")
    # pve (pve-lxc): the dir name is the vmid.
    return container_dir.name


def _load_resources(container_dir: Path) -> dict[str, int]:
    """Build the ``resources`` map from ``kento-cores`` / ``kento-memory`` (§11.0).

    An open ``name -> int`` bag (§2 principle 8): ``memory`` (MiB) and ``cores``
    when recorded. A non-integer recorded value is skipped (kept out of the bag)
    rather than crashing the snapshot — these are kento-written and should always
    be integers, but the loader stays total.
    """
    resources: dict[str, int] = {}
    for key, field in (("memory", "kento-memory"), ("cores", "kento-cores")):
        raw = _read_meta(container_dir, field)
        if raw is None:
            continue
        try:
            resources[key] = int(raw)
        except ValueError:
            _instances_logger.warning(
                "non-integer %s %r in %s; omitting from resources",
                field, raw, container_dir.name,
            )
    return resources


def _load_platform_profile(container_dir: Path, mode: str) -> PlatformProfile:
    """Build ``PlatformProfile`` from kento-mode/-vmid/-pve-args (§6.3, §11.0).

    The platform axis is split out of the flat ``kento-mode`` (§6): a PVE mode
    (``pve``/``pve-vm``) => ``PlatformMode.PVE`` with the vmid as ``mid`` and the
    ``kento-pve-args`` lines as ``extra_args``; anything else => STANDARD with
    ``mid=None`` and empty ``extra_args`` (the PlatformProfile coherence
    invariant, §6.2). The vmid source mirrors ``_orphan_vmid`` (pve-lxc dir name
    / pve-vm ``kento-vmid``).

    A PVE instance is genuinely PVE — we do NOT fabricate a STANDARD profile to
    paper over a malformed-on-disk vmid. If the recorded vmid is missing or below
    the PVE floor, ``PlatformProfile``'s coherence check raises a typed
    ``ValidationError`` (§6.2) — a real domain error surfaced honestly, not
    swallowed. ``list()`` stays total because that propagates as a ``KentoError``
    which the per-entry loop skips-and-logs (it does not abort the listing); a
    direct ``get()`` of one malformed instance honestly raises.
    """
    if mode not in _PVE_MODES:
        return PlatformProfile(mode=PlatformMode.STANDARD, mid=None, extra_args=())

    raw_vmid = _orphan_vmid(container_dir, mode)
    mid = int(raw_vmid) if raw_vmid is not None and raw_vmid.isdigit() else None
    extra_args = _load_passthrough(container_dir, "kento-pve-args")
    return PlatformProfile(mode=PlatformMode.PVE, mid=mid, extra_args=extra_args)


def _load_created(container_dir: Path) -> datetime:
    """The instance creation time = the container directory mtime (§11.0).

    Observed state (not user-written). A missing/unreadable dir falls back to the
    epoch so the field is always a real ``datetime`` (never None), keeping the
    loader total.
    """
    try:
        return datetime.fromtimestamp(os.path.getmtime(container_dir))
    except OSError:
        return datetime.fromtimestamp(0)


def _load_environment(container_dir: Path) -> dict[str, str]:
    """Build the ``environment`` map from ``kento-env`` (KEY=VALUE lines, §11.0).

    ``kento-env`` stores one ``KEY=VALUE`` per line (create.py / info.py). We
    decompose into a ``str -> str`` dict; a line with no ``=`` is skipped. A
    value may itself contain ``=`` (split on the FIRST only).
    """
    raw = _read_meta(container_dir, "kento-env")
    if not raw:
        return {}
    env: dict[str, str] = {}
    for line in raw.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            env[key] = value
    return env


def _load_passthrough(container_dir: Path, filename: str) -> tuple[str, ...]:
    """Read a pass-through args file into a tuple of non-empty lines.

    Same shape as ``info._read_passthrough_args`` (kento-lxc-args /
    kento-qemu-args / kento-pve-args): one arg per line, absent file => empty.
    Returns a ``tuple`` (immutable) to match the typed field declarations.
    """
    f = container_dir / filename
    if not f.is_file():
        return ()
    return tuple(line for line in f.read_text().splitlines() if line)
