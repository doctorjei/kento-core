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
import subprocess
from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from kento._diagnosis import Diagnosis, ReclaimReport
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
    from collections.abc import Iterator, Sequence

    from kento._network import GuestTarget, HostBinding
    from kento.errors import StateError

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

    # Dead-handle flag (§11.2 M7): set True after ``destroy()`` removes the
    # instance. A class-level default of ``False`` means a freshly-loaded
    # snapshot is alive without the loader having to set it; ``destroy`` flips
    # the INSTANCE attribute to True, and every mutating method guards on it via
    # ``_check_alive`` so a post-destroy reuse fails cleanly (§2 principle 5 —
    # raise, never silently no-op) instead of re-touching a removed directory.
    _dead: bool = False

    # Cached snapshot fields (§11.0, §11.2 M9). Block 11 reshapes the field model
    # from plain attributes into TYPED PROPERTIES backed by ``_``-prefixed cached
    # fields. The settable boundary is enforced BY THE TYPE (M9): a settable field
    # exposes a setter; a getter-only field has NO setter, so assigning it raises
    # ``AttributeError`` automatically (the no-setter idiom — clearer than a
    # __setattr__ gate, which would also intercept the loader's own internal
    # writes; explicit per-field properties keep internal writes on ``_field``).
    #
    # Reads return the cached backing value with NO I/O (§2 principle 2, §10.2);
    # a *setter* DOES I/O + catch-reverse (expected per M9 — §2 governs reads).
    # The loader sets the ``_``-prefixed backing fields; lifecycle methods that
    # self-update ``status`` write the ``_status`` backing field (status is
    # getter-only to the PUBLIC, but the lifecycle owns the backing value — the
    # resolution of the "status is getter-only yet lifecycle-mutated" tension).
    #
    # Backing-field annotations (set by ``_load_snapshot`` / ``refresh``):
    _name: str
    _hostname: str
    _sources: tuple[SourceReference, ...]
    _storage: StorageMode
    _network: NetworkConnection
    _forwards: dict["HostBinding", "GuestTarget"]
    _status: Status
    _resources: dict[str, int]
    _platform_profile: PlatformProfile
    _nesting: bool
    _created: datetime
    _environment: dict[str, str]

    # ------------------------------------------------------------------- #
    # Getter-only properties (§11.2 M9): identity + observed + create-time
    # fields. NO setter -> external assignment raises ``AttributeError``.
    # ------------------------------------------------------------------- #
    @property
    def name(self) -> str:
        """The instance name — identity, immutable (§11.0). Getter-only."""
        return self._name

    @property
    def sources(self) -> "tuple[SourceReference, ...]":
        """The boot-source locators — immutable (§11.0, §3.8). Getter-only."""
        return self._sources

    @property
    def storage(self) -> StorageMode:
        """Root-storage strategy — immutable (§8, §11.0). Getter-only."""
        return self._storage

    @property
    def status(self) -> Status:
        """Live lifecycle status — observed, never user-set (§7). Getter-only.

        The lifecycle methods (``start``/``stop``/``scrub``) self-update the
        cached value by writing the ``_status`` backing field; the public
        property exposes no setter, so ``inst.status = X`` from outside raises
        ``AttributeError`` (§11.2 M9: ``status`` is getter-only).
        """
        return self._status

    @property
    def platform_profile(self) -> PlatformProfile:
        """Platform axis (STANDARD/PVE + vmid + pve-args) — immutable (§6).

        Getter-only (§11.2 M9). The pass-through ``--pve-arg`` carried inside
        (``extra_args``) IS settable, via the dedicated ``extra_args`` property;
        the profile *object* itself is not reassignable.
        """
        return self._platform_profile

    @property
    def nesting(self) -> bool:
        """Allow nested virtualization — create-time, immutable (§11.0)."""
        return self._nesting

    @property
    def created(self) -> datetime:
        """Creation time (container-dir mtime) — observed (§11.0). Getter-only."""
        return self._created

    @property
    def environment(self) -> "dict[str, str]":
        """Create-time environment (no ``set --env`` today, §11.0). Getter-only."""
        return self._environment

    @property
    def forwards(self) -> "dict[HostBinding, GuestTarget]":
        """Port-forward map (§5.7) — GETTER-ONLY in this block.

        M9 makes ``forwards`` a LIVE settable field, but the live setter (nft/
        iptables DNAT + QMP ``hostfwd_add``, protocol-aware multi-forward diff-
        apply) is the Phase-5 network rework (§5.7C) — genuinely-new live runtime
        code this additive block does not build. So ``forwards`` stays getter-only
        here: assigning it raises ``AttributeError`` (the no-setter idiom). The
        live setter lands with the Phase-5 MINOR; this is a DOCUMENTED incremental
        gap, NOT a contradiction of M9's locked LIVE semantics.
        """
        return self._forwards

    # ------------------------------------------------------------------- #
    # Settable properties (§11.2 M9) — base fields shared by all kinds.
    #
    # Each setter follows the LOCKED M9 persist model: immediate-persist with
    # per-set CATCH-AND-REVERSE rollback (no save()/staging). The write is
    # serialized under ``kento_lock`` (excludes a concurrent ``start``); the
    # stopped-only guard is a LIVE ``is_running`` probe (NOT the cached
    # ``status``, which can be stale) taken INSIDE the lock (TOCTOU vs a
    # concurrent start). Persistence is delegated to ``set_cmd.set_cmd`` — the
    # SAME stopped-only mutation the ``kento set`` CLI uses (parity, no new live
    # runtime code); the typed value is decomposed into ``set_cmd`` params here.
    # ------------------------------------------------------------------- #
    @property
    def hostname(self) -> str:
        return self._hostname

    @hostname.setter
    def hostname(self, value: str) -> None:
        """Set the hostname — STOPPED-ONLY (§11.2 M9).

        Persists ``kento-hostname`` (+ the guest ``/etc/hostname`` overlay drop-in)
        via ``set_cmd(hostname=...)``. Running -> ``StateError`` (PVE/pct cannot
        change a running CT's UTS name either — VERIFIED on blue, §11.2 M9). The
        getter still falls back to ``name`` for a pre-back-fill instance (the
        create-time write is the Phase-6 † back-fill, §11.0); a SET persists the
        key, so a subsequent ``refresh()`` reads the new value honestly.
        """
        self._set_via_set_cmd(
            field="hostname",
            new_params={"hostname": value},
            old_params={"hostname": self._hostname},
            new_cached=value,
            cache_attr="_hostname",
        )

    @property
    def network(self) -> NetworkConnection:
        return self._network

    @network.setter
    def network(self, value: NetworkConnection) -> None:
        """Set the whole network attachment — STOPPED-ONLY (§11.2 M9, §5.7).

        Assign a WHOLE typed ``NetworkConnection`` (typed-object stance; sub-edits
        via ``dataclasses.replace``). The value is decomposed FAITHFULLY into
        ``set_cmd`` network params (``network=``/``ip=``/``gateway=``/``dns=`` —
        the exact strings ``set_cmd._parse_network_arg`` / its net params accept).
        Stopped-only — file injection (networkd drop-ins + NIC conf) only applies
        at guest boot, so a running instance raises ``StateError`` (the live-from-
        outside path is Phase 5, §5.7C; ``forwards`` is the only live network
        settable and it too defers). ``set_cmd`` enforces backend × mode validity
        (e.g. usermode is VM-only) — an invalid combo raises its typed error
        BEFORE any write, and the catch-reverse restores the prior value.
        """
        new_params = _network_to_set_cmd_params(value)
        old_params = _network_to_set_cmd_params(self._network)
        self._set_via_set_cmd(
            field="network",
            new_params=new_params,
            old_params=old_params,
            new_cached=value,
            cache_attr="_network",
        )

    @property
    def resources(self) -> "dict[str, int]":
        return self._resources

    @resources.setter
    def resources(self, value: "dict[str, int]") -> None:
        """Set the resource bag (memory/cores) (§11.2 M9; Jei run-33 deferral).

        Assign a WHOLE ``dict[str, int]`` (open bag, §2 principle 8); ``memory``
        (MiB) and ``cores`` are decomposed into ``set_cmd(memory=, cores=)``.

        Live-ness (Jei run-33): M9 locks "memory/cores apply LIVE on a running
        LXC/pve-lxc (cgroup/pct hotplug)", but that live path is genuinely-new
        runtime code (``set_cmd`` is stopped-only; no hotplug primitive exists),
        DEFERRED to Phase 6/E2E. So in THIS block a RUNNING instance raises
        ``StateError`` for BOTH kinds, with a MODE-APPROPRIATE message:

        * VM/pve-vm — "no live hotplug; stop first" (the PERMANENT M9 behavior:
          the memfd is sized to memory at boot, so a VM can never hotplug here).
        * LXC/pve-lxc — "stop first; live resource mutation lands in a future
          release" (a DOCUMENTED incremental gap — the live cgroup/pct path slots
          in HERE in Phase 6; see ``_resources_running_error`` for the seam).

        A STOPPED instance persists via ``set_cmd`` for all four modes. The
        capability-aware DISTINCTION (LXC-will-be-live vs VM-never) is kept VISIBLE
        in the error message + the seam — the deferral does not erase it.
        """
        # Pre-decompose so a bad bag (non-int / unknown key) fails before the
        # lock/probe — and reject the live case with a mode-appropriate message.
        new_params = _resources_to_set_cmd_params(value)
        old_params = _resources_to_set_cmd_params(self._resources)
        self._set_via_set_cmd(
            field="resources",
            new_params=new_params,
            old_params=old_params,
            new_cached=dict(value),
            cache_attr="_resources",
            running_error=self._resources_running_error,
        )

    @property
    def extra_args(self) -> "tuple[str, ...]":
        """Platform pass-through (``--pve-arg``) — convenience view of
        ``platform_profile.extra_args`` (§6, §11.2 M9). PVE-only.

        M9 lists ``extra_args`` as a settable field, but it lives INSIDE the
        getter-only ``platform_profile`` (the profile object is immutable). The
        resolution (brief JC4): a dedicated settable ``extra_args`` property
        whose getter mirrors ``platform_profile.extra_args`` and whose setter
        persists ``kento-pve-args`` AND keeps ``_platform_profile`` coherent via
        ``dataclasses.replace`` — ``platform_profile`` itself stays getter-only.
        The only new public name in this block (a property on an existing class —
        no ``__all__`` change), spec-sanctioned because M9 names ``extra_args``.
        """
        return self._platform_profile.extra_args

    @extra_args.setter
    def extra_args(self, value: "Sequence[str]") -> None:
        """Set the ``--pve-arg`` pass-through — STOPPED-ONLY, PVE-only (§11.2 M9).

        Assign the WHOLE list (declarative replace, mirroring ``kento set
        --pve-arg``); persisted via ``set_cmd(pve_args=[...])``. Stopped-only —
        the PVE ``.conf`` pass-through is consumed at boot. ``set_cmd`` enforces
        PVE-only validity and the denylist (a non-PVE instance or a denylisted
        arg raises BEFORE any write, and the catch-reverse restores the prior
        value). On success the cached ``_platform_profile`` is rebuilt with the
        new ``extra_args`` (``dataclasses.replace``) so the getter — and
        ``platform_profile.extra_args`` — stay coherent without a re-query.
        """
        from dataclasses import replace

        args = list(value)
        new_profile = replace(self._platform_profile, extra_args=tuple(args))
        self._set_via_set_cmd(
            field="extra_args",
            new_params={"pve_args": _set_cmd_list_arg(args)},
            old_params={
                "pve_args": _set_cmd_list_arg(
                    list(self._platform_profile.extra_args)
                )
            },
            new_cached=new_profile,
            cache_attr="_platform_profile",
        )

    def _resources_running_error(self) -> "StateError":
        """The mode-appropriate ``StateError`` for a running resources set.

        Phase-6 SEAM (Jei run-33): when the live cgroup/pct hotplug path is built,
        the LXC/pve-lxc branch here is where "apply live + persist" replaces the
        raise; the VM/pve-vm branch stays a raise PERMANENTLY (memfd sized at
        boot). Keeping the capability-aware distinction visible in the message is
        required (it is not a license to drop it).
        """
        from kento.errors import StateError

        if _is_vm_mode(self._mode):
            return StateError(
                f"cannot change resources on a running {type(self).__name__}: "
                "a VM has no live memory/CPU hotplug (the memfd is sized to "
                f"memory at boot). Stop it first: kento stop {self._name}"
            )
        # LXC / pve-lxc: live cgroup/pct mutation is a future release (Phase 6).
        return StateError(
            "cannot change resources on a running container yet: live resource "
            "mutation on a running LXC/pve-lxc lands in a future release. Stop "
            f"it first: kento stop {self._name}"
        )

    def _set_via_set_cmd(
        self,
        *,
        field: str,
        new_params: "dict[str, object]",
        old_params: "dict[str, object]",
        new_cached: object,
        cache_attr: str,
        running_error: "object | None" = None,
    ) -> None:
        """Persist a settable field via ``set_cmd``, lock-guarded + catch-reverse.

        The shared engine for every base/subclass setter (§11.2 M9 persist model):

        1. ``_check_alive`` (a destroyed handle is unusable, §11.2 M7).
        2. Acquire ``kento_lock`` (excludes a concurrent ``start``; ``set_cmd``
           does NOT take the lock itself, so one acquire here is deadlock-safe).
        3. Take a LIVE ``is_running`` probe INSIDE the lock (NOT the cached
           ``status``) and reject the stopped-only field on a running instance —
           ``running_error()`` (resources' mode-appropriate message) or the
           default ``StateError`` (lxc_args/qemu_args/network/hostname/...).
        4. Call ``set_cmd.set_cmd(self._name, **new_params)``. ``set_cmd``
           validates EVERY field BEFORE mutating anything, so bad input raises
           with no partial write.
        5. CATCH-AND-REVERSE: on any exception from ``set_cmd``, restore the prior
           persisted state by re-invoking ``set_cmd`` with ``old_params`` (still
           stopped -> valid), then re-raise the ORIGINAL error. This covers the
           residual multi-write partial-failure that validate-first cannot (e.g.
           a write that fails after an earlier field already landed). The reverse
           is best-effort: if IT fails we log and still raise the original.
        6. On success only, update the cached backing field — the snapshot stays
           coherent with what was persisted (a getter never re-queries, §2).
        """
        self._check_alive()
        from kento import is_running
        from kento.errors import StateError
        from kento.locking import kento_lock
        from kento import set_cmd as set_cmd_mod

        with kento_lock():
            # LIVE probe inside the lock — the cached status may be stale, and a
            # concurrent start is excluded by the lock we hold (TOCTOU-safe).
            if is_running(self._dir, self._mode):
                if running_error is not None:
                    raise running_error()
                raise StateError(
                    f"cannot change {field} on a running "
                    f"{type(self).__name__}: this setting only applies at the "
                    f"guest's next boot. Stop it first: kento stop {self._name}"
                )
            try:
                set_cmd_mod.set_cmd(self._name, **new_params)
            except Exception:
                # Catch-reverse: restore the prior persisted value, then re-raise
                # the ORIGINAL error (mirrors create.py:_run_cleanup — best-effort
                # rollback that never masks the real failure).
                try:
                    set_cmd_mod.set_cmd(self._name, **old_params)
                except Exception as reverse_err:  # noqa: BLE001 — best-effort
                    _instances_logger.warning(
                        "rollback of %s on %s failed: %s",
                        field, self._name, reverse_err,
                    )
                raise
            # Success: the persisted state matches new_params -> update the cache.
            setattr(self, cache_attr, new_cached)

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
        self._check_alive()
        fresh = _load_snapshot(self._dir, self._mode)
        # Copy the fresh snapshot's cached state into THIS handle (in place), so
        # any existing reference observes the updated values. _populate is the
        # single field-set used by both the loader and refresh.
        self.__dict__.update(fresh.__dict__)

    # ------------------------------------------------------------------- #
    # Dead-handle guard (§11.2 M7).
    # ------------------------------------------------------------------- #
    def _check_alive(self) -> None:
        """Raise if this handle was already destroyed (§11.2 M7, §2 principle 5).

        After ``destroy()`` the backing instance and its directory are gone, so
        any further lifecycle/refresh call would either no-op silently or fail
        with a confusing low-level error. We make the dead state explicit: a
        reused handle raises ``InstanceNotFoundError`` naming the instance, so
        the caller learns the handle is spent rather than acting on a removed
        instance.
        """
        if self._dead:
            raise InstanceNotFoundError(
                f"instance {self.name!r} was destroyed; this handle is no "
                f"longer usable."
            )

    # ------------------------------------------------------------------- #
    # M5 — start: boot the instance; self-update status (§11.2).
    # ------------------------------------------------------------------- #
    def start(self) -> None:
        """Boot the instance (M5, §11.2) — wraps ``start.start``.

        Delegates to the existing mode-aware ``start.start`` (which dispatches on
        ``self._mode`` internally: vm / pve-vm / pve / lxc), then self-updates the
        cached ``status`` from a fresh live probe (§10.2 — a lifecycle method DOES
        I/O and reflects the new state). ``start.start`` is idempotent (a no-op on
        an already-running instance), so a redundant ``start()`` is safe and the
        re-resolved status is still correct.

        Self-update via the Block-08 ``_resolve_status`` rather than a hard-coded
        ``Status.RUNNING``: re-resolving is the honest value (it picks up ORPHAN /
        UNKNOWN, and does not claim RUNNING if the boot silently failed to take),
        and it reuses the one status resolver instead of forking a second notion
        of "running".
        """
        self._check_alive()
        from kento import start as start_mod

        start_mod.start(self.name, container_dir=self._dir, mode=self._mode)
        self._status = _resolve_status(self._dir, self._mode)

    # ------------------------------------------------------------------- #
    # M6 — stop: graceful-or-forced shutdown (§11.2, LOCKED).
    # ------------------------------------------------------------------- #
    # Default graceful grace window (seconds) when ``timeout`` is None (§11.2 M6).
    _STOP_DEFAULT_TIMEOUT = 15

    def stop(self, *, timeout: int | None = None, force: bool = False) -> None:
        """Stop the instance (M6, §11.2 — LOCKED) — wraps ``stop.shutdown``.

        Delivers the full LOCKED M6 semantics. The typed layer owns ALL the
        timing/decision logic; ``stop.shutdown`` supplies the per-mode graceful
        and forced primitives (its graceful path is now genuinely no-kill —
        ``--nokill`` on LXC, no-SIGKILL on VM, no ``--forceStop`` on PVE — so a
        stubborn guest is left running for our re-probe):

        * ``force=False`` — graceful only, NEVER hard-kills. Issue a graceful
          stop, then re-probe: if still running, raise
          ``StopTimeout("cannot stop; try force")`` (the instance is left up).
        * ``force=True``, ``timeout`` None/0 — immediate hard kill.
        * ``force=True``, ``timeout > 0`` — grace that long, THEN kill: a graceful
          stop, a bounded liveness poll up to ``timeout`` seconds, then a hard
          kill only if it is still up.

        ``timeout`` is the grace window; the post-elapse action differs — report
        (raise) vs kill — and the DEFAULT differs by case (§11.2 M6): graceful
        ``None`` => 15s; forced ``None`` => 0 (immediate). Self-updates ``status``
        from a fresh probe afterward (§10.2).
        """
        self._check_alive()
        from kento import stop as stop_mod

        # Default grace window. Graceful: None => 15s. Forced: None => 0
        # (immediate kill); only an explicit timeout>0 buys a forced grace window.
        if timeout is None:
            grace = 0 if force else self._STOP_DEFAULT_TIMEOUT
        else:
            grace = timeout

        if force and grace <= 0:
            # Immediate hard kill: no grace window requested.
            stop_mod.shutdown(
                self.name, force=True,
                container_dir=self._dir, mode=self._mode,
            )
            self._status = _resolve_status(self._dir, self._mode)
            return

        # Both remaining paths begin with a genuine graceful (no-kill) stop.
        stop_mod.shutdown(
            self.name, graceful_only=True,
            container_dir=self._dir, mode=self._mode,
        )
        # Give the guest the grace window to actually go down.
        went_down = _wait_until_down(self._dir, self._mode, grace)

        if not went_down:
            if force:
                # force + timeout>0: grace elapsed, still up -> hard kill now.
                stop_mod.shutdown(
                    self.name, force=True,
                    container_dir=self._dir, mode=self._mode,
                )
            else:
                # Graceful only: NEVER kill -> report it is still running. Resolve
                # status first so the handle reflects the still-running reality.
                self._status = _resolve_status(self._dir, self._mode)
                from kento.errors import StopTimeout

                raise StopTimeout("cannot stop; try force")

        self._status = _resolve_status(self._dir, self._mode)

    # ------------------------------------------------------------------- #
    # M7 — destroy: stop (if needed) + remove instance + writable layer (§11.2).
    # ------------------------------------------------------------------- #
    def destroy(self, *, force: bool = False) -> None:
        """Destroy the instance (M7, §11.2) — wraps ``destroy.destroy``.

        Removes the instance and its writable layer, and releases this instance's
        OWN image hold (never the image — that is ``prune``'s job; ``destroy.py``
        calls ``remove_image_hold`` for this guest only). ``force=True`` →
        force-stop-then-remove (``destroy.destroy`` hard-stops a running instance
        before removal); ``force=False`` on a running instance raises
        ``StateError`` (``destroy.destroy``'s guard) rather than killing it.

        After this returns the backing instance is gone, so the handle is marked
        DEAD: a subsequent lifecycle/refresh call on it raises
        ``InstanceNotFoundError`` (via ``_check_alive``) instead of acting on a
        removed directory. We do NOT self-update ``status`` to a sentinel —
        there is no "destroyed" ``Status`` (the enum models a live instance's
        state); the dead flag is the honest signal that the handle is spent.
        """
        self._check_alive()
        from kento import destroy as destroy_mod

        destroy_mod.destroy(
            self.name, force, container_dir=self._dir, mode=self._mode,
        )
        # Only on success: the instance is gone, so the handle is spent.
        self._dead = True

    # ------------------------------------------------------------------- #
    # M8 — scrub: reset the writable upper layer + re-pin hold (§11.2).
    # ------------------------------------------------------------------- #
    def scrub(self) -> None:
        """Scrub the instance back to pristine image state (M8, §11.2).

        Wraps ``reset.reset``: full reset of the writable upper layer
        (keep-nothing — the surgical keep-``/home`` variant is the future
        ``wipe``/``rebuild`` verbs, not this), keeping the instance + identity,
        and re-pinning the image hold to the freshly-resolved image
        (``reset.reset`` calls ``repin_image_hold``).

        ``reset.reset`` refuses to run on a RUNNING instance (raises
        ``StateError`` — scrub a stopped instance). It leaves the instance
        stopped, so we re-resolve ``status`` afterward to reflect that (it
        stays STOPPED unless something changed underneath).
        """
        self._check_alive()
        from kento import reset as reset_mod

        reset_mod.reset(self.name, container_dir=self._dir, mode=self._mode)
        self._status = _resolve_status(self._dir, self._mode)

    # ------------------------------------------------------------------- #
    # M3 — adopt: heal an orphaned PVE instance; classmethod (§11.1, §11.9).
    # ------------------------------------------------------------------- #
    @classmethod
    def adopt(cls, name: str) -> "Instance":
        """Heal an orphaned PVE instance, returning its handle (M3, §11.1).

        An orphan is a kento-managed pve-lxc / pve-vm instance whose state dir
        survives but whose PVE ``.conf`` was destroyed out-of-band. ``adopt``
        regenerates the missing config (snippets wrapper + hook + ``.conf``)
        from the surviving ``kento-*`` state, bringing the instance back as a
        known instance. It does NOT auto-start or re-mount the rootfs — run
        ``start()`` afterward (§11.1).

        Wraps ``reconcile.adopt`` (which holds ``kento_lock``, requires root,
        and is PVE-only / fails closed). Its typed raises propagate unchanged:
        ``ModeError`` on a non-PVE instance, ``StateError`` when the instance is
        not an orphan / the vmid is occupied / network metadata is unrecoverable
        (§2 principle 5 — a typed raise, never a silent no-op).

        On success returns a FRESH, kind-checked handle via :meth:`get` (a live
        snapshot of the healed instance — not a stale construction). Because
        ``get`` is polymorphic, calling ``SystemContainer.adopt(...)`` on a name
        that healed to a VM (or vice versa) raises ``get``'s kind-mismatch
        ``InstanceNotFoundError`` rather than returning the wrong-typed handle.
        """
        from kento import reconcile

        reconcile.adopt(name)
        # Hand back a live, kind-checked handle (get narrows on a subclass).
        return cls.get(name)

    # ------------------------------------------------------------------- #
    # M4 — prune_orphans: batch-reconcile orphaned state; classmethod (§11.1).
    # ------------------------------------------------------------------- #
    @classmethod
    def prune_orphans(cls, *, reap: bool = False) -> ReclaimReport:
        """Batch-reconcile orphaned PVE state (M4, §11.1) — dry-run by default.

        Enumerates kento PVE instances whose ``.conf`` is definitively gone and,
        when ``reap=True``, destroys each (discarding its surviving state).
        ``reap=False`` (the default) is a DRY RUN: nothing is removed, the report
        lists what WOULD be reaped. Mirrors ``Instance.list()`` — collection-
        scoped, polymorphic over both namespaces:

        * base ``Instance`` => ``scope=None`` (both namespaces);
        * ``SystemContainer`` => ``scope="lxc"`` (the pve-lxc namespace);
        * ``VirtualMachine`` => ``scope="vm"`` (the pve-vm namespace).

        Returns the shared :class:`ReclaimReport` (M25), built per the locked
        mapping (§11.6, spec line ~1334): ``dry_run = not reap``; ``reclaimed`` =
        the orphan names that were (or would be) reaped; ``failed`` = ``(name,
        reason)`` pairs for orphans whose ``destroy`` failed under ``reap=True``
        (the 1.6.2 failure-surfacing contract — surfaced, never swallowed).

        Wraps ``reconcile.reap_orphans`` (which isolates each per-orphan failure
        and never raises for a single failure), then projects its per-orphan
        result entries into the typed report.
        """
        from kento import reconcile

        results = reconcile.reap_orphans(reap, cls._prune_scope())

        reclaimed: list[str] = []
        failed: list[tuple[str, str]] = []
        for entry in results:
            if reap and entry.get("error"):
                # A reap that failed -> surface (name, reason) (1.6.2 contract).
                failed.append((entry["name"], entry["error"]))
            else:
                # Dry-run: every entry is "would reap". reap=True success: reaped.
                # (A dry-run entry never carries an error — reap_orphans sets it
                # only under reap=True; so the else-branch is exactly would-reap
                # or successfully-reaped.)
                reclaimed.append(entry["name"])

        return ReclaimReport(
            dry_run=not reap,
            reclaimed=tuple(reclaimed),
            failed=tuple(failed),
        )

    @classmethod
    def _prune_scope(cls) -> str | None:
        """The ``reap_orphans`` scope for this class (mirrors ``list()``).

        The base ``Instance`` reconciles BOTH namespaces (``None``); a concrete
        kind narrows to its own (``SystemContainer`` -> the LXC namespace,
        ``VirtualMachine`` -> the VM namespace). Only pve-lxc / pve-vm instances
        can orphan, so this is the namespace, not the platform — the same axis
        ``reconcile.find_orphans`` scopes on.
        """
        if cls is SystemContainer:
            return "lxc"
        if cls is VirtualMachine:
            return "vm"
        return None

    # ------------------------------------------------------------------- #
    # M11 — diagnose: INSTANCE-domain health checks for THIS instance (§11.2).
    # ------------------------------------------------------------------- #
    def diagnose(self) -> Diagnosis:
        """Run the read-only INSTANCE-domain health checks for this one (M11).

        Runs the existing ``diagnose.run_diagnostics(self.name)`` scan (read-only
        / silent — it REPORTS, never reaps) and projects the flat findings into a
        typed :class:`Diagnosis` (§11.8 D3). ``run_diagnostics(name)`` returns
        the host-level checks PLUS this instance's checks; M11 is specifically
        the INSTANCE-domain checks for THIS instance, so we filter to
        ``domain=INSTANCE`` and ``subject=self.name`` — dropping the host/image
        findings that the same scan also surfaces (those belong to
        ``kento.diagnose()`` / ``image.diagnose()``).

        Performs I/O (the scan) — an explicit, named method, exactly the kind of
        moment §2 principle 2 permits I/O; the returned ``Diagnosis`` is an inert
        value. Raises ``InstanceNotFoundError`` if the instance has vanished
        (``run_diagnostics`` resolves the name and raises on a miss).
        """
        self._check_alive()
        import importlib

        from kento._diagnosis import DiagnosisDomain, diagnosis_from_report

        # Reach the diagnose SUBMODULE, not the top-level ``kento.diagnose``
        # FUNCTION (Block 10's name-collision foot-gun): ``from kento import
        # diagnose`` would bind the function. ``import_module`` returns the
        # cached submodule from ``sys.modules`` — its ``run_diagnostics``.
        _diagnose = importlib.import_module("kento.diagnose")
        report = _diagnose.run_diagnostics(self.name)
        return diagnosis_from_report(
            report, domain=DiagnosisDomain.INSTANCE, subject=self.name,
        )

    # ------------------------------------------------------------------- #
    # M12 — attach: interactive console/TTY; ON THE BASE (§11.3, all modes).
    # ------------------------------------------------------------------- #
    def attach(self) -> None:
        """Attach to the guest console/TTY — BLOCKS until detach (M12, §11.3).

        On the BASE ``Instance`` because the capability is genuinely shared by
        all four modes (§11.3): ``attach.attach`` already dispatches lxc-attach /
        pct enter / qm terminal / VM serial-relay internally. This is an
        INTERACTIVE method — it takes over the terminal, blocks for the duration
        of the session, and returns when the user detaches (the detach
        key-sequence is a terminal concern the runtime passes through; there is
        no special library detach API in 1.0, §11.3).

        ``-> None`` (the int→None mapping, brief JC3): ``attach.attach`` returns
        the wrapped tool's exit code, but ``attach`` is an interactive console
        session, NOT a status check — the exit code of ``lxc-attach``/``pct
        enter``/``qm terminal`` after a manual detach is not a meaningful result
        to the caller (a clean detach and a session that ran a failing last
        command are indistinguishable here). The spec locks ``-> None``; we DROP
        the exit code deliberately. A genuine *failure to attach at all* (no
        serial socket, not a tty, can't connect) is surfaced by ``attach.attach``
        as a typed ``StateError`` — that propagates, it is not swallowed.

        Raises ``InstanceNotFoundError`` via ``_check_alive`` if the handle was
        destroyed.
        """
        self._check_alive()
        from kento import attach as attach_mod

        # Drop the interactive session's exit code (§11.3: attach is a console,
        # not a status check). Pass the raw mode as the namespace hint is not
        # needed — attach.attach re-resolves from the name across both spaces;
        # a destroyed/renamed name would raise, which _check_alive pre-empts for
        # the dead-handle case and attach.attach's own resolve handles otherwise.
        attach_mod.attach(self.name)


# --------------------------------------------------------------------------- #
# SystemContainer — the LXC backend (§11.0).
# --------------------------------------------------------------------------- #


class SystemContainer(Instance):
    """An LXC system container — full-init backend (§11.0).

    Adds the LXC backend-specific cached fields to the base §11.0 set:

    * ``unprivileged`` — ``kento-unprivileged``; create-time (immutable) —
      getter-only (§11.2 M9).
    * ``lxc_args`` — ``kento-lxc-args`` (``--lxc-arg``); SETTABLE, stopped-only
      (§11.2 M9 — config consumed at boot).

    ``nesting`` lives on the BASE (run 30). ``apparmor`` is intentionally NOT a
    field — it is the ambient ``KENTO_APPARMOR_PROFILE`` env hatch, already
    inspectable via ``diagnose`` (§11.0 ‡).
    """

    # Backing fields (§11.2 M9) — set by the loader; exposed via properties.
    _unprivileged: bool
    _lxc_args: tuple[str, ...]

    @property
    def unprivileged(self) -> bool:
        """Unprivileged (idmap) backend — create-time, immutable. Getter-only."""
        return self._unprivileged

    @property
    def lxc_args(self) -> "tuple[str, ...]":
        return self._lxc_args

    @lxc_args.setter
    def lxc_args(self, value: "Sequence[str]") -> None:
        """Set the ``--lxc-arg`` pass-through — STOPPED-ONLY (§11.2 M9).

        Assign the WHOLE list (declarative replace, mirroring ``kento set
        --lxc-arg``); persisted via ``set_cmd(lxc_args=[...])``. Stopped-only —
        the native LXC ``config`` is consumed at boot, so a running instance
        raises ``StateError``. ``set_cmd`` enforces plain-LXC-only validity and
        the structural-line denylist (an invalid arg raises BEFORE any write).
        An empty list CLEARS the pass-through (``set_cmd``'s clear sentinel). The
        cached value is normalized to a ``tuple`` (the field's immutable type).
        """
        args = list(value)
        self._set_via_set_cmd(
            field="lxc_args",
            new_params={"lxc_args": _set_cmd_list_arg(args)},
            old_params={"lxc_args": _set_cmd_list_arg(list(self._lxc_args))},
            new_cached=tuple(args),
            cache_attr="_lxc_args",
        )

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

    # ------------------------------------------------------------------- #
    # M13 — exec: run a command in the guest, return its exit code (§11.3).
    # ON SystemContainer ONLY (a VM has no in-guest agent; §11.3).
    # ------------------------------------------------------------------- #
    def exec(
        self,
        command: "Sequence[str]",
        *,
        tty: bool = False,
        user: str | None = None,
        env: "dict[str, str] | None" = None,
    ) -> int:
        """Run ``command`` in the guest, returning its exit code (M13, §11.3).

        ON ``SystemContainer`` only (the base lacks it, and a VM has no in-guest
        agent — §11.3). Wraps ``exec_cmd.exec_cmd`` (the operator-authorized
        minimal touch threads ``tty``/``user``/``env`` through):

        * ``tty`` — best-effort (``lxc-attach``/``pct exec`` inherit a pty from
          this process's stdio; a pty cannot be fabricated when the caller has no
          terminal — see ``exec_cmd`` for the honest limit).
        * ``user`` — run as that guest user (``runuser -u <user> -- ``).
        * ``env`` — set in the guest (in-guest ``env K=V … `` prefix).

        Returns the command's exit code and does NOT raise on a non-zero code
        (§11.9, M13: non-zero is normal information — ``grep`` returning 1 is a
        result, not an exception — and the caller decides what it means). A
        genuine inability to RUN the command (instance gone, ``require_root``,
        empty command) still raises the runtime's typed error. ``exec_cmd``
        raises ``ModeError`` for vm/pve-vm — unreachable here since ``exec`` lives
        only on ``SystemContainer`` (LXC family), but it remains the backstop.

        Raises ``InstanceNotFoundError`` via ``_check_alive`` on a dead handle.
        """
        self._check_alive()
        from kento import exec_cmd as exec_cmd_mod

        return exec_cmd_mod.exec_cmd(
            self.name, list(command), tty=tty, user=user, env=env,
        )

    # ------------------------------------------------------------------- #
    # M14 — logs: line-oriented journal stream (§11.3). ADDITIVE generator —
    # does NOT wrap ``logs.logs`` (that streams to stdout + returns int; it is
    # un-wrappable into an Iterator[str]). Reimplements the small LXC-only
    # mode-dispatch with PIPED stdout, like Block 07's direct-podman query.
    # ------------------------------------------------------------------- #
    def logs(
        self, *, follow: bool = False, lines: int | None = None,
    ) -> "Iterator[str]":
        """Line-oriented journal stream from the guest (M14, §11.3).

        ON ``SystemContainer`` only (VM logs are unsupported today — §11.3).
        Returns an ``Iterator[str]`` of decoded journal lines; ONE return type
        covers both modes (§11.3):

        * ``follow=False`` (default) — a FINITE snapshot of the journal *now*.
          ``lines=N`` tails the last N entries (``journalctl -n N``); the
          iterator ends at EOF. The caller does ``list(...)`` / ``"\\n".join(...)``.
        * ``follow=True`` — an OPEN iterator (``journalctl -f``) that yields live
          lines as they are written and does not naturally end. The caller
          iterates and stops when it wants; closing the generator (``.close()`` /
          GC / breaking out of the loop) terminates the ``journalctl -f`` child
          so no follower process leaks (brief JC2).

        ADDITIVE (does NOT touch ``logs.py``): ``logs.logs`` streams to inherited
        stdout and returns an int — un-wrappable into an iterator — so this
        reimplements the same LXC-only ``lxc-attach``/``pct exec journalctl``
        dispatch with PIPED stdout (mirroring how Block 07's ``prune`` queried
        podman directly). Lines are decoded UTF-8 with ``errors="replace"`` (a
        log line is human text; a stray non-UTF-8 byte must not crash the stream,
        and there is no raw-bytes API in 1.0 — §11.3 defers encoding).

        ``_check_alive`` runs eagerly (a destroyed handle raises
        ``InstanceNotFoundError`` at the call, not lazily on first ``next()``);
        the subprocess is spawned lazily inside the generator so the child's
        lifetime is bounded by iteration.
        """
        self._check_alive()
        argv = self._logs_argv(follow=follow, lines=lines)
        return _stream_lines(argv)

    def _logs_argv(self, *, follow: bool, lines: int | None) -> list[str]:
        """Build the host argv that runs ``journalctl`` in the guest (M14).

        Mirrors ``logs.py``'s LXC-only dispatch (plain-lxc ``lxc-attach -n
        <name>`` / pve-lxc ``pct exec <vmid>``), then appends ``journalctl`` with
        the args derived from ``follow``/``lines``:

        * ``follow=True``  -> ``-f`` (live tail; the open-iterator case).
        * ``lines`` set    -> ``-n <N>`` (tail the last N — the spec's ``lines=N``
          snapshot semantics; valid with or without ``-f``).

        A negative ``lines`` is a ``ValidationError`` (``journalctl -n`` wants a
        non-negative count; we reject at the boundary rather than emit a bad arg,
        §2 principle 5). ``self._mode`` is ``"lxc"`` or ``"pve"`` here — a VM mode
        cannot reach this method (``logs`` lives only on ``SystemContainer``).
        """
        if lines is not None and lines < 0:
            from kento.errors import ValidationError

            raise ValidationError(
                f"logs(lines={lines}) must be >= 0 (journalctl -n takes a "
                f"non-negative count)."
            )
        jargs: list[str] = []
        if follow:
            jargs.append("-f")
        if lines is not None:
            jargs += ["-n", str(lines)]

        if self._mode == "pve":
            # pve-lxc: the instance directory name IS the VMID (mirrors logs.py).
            vmid = self._dir.name
            return ["pct", "exec", vmid, "--", "journalctl", *jargs]
        # plain lxc: the name is the container name.
        return ["lxc-attach", "-n", self.name, "--", "journalctl", *jargs]


# --------------------------------------------------------------------------- #
# VirtualMachine — the QEMU/KVM backend (§11.0).
# --------------------------------------------------------------------------- #


class VirtualMachine(Instance):
    """A QEMU/KVM virtual machine — full-system backend (§11.0).

    Adds the VM backend-specific cached field to the base §11.0 set:

    * ``qemu_args`` — ``kento-qemu-args`` (``--qemu-arg``); SETTABLE, stopped-only
      (§11.2 M9 — argv consumed at boot).

    ``nesting`` lives on the BASE (run 30; VM nested-virt CPU features).
    ``kernel``/``initramfs``/``machine`` are image-contract constants, NOT fields
    (M16, §11.0).
    """

    # Backing field (§11.2 M9) — set by the loader; exposed via the property.
    _qemu_args: tuple[str, ...]

    @property
    def qemu_args(self) -> "tuple[str, ...]":
        return self._qemu_args

    @qemu_args.setter
    def qemu_args(self, value: "Sequence[str]") -> None:
        """Set the ``--qemu-arg`` pass-through — STOPPED-ONLY (§11.2 M9).

        Assign the WHOLE list (declarative replace, mirroring ``kento set
        --qemu-arg``); persisted via ``set_cmd(qemu_args=[...])``. Stopped-only —
        the QEMU argv is consumed at boot, so a running instance raises
        ``StateError``. ``set_cmd`` enforces VM-only validity and the argv
        denylist (an invalid arg raises BEFORE any write). An empty list CLEARS
        the pass-through. The cached value is normalized to a ``tuple``.
        """
        args = list(value)
        self._set_via_set_cmd(
            field="qemu_args",
            new_params={"qemu_args": _set_cmd_list_arg(args)},
            old_params={"qemu_args": _set_cmd_list_arg(list(self._qemu_args))},
            new_cached=tuple(args),
            cache_attr="_qemu_args",
        )

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

    # ------------------------------------------------------------------- #
    # M17 — suspend: pause vCPUs to RAM; VM-only; self-update status (§11.4).
    # ------------------------------------------------------------------- #
    def suspend(self) -> None:
        """Pause the VM's vCPUs to RAM (M17, §11.4) — wraps ``suspend.suspend``.

        VM-only (this method lives only on ``VirtualMachine``). Wraps
        ``suspend.suspend`` (QMP ``stop`` for plain vm / ``qm suspend`` for
        pve-vm) — a *pause to RAM*, NOT a shutdown: the VM process keeps running
        and its memory is retained. ``suspend.suspend`` raises ``StateError`` if
        the VM is not running and ``SubprocessError`` if the QMP/qm call fails;
        those propagate (no silent no-op, §2 principle 5).

        Self-updates ``status`` to ``SUSPENDED`` (M17 — brief JC4): on success we
        write the ``_status`` BACKING field directly (the public ``status`` is
        getter-only — §11.2 M9). Unlike ``start``/``stop``, we set the LITERAL
        ``Status.SUSPENDED`` rather than re-resolving via ``_resolve_status``,
        because that resolver CANNOT see SUSPENDED yet (it wraps a plain
        ``is_running`` bool that reports a paused VM as RUNNING — disclosed in
        ``_resolve_status``'s own docstring, §7.3). Re-resolving would
        incorrectly overwrite the just-set SUSPENDED with RUNNING; the literal is
        the honest cached value after a successful suspend.
        """
        self._check_alive()
        from kento import suspend as suspend_mod

        suspend_mod.suspend(self.name)
        self._status = Status.SUSPENDED

    # ------------------------------------------------------------------- #
    # M18 — resume: un-pause vCPUs; VM-only; self-update status (§11.4).
    # ------------------------------------------------------------------- #
    def resume(self) -> None:
        """Un-pause the VM's vCPUs (M18, §11.4) — wraps ``suspend.resume``.

        VM-only. Wraps ``suspend.resume`` (QMP ``cont`` / ``qm resume``). Mirrors
        :meth:`suspend`: ``suspend.resume`` raises ``StateError`` (not running) /
        ``SubprocessError`` (call failed), which propagate.

        Self-updates ``status`` to ``RUNNING`` (M18 — brief JC4) by writing the
        ``_status`` backing field with the LITERAL ``Status.RUNNING`` — the same
        rationale as ``suspend``: ``_resolve_status`` already reports a paused VM
        as RUNNING, so a literal RUNNING after a successful un-pause is correct
        and avoids a redundant probe.
        """
        self._check_alive()
        from kento import suspend as suspend_mod

        suspend_mod.resume(self.name)
        self._status = Status.RUNNING


# --------------------------------------------------------------------------- #
# Captured-line journal streamer (M14, §11.3) — ADDITIVE.
#
# A module-level generator that runs a ``journalctl`` argv with PIPED stdout and
# yields decoded lines. Separated from the method so its process lifecycle is
# self-contained and unit-testable. The cleanup contract (brief JC2): a
# ``follow=True`` (``journalctl -f``) child must NOT leak when the caller stops
# iterating early — closing the generator (``.close()`` / GC / a ``break`` out of
# the for-loop, which Python turns into ``GeneratorExit``) terminates the child.
# --------------------------------------------------------------------------- #


def _stream_lines(argv: list[str]) -> "Iterator[str]":
    """Yield decoded stdout lines from running ``argv``; clean up the child.

    Spawns ``argv`` with ``stdout=PIPE`` (text mode, UTF-8, ``errors="replace"``
    — a stray non-UTF-8 byte must not crash the stream; §11.3 defers a raw-bytes
    API) and yields each line WITHOUT its trailing newline. The generator owns
    the child's whole lifetime:

    * Normal end (``follow=False``): stdout reaches EOF, the loop ends, we
      ``wait()`` and return — a finite snapshot.
    * Early stop (``follow=True`` or any ``break``/``.close()``/GC): Python raises
      ``GeneratorExit`` into the generator at the suspended ``yield``. The
      ``finally`` then TERMINATES the child (``terminate()`` -> short ``wait`` ->
      ``kill()`` if it ignores SIGTERM) and closes the pipe, so no orphaned
      ``journalctl -f`` follower is left running (brief JC2).

    The ``finally`` runs on EVERY exit path (EOF, GeneratorExit, or an exception
    propagating out), so the child and pipe are always reclaimed.
    """
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        # proc.stdout is a TextIO (text=True). Iterating it yields lines as they
        # are flushed by the child — the live-tail behavior for ``-f``.
        assert proc.stdout is not None
        for line in proc.stdout:
            yield line.rstrip("\n")
        # follow=False: child finished writing -> reap it (finite snapshot).
        proc.wait()
    finally:
        _reap(proc)


def _reap(proc: "subprocess.Popen") -> None:
    """Terminate ``proc`` if still running and close its stdout pipe (no leak).

    Idempotent / total: if the child already exited (the ``follow=False`` EOF
    path already ``wait()``-ed), ``poll()`` is non-None and we only close the
    pipe. For a still-running ``journalctl -f`` we SIGTERM it, give it a brief
    moment, then SIGKILL if it ignored the term — so the follower can never
    outlive the iterator. Any teardown error is swallowed (best-effort cleanup
    must not mask the caller's own control flow / exception).
    """
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    except Exception as exc:  # noqa: BLE001 — best-effort teardown
        _instances_logger.warning("error reaping logs child: %s", exc)
    finally:
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except Exception:  # noqa: BLE001 — best-effort
            pass


# --------------------------------------------------------------------------- #
# Typed-value -> set_cmd-param decomposition (§11.2 M9 setters).
#
# The setters take a WHOLE typed value (NetworkConnection / dict / Sequence) and
# decompose it into the EXACT ``set_cmd.set_cmd`` keyword params (the inverse of
# the loader's ``_load_*`` mappings). Getting the param strings right is JC3 — a
# wrong mapping silently mis-persists. ``set_cmd`` then validates + writes.
# --------------------------------------------------------------------------- #


def _set_cmd_list_arg(args: list[str]) -> list[str]:
    """Normalize a pass-through list for ``set_cmd``'s replace/clear sentinel.

    ``set_cmd`` reads a list arg via ``_classify_list``: ``None`` => skip (we
    never want skip — a declarative set always acts), ``[non-empty...]`` =>
    REPLACE, ``[]`` or all-empty => CLEAR. A whole-list assignment of ``[]`` must
    therefore reach ``set_cmd`` as the CLEAR sentinel ``[""]`` (an all-empty
    list), NOT ``[]`` — both classify as clear, but ``[""]`` is the explicit form
    create.py/the CLI emit. A non-empty list passes through verbatim.
    """
    return args if args else [""]


def _network_to_set_cmd_params(conn: "NetworkConnection") -> "dict[str, object]":
    """Decompose a ``NetworkConnection`` into ``set_cmd`` net params (§5, M9).

    The inverse of ``_load_network`` — map the typed ``NetworkMode`` + the two
    L2/L3 maps back to the exact ``set_cmd`` keyword params
    (``network``/``ip``/``gateway``/``dns``/``mac``), the strings
    ``_parse_network_arg`` and the net-delta path consume.

    DECLARATIVE WHOLE-VALUE set (M9: assign a whole new ``NetworkConnection``).
    ``set_cmd`` does an RMW MERGE — it overwrites ONLY the params you pass and
    leaves the rest at their current on-disk value. So to make the assignment
    truly replace the prior state we pass the FULL param set every time,
    explicitly clearing fields the new value doesn't define (else a switch from
    STATIC -> HOST/DHCP would leave a stale static ip/gateway/dns on disk):

    * ``DHCP``   -> ``network="bridge[=name]"``, ``ip="dhcp"`` (clears ip+gw),
      ``dns=""`` (clears dns — ``ip="dhcp"`` does NOT touch dns).
    * ``STATIC`` -> ``network="bridge[=name]"``, ``ip="address[/subnet]"``,
      ``gateway=<gw or "">``, ``dns=<dns1 or "">``.
    * ``USER``   -> ``network="usermode"``,  ``ip="dhcp"``, ``dns=""``.
    * ``HOST``   -> ``network="host"``,       ``ip="dhcp"``, ``dns=""``.
    * ``DISABLED`` -> ``network="none"``,     ``ip="dhcp"``, ``dns=""``.

    ``ip="dhcp"`` is safe for the non-bridge modes: ``set_cmd`` sets the resolved
    ``new["ip"]=None`` for ``dhcp``, so its "``--ip`` requires bridge" guard does
    NOT fire (it checks ``new["ip"] is not None``); it simply means "no static
    address", which is exactly the non-bridge reality. ``""`` is ``set_cmd``'s
    clear sentinel (``x or None`` -> None) for ``gateway``/``dns``.

    ``mac`` (``link_config[mac]``) is passed when present — VM-only in
    ``set_cmd``; on an LXC instance ``set_cmd`` raises ``ModeError`` (a mac on a
    plain-LXC NIC IS invalid), which propagates rather than being silently
    dropped. ``dns2`` cannot round-trip (``set_cmd`` has a single ``--dns``) — a
    value carrying ``dns2`` is REJECTED with ``ValidationError`` rather than
    silently losing it (gate C). ``searchdomain`` is not in the typed model
    (§5.3) and is intentionally absent here.
    """
    from kento.errors import ValidationError

    mode = conn.mode
    bridge = conn.link_config.get("bridge")
    bridge_arg = f"bridge={bridge}" if bridge else "bridge"
    mac = conn.link_config.get("mac")

    if "dns2" in conn.ip_config:
        raise ValidationError(
            "NetworkConnection.ip_config carries 'dns2', but the persistence "
            "layer (set_cmd) supports a single DNS server. Drop 'dns2' (a "
            "second resolver is not yet round-trippable through `kento set`)."
        )

    if mode is NetworkMode.STATIC:
        params: dict[str, object] = {"network": bridge_arg}
        address = conn.ip_config.get("address")
        subnet = conn.ip_config.get("subnet")
        if address is not None:
            params["ip"] = f"{address}/{subnet}" if subnet else address
        params["gateway"] = conn.ip_config.get("gateway") or ""
        params["dns"] = conn.ip_config.get("dns1") or ""
    else:
        # DHCP / USER / HOST / DISABLED: no static L3. Clear ip+gateway (ip=dhcp)
        # and dns explicitly so a prior STATIC value does not linger on disk.
        network = {
            NetworkMode.DHCP: bridge_arg,
            NetworkMode.USER: "usermode",
            NetworkMode.HOST: "host",
            NetworkMode.DISABLED: "none",
        }[mode]
        params = {"network": network, "ip": "dhcp", "dns": ""}

    if mac is not None:
        params["mac"] = mac
    return params


def _resources_to_set_cmd_params(resources: "dict[str, int]") -> "dict[str, object]":
    """Decompose the ``resources`` bag into ``set_cmd(memory=, cores=)`` (M9).

    The inverse of ``_load_resources``: ``memory`` (MiB) and ``cores`` map to the
    ``set_cmd`` scalar params; any other key is rejected (the bag is open at the
    type level, §2 principle 8, but only memory/cores are persistable today — an
    unknown key would silently vanish, so we fail loud per §2 principle 5). A
    non-int value is likewise a typed ``ValidationError``. Only the keys PRESENT
    in the bag are passed (an absent key => ``None`` => left unchanged by
    ``set_cmd``); this is a declarative set of the named resources.
    """
    from kento.errors import ValidationError

    known = {"memory", "cores"}
    unknown = set(resources) - known
    if unknown:
        raise ValidationError(
            f"unsupported resources key(s) {sorted(unknown)!r}; only "
            f"{sorted(known)!r} are settable."
        )
    params: dict[str, object] = {}
    for key in ("memory", "cores"):
        if key in resources:
            value = resources[key]
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValidationError(
                    f"resources[{key!r}] must be an int, got {value!r}."
                )
            params[key] = value
    return params


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

    # The loader writes the ``_``-prefixed BACKING fields directly (Block 11): the
    # public ``name``/``status``/... are now getter-only properties (no setter), so
    # ``inst.name = ...`` would raise ``AttributeError``. Settable fields likewise
    # have their backing field set here — the setter is for EXTERNAL mutation.
    name = _read_meta(container_dir, "kento-name") or container_dir.name
    inst._name = name
    # † hostname: load the hostname key, fallback to name. The create-WRITE
    # back-fill is Phase 6 (a live-path change); the read-fallback is correct
    # now — a pre-back-fill instance has no hostname key, so name is the honest
    # value (§11.0 †). A SET persists kento-hostname, so a later refresh reads it.
    inst._hostname = _read_meta(container_dir, "hostname") or name
    inst._sources = _load_sources(container_dir)
    inst._storage = _load_storage(container_dir)
    inst._network = _load_network(container_dir)
    inst._forwards = _load_forwards(container_dir)
    inst._status = _resolve_status(container_dir, mode)
    inst._resources = _load_resources(container_dir)
    inst._platform_profile = _load_platform_profile(container_dir, mode)
    inst._nesting = (_read_meta(container_dir, "kento-nesting") == "1")
    inst._created = _load_created(container_dir)
    inst._environment = _load_environment(container_dir)

    # Subclass-specific fields (backing fields; ``unprivileged`` is getter-only,
    # ``lxc_args``/``qemu_args`` are settable in Block 11 — both backed by ``_``).
    if isinstance(inst, SystemContainer):
        inst._unprivileged = (_read_meta(container_dir, "kento-unprivileged") == "1")
        inst._lxc_args = _load_passthrough(container_dir, "kento-lxc-args")
    else:  # VirtualMachine
        inst._qemu_args = _load_passthrough(container_dir, "kento-qemu-args")

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


def _wait_until_down(container_dir: Path, mode: str, timeout: int) -> bool:
    """Poll the run-state up to ``timeout`` s; True iff the instance went down.

    Drives the M6 grace window: after a graceful stop, poll the runtime
    ``is_running`` probe until it reports the instance is down or ``timeout``
    seconds elapse. Returns True if it went down within the window (so the caller
    neither raises ``StopTimeout`` nor force-kills), False if it is still up at
    the deadline (caller decides: raise on the graceful path, kill on force).

    Total over a failing probe: if the probe raises (node unreachable, tool
    missing) we treat the instance as DOWN for this decision — we will NOT raise
    "cannot stop" nor hard-kill on an unobservable instance (acting destructively
    or alarmingly on what we cannot even observe is the wrong default; the
    conservative read is to let the graceful stop stand).

    ``timeout <= 0`` does a single immediate probe (no wait). The poll cadence is
    a fixed 0.5s tick; the final probe always lands at-or-after the deadline so a
    just-in-time shutdown is still seen.
    """
    import time

    from kento import is_running

    deadline = time.monotonic() + max(timeout, 0)
    while True:
        try:
            running = is_running(container_dir, mode)
        except (OSError, KentoError):
            # Unobservable -> treat as down (don't raise / don't kill).
            return True
        if not running:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


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
