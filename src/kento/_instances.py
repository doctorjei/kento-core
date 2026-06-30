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
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from kento._diagnosis import Diagnosis, ReclaimReport
from kento._images import Image
from kento._network import (
    NetworkConnection,
    NetworkMode,
    parse_cidr,
    parse_forward_spec,
)
from kento._platform import PlatformMode, PlatformProfile, Status
from kento._references import OciReference, SourceReference
from kento._result import Ok, Result, _error_from
from kento._storage import StorageMode
from kento.errors import InstanceNotFoundError, KentoError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from kento._images import Hold
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

    # Last wrapped-attach exit code (M12 refinement, Jei-ruled): ``attach()``
    # returns ``None`` (it is a console session, not a status check), but the
    # wrapped tool's returncode is CAPTURED and stored here for callers that want
    # it after the fact (plus logged). Class-level ``None`` default = "attach has
    # not run on this handle yet"; ``attach`` sets the INSTANCE attribute.
    _attach_exit_code: int | None = None

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
    _hold: "Hold | None"
    # Overlay UPPERDIR / WORKDIR (§12.2 cell #2). Derived ONCE in the hydrate path
    # from the ``kento-state`` redirect (state_dir/"upper" and state_dir/"work"),
    # cached so the public ``upper``/``work`` properties do ZERO I/O on access
    # (§2 principle 2) — exactly like ``_hold`` (SD2). NOT the lower dir (= the ro
    # base layers, the instance's baked ``kento-layers``) and NOT the merged
    # ``$ROOTFS`` (the realized composite). ``work`` is overlayfs copy-up/rename
    # STAGING — a path only, never modeled as an image (§12.2).
    _upper: Path
    _work: Path

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
    def directory(self) -> Path:
        """The on-disk container directory — observed identity (§11.0). Getter-only.

        The instance's state directory under ``LXC_BASE`` / ``VM_BASE`` (the path
        that holds its ``kento-*`` state files). This is the SAME value the legacy
        ``info``/``inspect`` wire already surfaces as its ``directory`` field — it
        is part of the instance's observable identity, not a derived display
        convenience. Exposing it read-only lets a consumer (e.g. the CLI's wire
        projection, §11.8 D1) reach the residual on-disk state without touching
        the private ``_dir`` backing attribute. Getter-only: a snapshot's location
        is fixed; relocating an instance is not a property assignment.
        """
        return self._dir

    @property
    def environment(self) -> "dict[str, str]":
        """Create-time environment (no ``set --env`` today, §11.0). Getter-only."""
        return self._environment

    @property
    def hold(self) -> "Hold | None":
        """This instance's prune-protection pin, or ``None`` (§12.3). Getter-only.

        The ``Hold`` (``kento-hold.<name>``) that pins this instance's image
        against ``podman prune`` — the cached snapshot value, loaded at
        get/list/refresh (eager, §2 principle 2 — a property returns cached
        state and performs NO I/O on access). ``None`` when no hold exists (an
        adopted/legacy/pre-hold guest). Getter-only: a hold is created/removed
        by the procedural pin lifecycle (``layers.py``), never assigned onto an
        instance handle. The global view of all holds is ``Hold.list()``.
        """
        return self._hold

    @property
    def upper(self) -> Path:
        """The overlay UPPERDIR — where this instance's writes land (§12.2).

        In the 1.0 writable-root model (cell #2: ro base + full overlay), the
        overlayfs ``upperdir`` is the single writable layer; the ro base layers
        are the ``lowerdir`` and are NOT part of this path. Resolved as
        ``state_dir/"upper"`` where ``state_dir`` is the ``kento-state`` redirect
        if present, else the container directory (the same derivation the legacy
        ``info`` wire uses).

        Cached: the ``kento-state`` redirect is read ONCE in the hydrate path
        (get/list/refresh) and ``state_dir/"upper"`` cached, so reading this
        property performs NO I/O (§2 principle 2 — a property returns the cached
        snapshot, never queries the backend). The path is returned whether or not
        it exists on disk (existence is observed by ``disk_usage()``, which does
        the I/O). Getter-only: a snapshot's storage location is fixed.
        """
        return self._upper

    @property
    def work(self) -> Path:
        """The overlayfs WORKDIR — copy-up/rename staging (§12.2). Getter-only.

        overlayfs requires an empty ``workdir`` on the same filesystem as the
        ``upperdir`` for atomic copy-up and rename operations; it is internal
        staging, NOT a layer and NOT modeled as an image (§12.2). Resolved as
        ``state_dir/"work"`` from the SAME single ``kento-state`` read that backs
        :attr:`upper`, and cached identically — so this property does NO I/O on
        access (§2 principle 2). Getter-only.
        """
        return self._work

    def disk_usage(self) -> int:
        """Bytes written by this instance = the size of the overlay UPPERDIR.

        An explicit I/O HANDLE METHOD — NOT a property — because it shells out to
        ``du`` to measure the on-disk tree (§2 principle 2: I/O lives in named
        methods, never behind a property). Measures :attr:`upper` ONLY: the lower
        layers are the read-only base image (shared, not "used" by this instance)
        and are deliberately not counted; ``work`` is transient staging and is
        likewise excluded (§12.2).

        Returns the **allocated** byte size via ``du -s --block-size=1`` (the
        bytes actually occupied on disk — the sum of block allocations, scaled to
        a byte count). This is true disk usage, not the apparent byte count
        (``du -sb``): a sparse file or sub-block tail counts only the blocks it
        occupies, which is the figure a consumer asking "how much disk is this
        instance using" expects. Failure modes:

        * **Upper directory absent** (no writes yet, or a fully-ephemeral cell-#1
          instance) -> ``0``. Checked BEFORE running ``du`` so the common
          no-writes-yet case never spawns a process and never logs.
        * **``du`` non-zero exit / unparseable output** -> ``0`` with a logged
          warning. Rationale: ``disk_usage`` is observational, not a guard — a
          transient ``du`` failure (a racing scrub removing the tree, a
          permission blip) should surface as "nothing measured" rather than crash
          a caller iterating instances or rendering a list. ``0`` is the same
          sentinel as the absent case and is unambiguous against a real size
          (a populated upper is always > 0); the log line preserves the honesty
          (the failure is recorded, not silently swallowed). Mirrors the legacy
          ``info._get_size`` "?"-on-error stance, in the typed ``int`` domain.
        """
        upper = self._upper
        if not upper.is_dir():
            return 0
        result = subprocess.run(
            ["du", "-s", "--block-size=1", str(upper)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            _instances_logger.warning(
                "du failed for %s (exit %d): %s",
                upper, result.returncode, result.stderr.strip(),
            )
            return 0
        try:
            return int(result.stdout.split()[0])
        except (IndexError, ValueError):
            _instances_logger.warning("could not parse du output for %s: %r",
                                      upper, result.stdout)
            return 0

    def image(self) -> Image:
        """Resolve this instance's boot rootfs to a concrete ``Image`` (§4.5).

        An explicit I/O HANDLE METHOD — NOT a property — because resolution
        queries the backing store (§2 principle 2, §4.5: "resolution + prepare
        are explicit handle actions, not properties"). Resolves the FIRST
        (rootfs) boot source — 1.0 is a single ``oci://`` source (§3.8/§4) — via
        ``OciImage.resolve`` against the local store (no network); propagates
        ``ImageNotFoundError`` if the content is gone (no fabricated handle).

        Boot-source override echo (§8 Phase A): when this instance carries a
        persisted ``kento-kernel`` / ``kento-initramfs`` (a VM with ``--kernel`` /
        ``--initrd``), the resolved image's ``kernel`` / ``initramfs`` are
        populated from those state files via ``dataclasses.replace`` — so ``info``
        and a projection can report the override source. Each side is independent;
        an absent marker leaves that side ``None`` (in-image fallback). An image
        with NO override resolves with both ``None`` (unchanged §4.1 default).
        """
        from dataclasses import replace
        from kento._images import OciImage

        if not self._sources:
            raise InstanceNotFoundError(
                f"{self._name}: no boot source recorded (kento-image missing)"
            )
        rootfs_source = self._sources[0]
        if not isinstance(rootfs_source, OciReference):
            # 1.0 resolves only oci:// sources; a reserved scheme (file/http)
            # has no resolver yet. Surface honestly rather than guess (§2 p5).
            raise InstanceNotFoundError(
                f"{self._name}: boot source {rootfs_source!r} is not an "
                f"OCI reference (only oci:// is resolvable in 1.0)."
            )
        # Use the RAISING ``_resolve`` (not the public Result-returning
        # ``resolve``): ``Instance.image()`` is still a raising method (converted
        # in S3), so an ``ImageNotFoundError`` must propagate UP to ``image()``'s
        # own future boundary with its real kind, not collapse to ``INTERNAL`` via
        # an intermediate ``.unwrap()`` (the KIND-FIDELITY rule, Result sweep S2).
        img = OciImage._resolve(rootfs_source)

        kernel_meta = _read_meta(self._dir, "kento-kernel")
        initramfs_meta = _read_meta(self._dir, "kento-initramfs")
        if kernel_meta is None and initramfs_meta is None:
            return img
        return replace(
            img,
            kernel=Path(kernel_meta) if kernel_meta else None,
            initramfs=Path(initramfs_meta) if initramfs_meta else None,
        )

    @property
    def forwards(self) -> "dict[HostBinding, GuestTarget]":
        """Port-forward map (§5.7) — the ONE LIVE-capable network settable (M9).

        Assign a WHOLE typed value ``inst.forwards = {(proto, None, hport):
        (None, gport), ...}`` (typed-object stance — sub-edits are ``dict(...)``
        by the caller / the CLI's RMW). On a **running** instance the setter
        applies the change LIVE (bridged = nft/iptables DNAT diff-apply against
        the live-resolved guest IP; VM-usermode = QMP ``hostfwd_add``/``remove``)
        AND persists; on a **stopped** one it persists only (next boot rebuilds
        from ``kento-port``). The getter returns the cached snapshot value with no
        I/O (§2). See the setter for the full live mechanism (§5.7C / §11.2 M9).
        """
        return self._forwards

    @forwards.setter
    def forwards(self, value: "dict[HostBinding, GuestTarget]") -> None:
        """Set the whole port-forward map — LIVE on running, persist on stopped.

        The ONLY live-capable network settable (§11.2 M9, §5.7C). Routes through
        :meth:`_set_forwards_live`, which diffs against the current set, applies
        removes+adds live (running) via the host firewall (bridged) or QMP
        (VM-usermode), then persists ``kento-port`` — all under ``kento_lock``,
        guarded on a LIVE ``is_running`` probe, with catch-reverse rollback.
        """
        self._set_forwards_live(value)

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
        """Set the resource bag (memory/cores) — CAPABILITY-AWARE (§11.2 M9).

        Assign a WHOLE ``dict[str, int]`` (open bag, §2 principle 8); ``memory``
        (MiB) and ``cores`` are the two settable keys. Live-ness per M9:

        * **Stopped** (any kind) — persist only (``set_cmd`` writes
          ``kento-memory``/``kento-cores`` + the boot config).
        * **Running + VM/pve-vm** — RAISE ``StateError`` (PERMANENT: a VM's
          memory memfd is sized at boot, so there is no live memory/CPU
          hotplug). The message comes from ``_resources_running_error``.
        * **Running + LXC/pve-lxc** — APPLY LIVE (Block 16): plain LXC via the
          running container's cgroup-v2 knobs (``memory.max``/``cpu.max``, the
          exact knobs the boot config + hook use); pve-lxc via ``pct set``
          (PVE's live-capable path). Then persist. The live apply + persist are
          sequenced apply-live-THEN-persist + catch-reverse undo (both-or-none).

        The whole running-LXC path lives in ``_set_resources_live`` (a sibling of
        the stopped-only ``_set_via_set_cmd`` engine, mirroring how ``forwards``
        gets ``_set_forwards_live``): the stopped-only engine raises on a running
        instance, which a live-capable field must not.
        """
        # Pre-decompose so a bad bag (non-int / unknown key) fails before the
        # lock/probe — for BOTH the stopped persist and the live params.
        new_params = _resources_to_set_cmd_params(value)
        old_params = _resources_to_set_cmd_params(self._resources)
        self._set_resources_live(value, new_params, old_params)

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
        """The PERMANENT ``StateError`` for a running VM/pve-vm resources set.

        VM-family ONLY (Block 16): a VM has no live memory/CPU hotplug — the
        memory memfd is sized at boot — so this raise is permanent, not a seam.
        The running LXC/pve-lxc case is NO LONGER routed here; it applies live in
        ``_set_resources_live`` (the Phase-5c fold-in that closed Block 11's
        Phase-6 seam). Callers must only invoke this on a VM-family mode.
        """
        from kento.errors import StateError

        return StateError(
            f"cannot change resources on a running {type(self).__name__}: "
            "a VM has no live memory/CPU hotplug (the memfd is sized to "
            f"memory at boot). Stop it first: kento stop {self._name}"
        )

    def _set_via_set_cmd(
        self,
        *,
        field: str,
        new_params: "dict[str, object]",
        old_params: "dict[str, object]",
        new_cached: object,
        cache_attr: str,
    ) -> None:
        """Persist a STOPPED-ONLY settable field via ``set_cmd``, lock-guarded +
        catch-reverse.

        The shared engine for every STOPPED-ONLY base/subclass setter
        (lxc_args/qemu_args/network/hostname/extra_args — §11.2 M9 persist model).
        ``resources`` and ``forwards`` are CAPABILITY-AWARE (live on a running
        LXC) and so use their own engines (``_set_resources_live`` /
        ``_set_forwards_live``); this engine UNCONDITIONALLY raises on a running
        instance, which a live-capable field must not.

        1. ``_check_alive`` (a destroyed handle is unusable, §11.2 M7).
        2. Acquire ``kento_lock`` (excludes a concurrent ``start``; ``set_cmd``
           does NOT take the lock itself, so one acquire here is deadlock-safe).
        3. Take a LIVE ``is_running`` probe INSIDE the lock (NOT the cached
           ``status``) and reject a running instance with ``StateError``
           (stopped-only: the setting only applies at the guest's next boot).
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
    # forwards — the ONE LIVE-capable network settable (§11.2 M9, §5.7C).
    #
    # The live firewall/QMP I/O does not fit the stopped-only ``_set_via_set_cmd``
    # path (which raises on a running instance), so it gets its OWN engine — but
    # it MIRRORS the same discipline: ``_check_alive`` -> ``kento_lock`` -> LIVE
    # ``is_running`` probe inside the lock -> apply -> catch-reverse -> cache on
    # success. The difference is the running branch APPLIES live (diff-apply +
    # catch-reverse undo stack) instead of raising, and persistence is sequenced
    # apply-live-THEN-persist with a rollback that also restores the state file.
    # ------------------------------------------------------------------- #
    def _set_forwards_live(
        self, value: "dict[HostBinding, GuestTarget]") -> None:
        """Engine for the ``forwards`` setter (§5.7C / §11.2 M9).

        Steps (mirroring ``_set_via_set_cmd`` lock/guard discipline):

        1. ``_check_alive`` (a destroyed handle is unusable, §11.2 M7).
        2. Validate the NEW set first (re-render every binding -> the §5.7A spec
           lines, via ``parse_forwards`` over the rendered specs) so a bad value
           (address form, malformed) raises with NO state touched.
        3. Acquire ``kento_lock`` (excludes a concurrent start; one acquire here,
           ``set_cmd`` takes no lock -> deadlock-safe).
        4. LIVE ``is_running`` probe INSIDE the lock (NOT cached ``status``).
        5. RUNNING -> compute the diff (``add = new - old``, ``remove = old -
           new`` keyed by ``HostBinding``), apply removes+adds LIVE (bridged
           firewall or VM-usermode QMP), pushing each op's INVERSE on an undo
           stack; THEN persist ``kento-port``. On ANY failure, unwind the undo
           stack (re-add removed rules / remove added rules) and re-raise.
           STOPPED -> persist only (next boot rebuilds from state).
        6. On success only, update the ``_forwards`` cache (§2: getter never
           re-queries).

        Persistence (``_persist_forwards``) writes ``kento-port`` via the SAME
        canonical low-level writer the ``kento set --port`` CLI uses
        (``set_cmd._apply_port_meta``), so the on-disk form is canonical with no
        second writer — but NOT through ``set_cmd`` itself, which rejects a
        running instance and would make the live path never succeed.
        """
        from kento._network import render_forward_spec, parse_forwards
        from kento.locking import kento_lock

        self._check_alive()

        # (2) Validate the new set up front — render to spec lines, re-parse to
        # catch address forms / dupes BEFORE any I/O. parse_forwards over the
        # rendered specs is the same validation set_cmd will run, surfaced early.
        new_specs = [render_forward_spec(b, t) for b, t in value.items()]
        parse_forwards(new_specs)  # raises ValidationError / ForwardAddress...

        with kento_lock():
            from kento import is_running

            # LIVE probe inside the lock (NOT cached status); a concurrent start
            # is excluded by the lock we hold (TOCTOU-safe).
            if not is_running(self._dir, self._mode):
                # STOPPED: persist only. The next boot rebuilds rules from state.
                self._persist_forwards(new_specs)
                self._forwards = dict(value)
                return

            # RUNNING: diff-apply live, then persist; rollback on any failure.
            old = self._forwards
            to_remove = {b: t for b, t in old.items() if b not in value}
            to_add = {b: t for b, t in value.items()
                      if b not in old or old[b] != value[b]}
            # A binding whose target CHANGED is a remove-then-add (its rules point
            # at the old guest_port); include it in both so the live state ends up
            # matching ``value`` exactly.
            for b, t in value.items():
                if b in old and old[b] != t:
                    to_remove[b] = old[b]

            undo: list[Callable[[], None]] = []
            try:
                # Build the live applier ONLY when there is live work to do — a
                # no-op diff (e.g. re-assigning the same set) must not raise the
                # "no live applier for this config" ModeError, and need touch no
                # firewall/QMP at all (just re-persist the identical file).
                if to_remove or to_add:
                    applier = self._forwards_applier()
                    for binding, target in to_remove.items():
                        applier.remove(binding, target)
                        undo.append(
                            lambda b=binding, t=target: applier.add(b, t))
                    for binding, target in to_add.items():
                        applier.add(binding, target)
                        undo.append(
                            lambda b=binding, t=target: applier.remove(b, t))
                # Live state now matches ``value``; persist the state file last.
                self._persist_forwards(new_specs)
            except Exception:
                # Catch-reverse: unwind applied live ops in REVERSE order, then
                # re-raise the ORIGINAL error (mirrors create.py:_run_cleanup —
                # best-effort, never masks the real failure).
                for inverse in reversed(undo):
                    try:
                        inverse()
                    except Exception as rb_err:  # noqa: BLE001 — best-effort
                        _instances_logger.warning(
                            "rollback of a forwards op on %s failed: %s",
                            self._name, rb_err,
                        )
                raise
            self._forwards = dict(value)

    def _persist_forwards(self, specs: "list[str]") -> None:
        """Persist the forward set to ``kento-port`` (declarative full set, §5.7B).

        Reuses ``set_cmd._apply_port_meta`` — the SAME canonical ``kento-port``
        writer the ``kento set --port`` CLI uses (it re-renders each spec via
        ``render_forward_spec`` so the on-disk form is canonical + deduped), one
        copy, no drift. It is called DIRECTLY, NOT via ``set_cmd``: ``set_cmd``
        unconditionally rejects a RUNNING instance (set_cmd.py:401 — "Stop it
        first"), so routing the live (running) persist through it would make the
        live setter ALWAYS fail-then-rollback (the Blocker the Editor caught).
        The low-level writer has no such guard — correct here, because by the time
        we persist on the running path the rules are ALREADY applied live, and the
        live-vs-stopped decision belongs to ``_set_forwards_live`` (which holds
        ``kento_lock`` across the whole op), not to the file writer.

        ``"replace"`` writes the N specs; ``"clear"`` unlinks the file when the
        set is empty (the writer's two declarative actions). Plain file I/O under
        our held ``kento_lock``.
        """
        from kento.set_cmd import _apply_port_meta

        if specs:
            _apply_port_meta(self._dir, specs, "replace")
        else:
            _apply_port_meta(self._dir, [""], "clear")

    def _forwards_applier(
        self) -> "_BridgedForwardsApplier | _QmpForwardsApplier":
        """The mode-appropriate live applier (bridged firewall vs VM-usermode QMP).

        * Container modes (lxc / pve-lxc) -> ``_BridgedForwardsApplier`` (host
          nft/iptables DNAT against the resolved guest IP).
        * VM-family modes (vm / pve-vm) in **USER (slirp) net-mode** -> the
          ``_QmpForwardsApplier`` (QMP ``hostfwd_add``/``remove`` over
          ``qmp.sock``).
        * A VM-family instance NOT in usermode -> bridged-VM forwarding is not a
          current capability (§5.7C), so there is no live applier. We raise a
          clear ``ModeError`` rather than hand back the QMP applier (which would
          send ``hostfwd_add`` to a VM that has no slirp netdev and fail with an
          opaque QMP error) — gate C: don't return an applier that invites a
          misleading failure. This is only reachable on the RUNNING path with a
          non-empty live diff; the STOPPED path persists without an applier.
        """
        if _is_vm_mode(self._mode):
            if self._network.mode is not NetworkMode.USER:
                from kento.errors import ModeError

                raise ModeError(
                    f"cannot apply port forwards live to '{self._name}': live "
                    "port-forwarding on a VM requires usermode (slirp) "
                    f"networking, but this VM is in {self._network.mode.value!r} "
                    "mode (bridged-VM forwarding is not supported). Stop it to "
                    "change forwards, which apply at next boot."
                )
            return _QmpForwardsApplier(self._dir / "qmp.sock", self._name)
        return _BridgedForwardsApplier(self)

    # ------------------------------------------------------------------- #
    # resources — capability-aware (§11.2 M9). The running LXC/pve-lxc live
    # path (Block 16, Phase 5c) does not fit the stopped-only ``_set_via_set_cmd``
    # engine (which raises on a running instance), so — exactly like ``forwards``
    # — it gets its OWN engine that MIRRORS the same discipline: ``_check_alive``
    # -> ``kento_lock`` -> LIVE ``is_running`` probe inside the lock -> apply ->
    # catch-reverse -> persist -> cache on success. The difference from the
    # stopped-only engine: the running LXC branch APPLIES live (cgroup / pct set
    # with a both-or-neither undo stack) instead of raising; the running VM branch
    # raises PERMANENTLY (no memory/CPU hotplug); the stopped branch persists.
    # ------------------------------------------------------------------- #
    def _set_resources_live(
        self,
        value: "dict[str, int]",
        new_params: "dict[str, object]",
        old_params: "dict[str, object]",
    ) -> None:
        """Engine for the ``resources`` setter (§11.2 M9 / Block 16).

        ``value`` is the whole new bag (already validated by
        ``_resources_to_set_cmd_params`` -> ``new_params``, the
        ``memory=``/``cores=`` scalars; ``old_params`` is the same for the prior
        cached bag, used for the stopped catch-reverse). Steps:

        1. ``_check_alive`` (a destroyed handle is unusable, §11.2 M7).
        2. Acquire ``kento_lock`` (excludes a concurrent ``start``; ``set_cmd``
           and the live tools take no lock themselves -> one acquire here is
           deadlock-safe).
        3. LIVE ``is_running`` probe INSIDE the lock (NOT cached ``status``).
        4. STOPPED -> persist via ``set_cmd`` with catch-reverse — BYTE-IDENTICAL
           to the old (Block 11) stopped path (preserves the VM node-capacity
           clamp + field validation that ``set_cmd`` runs). RUNNING + VM/pve-vm ->
           raise the PERMANENT ``_resources_running_error`` (no hotplug). RUNNING
           + LXC/pve-lxc -> apply each changed knob LIVE (pushing its INVERSE on
           an undo stack), THEN persist the canonical config via
           ``_persist_resources``; on ANY failure unwind the undo stack (restore
           the prior live value) and re-raise — both-or-neither (JC4/JC5).
        5. On success only, update the ``_resources`` cache (§2: getter never
           re-queries).
        """
        from kento.locking import kento_lock

        self._check_alive()

        with kento_lock():
            from kento import is_running

            if not is_running(self._dir, self._mode):
                # STOPPED: persist via set_cmd + catch-reverse (Block 11 path,
                # byte-identical — keeps the VM clamp + set_cmd validation).
                self._persist_resources_stopped(new_params, old_params)
                self._resources = dict(value)
                return

            # RUNNING + VM/pve-vm: PERMANENT raise — no live memory/CPU hotplug.
            if _is_vm_mode(self._mode):
                raise self._resources_running_error()

            # RUNNING + LXC/pve-lxc: apply each changed knob LIVE, undo on fail.
            applier = self._resources_applier()
            old = self._resources
            undo: list[Callable[[], None]] = []
            try:
                for key in ("memory", "cores"):
                    if key not in new_params:
                        continue
                    new_val = new_params[key]
                    if key in old and old[key] == new_val:
                        continue  # unchanged -> no live op, nothing to undo
                    old_val = old.get(key)
                    applier.apply(key, new_val)
                    if old_val is not None:
                        undo.append(
                            lambda k=key, v=old_val: applier.apply(k, v))
                    # No prior recorded value -> nothing meaningful to restore
                    # live (the kernel's pre-existing limit stands); the persist
                    # rollback below still restores the on-disk state.
                # Live state now matches ``value``; persist the canonical config.
                self._persist_resources(new_params)
            except Exception:
                # Catch-reverse: unwind applied LIVE knobs in REVERSE order, AND
                # restore the on-disk config to the prior values, then re-raise
                # the ORIGINAL error (mirrors create.py:_run_cleanup — best-effort,
                # never masks the real failure). Restoring disk too keeps the
                # running path SYMMETRIC with the stopped path (which restores via
                # set_cmd(old_params)): if persist failed mid-write (e.g.
                # kento-memory landed but kento-cores did not), a bare live-only
                # unwind would leave a PARTIAL config that boots stale next start,
                # diverging from the restored live+cache. So we also re-write disk
                # to old_params. When persist was never reached (a live-knob
                # failure), old_params == what is already on disk, so this is an
                # idempotent no-op write — never harmful.
                for inverse in reversed(undo):
                    try:
                        inverse()
                    except Exception as rb_err:  # noqa: BLE001 — best-effort
                        _instances_logger.warning(
                            "rollback of a resources knob on %s failed: %s",
                            self._name, rb_err,
                        )
                try:
                    self._persist_resources(old_params)
                except Exception as disk_err:  # noqa: BLE001 — best-effort
                    _instances_logger.warning(
                        "rollback of the resources config on %s failed: %s",
                        self._name, disk_err,
                    )
                raise
            self._resources = dict(value)

    def _persist_resources_stopped(
        self,
        new_params: "dict[str, object]",
        old_params: "dict[str, object]",
    ) -> None:
        """Stopped-path persist via ``set_cmd`` + catch-reverse (Block 11 path).

        Identical in behavior to the old ``_set_via_set_cmd`` stopped persist for
        resources: call ``set_cmd.set_cmd(name, **new_params)`` (which runs field
        validation + the VM node-capacity clamp), and on any failure re-invoke it
        with ``old_params`` to restore the prior persisted value, then re-raise
        the ORIGINAL error (best-effort reverse; a failed reverse is logged, not
        masked). Called only on the STOPPED branch, under our held ``kento_lock``;
        ``set_cmd`` takes no lock itself, so this is deadlock-safe.
        """
        from kento import set_cmd as set_cmd_mod

        try:
            set_cmd_mod.set_cmd(self._name, **new_params)
        except Exception:
            try:
                set_cmd_mod.set_cmd(self._name, **old_params)
            except Exception as reverse_err:  # noqa: BLE001 — best-effort
                _instances_logger.warning(
                    "rollback of resources on %s failed: %s",
                    self._name, reverse_err,
                )
            raise

    def _persist_resources(self, params: "dict[str, object]") -> None:
        """Running-path persist: write the canonical kento LXC config (§11.0).

        Reuses the SAME low-level writers ``set_cmd`` uses to write
        ``kento-memory``/``kento-cores`` + the boot config cgroup lines
        (``set_cmd._apply_lxc`` for plain LXC / ``_apply_pve_lxc`` for pve-lxc) —
        one canonical writer, no drift. Called DIRECTLY, NOT via ``set_cmd``:
        ``set_cmd`` unconditionally rejects a RUNNING instance (set_cmd.py:401 —
        "Stop it first"), so routing the live (running) persist through it would
        make the live setter ALWAYS fail-then-rollback. The low-level writers
        have no such guard — correct here, because by the time we persist on the
        running path the cgroup/pct value is ALREADY applied live, and the
        live-vs-stopped decision belongs to ``_set_resources_live`` (which holds
        ``kento_lock`` across the whole op), not to the file writer. Only reached
        on the RUNNING + LXC/pve-lxc branch (the only modes with a live applier).
        """
        from kento import set_cmd as set_cmd_mod

        memory = params.get("memory")
        cores = params.get("cores")
        if self._mode == "pve":
            set_cmd_mod._apply_pve_lxc(self._dir, memory, cores, pve_args=None)
        else:  # lxc
            set_cmd_mod._apply_lxc(self._dir, memory, cores)

    def _resources_applier(self) -> "_LxcResourcesApplier | _PveLxcResourcesApplier":
        """The mode-appropriate LIVE resources applier (Block 16).

        * Plain LXC -> ``_LxcResourcesApplier`` (writes the running container's
          cgroup-v2 ``memory.max``/``cpu.max`` via ``lxc-cgroup -n <name>`` — the
          exact knobs the boot config + start-host hook use).
        * pve-lxc -> ``_PveLxcResourcesApplier`` (``pct set <vmid> -memory/-cores
          -cpulimit`` — PVE's live-capable path; matches the create mapping where
          ``cores`` is cpuset and ``cpulimit`` drives ``cpu.max``).

        Only ever called on the RUNNING + LXC/pve-lxc branch — VM-family raises
        before reaching here, and the stopped branch persists without an applier.
        """
        if self._mode == "pve":
            return _PveLxcResourcesApplier(self._dir, self._name)
        return _LxcResourcesApplier(self._name)

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
        ``name`` is; called on a **subclass** it NARROWS — resolving WITHIN that
        kind's namespace, so a name that exists in BOTH namespaces (a
        ``create --force`` duplicate) resolves to the subclass's own kind rather
        than raising ``resolve_any``'s cross-namespace "ambiguous" error.
        Resolving a name that is ONLY a DIFFERENT kind still raises a
        kind-mismatch ``InstanceNotFoundError`` whose message names the actual
        kind (NOT a ``None`` return, §2 principle 5). Raises
        ``InstanceNotFoundError`` when no such instance exists in any namespace.
        """
        from kento import InstanceNotFoundError, resolve_any

        namespace = cls._namespace()
        if namespace is None:
            # Base Instance: span BOTH namespaces (byte-identical to before — a
            # bare lookup of a dup name is ambiguous; the caller picks a scope).
            container_dir, mode = resolve_any(name)
            inst = _load_snapshot(container_dir, mode)
            cls._reject_kind_mismatch(inst, name)
            return inst

        # Subclass: NARROW to this kind's namespace first (so a cross-namespace
        # dup resolves THIS kind, not "ambiguous").
        try:
            container_dir, mode = resolve_any(name, namespace=namespace)
        except InstanceNotFoundError:
            # Not in our namespace. If the name IS the OTHER kind, surface the
            # spec-required kind-mismatch message (naming the actual kind);
            # otherwise re-raise the genuine not-found. We re-resolve via the
            # base (both-namespace) path to discover the other-kind instance.
            other = _try_resolve_other_kind(name)
            if other is not None:
                cls._reject_kind_mismatch(other, name)  # always raises here
            raise
        inst = _load_snapshot(container_dir, mode)
        # Defensive: namespace-scoped resolve already guarantees the kind, but
        # keep the kind-check so an unexpected mode still fails type-honestly.
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

    @classmethod
    def _namespace(cls) -> str | None:
        """The runtime namespace this class resolves within (§10.1 narrowing).

        The base ``Instance`` spans BOTH namespaces (``None``); a concrete kind
        narrows to its own — ``SystemContainer`` -> the ``"lxc"`` namespace,
        ``VirtualMachine`` -> the ``"vm"`` namespace. This is the single source of
        the class->namespace axis used by ``get``/``list`` (narrowing) and
        ``prune_orphans`` (``_prune_scope``).
        """
        if cls is SystemContainer:
            return "lxc"
        if cls is VirtualMachine:
            return "vm"
        return None

    # ------------------------------------------------------------------- #
    # M2 — list: enumerate cls's kind, narrowed by namespace (§11.1).
    # ------------------------------------------------------------------- #
    @classmethod
    def list(cls) -> "list[Instance]":
        """Enumerate instances of ``cls``'s kind (M2, §10.1 narrowing).

        Globs ``*/kento-image`` in the relevant namespace base(s) (the same
        enumeration source ``list.py`` uses) and loads each as a snapshot.
        Polymorphic (§10.1): the **base** scans BOTH namespaces and returns ALL
        kinds; a **subclass** NARROWS to its OWN namespace
        (``SystemContainer`` -> ``LXC_BASE``, ``VirtualMachine`` -> ``VM_BASE``),
        so it never even loads the other kind's entries. The ``isinstance`` filter
        remains as a type-honesty backstop. No filter params in 1.0 (callers
        filter the typed list, §11.9).

        TOTAL OVER THE STORE: a corrupt / mid-destroy / unresolvable entry is
        SKIPPED WITH A LOG, never fatal to the whole listing — one bad instance
        must not hide every healthy one (mirrors ``list.py``'s per-entry
        ``except OSError: continue`` and the ``Status.UNKNOWN`` totality
        rationale, §7.2). The status probe itself is already total (a failed
        probe yields ``Status.UNKNOWN``, not an exception).
        """
        from kento import LXC_BASE, VM_BASE

        namespace = cls._namespace()
        if namespace == "lxc":
            bases = (LXC_BASE,)
        elif namespace == "vm":
            bases = (VM_BASE,)
        else:  # base Instance — scan both (byte-identical to before).
            bases = (LXC_BASE, VM_BASE)

        instances: list[Instance] = []
        for base in bases:
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
    def start(self) -> Result[None]:
        """Boot the instance (M5, §11.2) — wraps ``start.start``.

        Public Result boundary (Result-propagation sweep, Block S3): delegates to
        the existing mode-aware ``start.start`` (which dispatches on ``self._mode``
        internally: vm / pve-vm / pve / lxc), then self-updates the cached
        ``status`` from a fresh live probe (§10.2 — a lifecycle method DOES I/O and
        reflects the new state). ``start.start`` is idempotent (a no-op on an
        already-running instance), so a redundant ``start()`` is safe and the
        re-resolved status is still correct. On success returns ``Ok(None)``.

        Self-update via the Block-08 ``_resolve_status`` rather than a hard-coded
        ``Status.RUNNING``: re-resolving is the honest value (it picks up ORPHAN /
        UNKNOWN, and does not claim RUNNING if the boot silently failed to take),
        and it reuses the one status resolver instead of forking a second notion
        of "running".

        Any ``KentoError`` raised inside the body — ``_check_alive``'s
        ``InstanceNotFoundError`` on a dead handle, or a ``SubprocessError`` /
        ``StateError`` from the delegated ``start.start`` — is caught at this
        boundary and converted to an ``Error`` with the real kind (KIND-FIDELITY
        rule). ``start.start`` STAYS RAISING (internal control flow). A
        non-``KentoError`` is a panic and propagates.
        """
        try:
            self._check_alive()
            from kento import start as start_mod

            start_mod.start(self.name, container_dir=self._dir, mode=self._mode)
            self._status = _resolve_status(self._dir, self._mode)
            return Ok(value=None)
        except KentoError as exc:
            return _error_from(exc)

    # ------------------------------------------------------------------- #
    # M6 — stop: graceful-or-forced shutdown (§11.2, LOCKED).
    # ------------------------------------------------------------------- #
    # Default graceful grace window (seconds) when ``timeout`` is None (§11.2 M6).
    _STOP_DEFAULT_TIMEOUT = 15

    def stop(
        self, *, timeout: int | None = None, force: bool = False
    ) -> Result[None]:
        """Stop the instance (M6, §11.2 — LOCKED) — wraps ``stop.shutdown``.

        Public Result boundary (Result-propagation sweep, Block S3). Delivers the
        full LOCKED M6 semantics. The typed layer owns ALL the timing/decision
        logic; ``stop.shutdown`` supplies the per-mode graceful and forced
        primitives (its graceful path is now genuinely no-kill — ``--nokill`` on
        LXC, no-SIGKILL on VM, no ``--forceStop`` on PVE — so a stubborn guest is
        left running for our re-probe):

        * ``force=False`` — graceful only, NEVER hard-kills. Issue a graceful
          stop, then re-probe: if still running, the internal
          ``StopTimeout("cannot stop; try force")`` is raised and caught at this
          boundary → ``Error(STOP_TIMEOUT)`` (the instance is left up).
        * ``force=True``, ``timeout`` None/0 — immediate hard kill.
        * ``force=True``, ``timeout > 0`` — grace that long, THEN kill: a graceful
          stop, a bounded liveness poll up to ``timeout`` seconds, then a hard
          kill only if it is still up.

        ``timeout`` is the grace window; the post-elapse action differs — report
        (raise) vs kill — and the DEFAULT differs by case (§11.2 M6): graceful
        ``None`` => 15s; forced ``None`` => 0 (immediate). Self-updates ``status``
        from a fresh probe afterward (§10.2). On success returns ``Ok(None)``.

        The whole body is wrapped: the LOCKED graceful-timeout ``StopTimeout``
        (most-specific-first → ``STOP_TIMEOUT``, NOT its parent ``INVALID_STATE``),
        a ``SubprocessError`` from a failed kill, or ``_check_alive``'s
        ``InstanceNotFoundError`` are all caught here and converted to an ``Error``
        with the real kind (KIND-FIDELITY rule). ``stop.shutdown`` STAYS RAISING.
        """
        try:
            self._check_alive()
            from kento import stop as stop_mod

            # Default grace window. Graceful: None => 15s. Forced: None => 0
            # (immediate kill); only an explicit timeout>0 buys a forced grace
            # window.
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
                return Ok(value=None)

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
                    # Graceful only: NEVER kill -> report it is still running.
                    # Resolve status first so the handle reflects the
                    # still-running reality.
                    self._status = _resolve_status(self._dir, self._mode)
                    from kento.errors import StopTimeout

                    raise StopTimeout("cannot stop; try force")

            self._status = _resolve_status(self._dir, self._mode)
            return Ok(value=None)
        except KentoError as exc:
            return _error_from(exc)

    # ------------------------------------------------------------------- #
    # M7 — destroy: stop (if needed) + remove instance + writable layer (§11.2).
    # ------------------------------------------------------------------- #
    def destroy(self, *, force: bool = False) -> Result[None]:
        """Destroy the instance (M7, §11.2) — wraps ``destroy.destroy``.

        Public Result boundary (Result-propagation sweep, Block S3). Removes the
        instance and its writable layer, and releases this instance's OWN image
        hold (never the image — that is ``prune``'s job; ``destroy.py`` calls
        ``remove_image_hold`` for this guest only). ``force=True`` →
        force-stop-then-remove (``destroy.destroy`` hard-stops a running instance
        before removal); ``force=False`` on a running instance raises
        ``StateError`` (``destroy.destroy``'s guard) — caught here and returned as
        ``Error(INVALID_STATE)`` — rather than killing it. On success returns
        ``Ok(None)``.

        After a successful destroy the backing instance is gone, so the handle is
        marked DEAD: a subsequent lifecycle/refresh call on it returns
        ``Error(INSTANCE_NOT_FOUND)`` (via ``_check_alive`` at its own boundary)
        instead of acting on a removed directory. We do NOT self-update ``status``
        to a sentinel — there is no "destroyed" ``Status`` (the enum models a live
        instance's state); the dead flag is the honest signal that the handle is
        spent. ``_dead`` is set ONLY on the success path (inside the try, after the
        delegate returns) — a failed destroy leaves the handle alive, so the error
        path never marks it dead. ``destroy.destroy`` STAYS RAISING.
        """
        try:
            self._check_alive()
            from kento import destroy as destroy_mod

            destroy_mod.destroy(
                self.name, force, container_dir=self._dir, mode=self._mode,
            )
            # Only on success: the instance is gone, so the handle is spent.
            self._dead = True
            return Ok(value=None)
        except KentoError as exc:
            return _error_from(exc)

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
        ``reconcile.find_orphans`` scopes on. Delegates to :meth:`_namespace`,
        the shared class->namespace mapping.
        """
        return cls._namespace()

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
    def attach(self) -> Result[None]:
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
        enter``/``qm terminal`` after a manual detach is not a meaningful return
        value to the caller (a clean detach and a session that ran a failing last
        command are indistinguishable here). The spec locks ``-> None``, so the
        method does NOT return it. But the code is NOT thrown away (Jei-ruled M12
        refinement): it is CAPTURED on the handle as :attr:`attach_exit_code` (a
        read-only property, ``None`` until ``attach`` runs) and logged, so a
        caller that wants it can read it after the fact. A genuine *failure to
        attach at all* (no serial socket, not a tty, can't connect) is surfaced by
        ``attach.attach`` as a typed ``StateError`` — that propagates, it is not
        swallowed (and leaves ``attach_exit_code`` unchanged).

        Public Result boundary (Result-propagation sweep, Block S3): a destroyed
        handle (``_check_alive``'s ``InstanceNotFoundError``) or a genuine
        failure-to-attach (``attach.attach``'s ``StateError`` — no serial socket,
        not a tty, can't connect) is caught here and returned as an ``Error`` with
        the real kind; on a clean detach returns ``Ok(None)`` (the wrapped tool's
        exit code is captured on :attr:`attach_exit_code`, NOT in the Result —
        ``-> Result[None]`` preserves the locked int→None mapping). ``attach.attach``
        STAYS RAISING.
        """
        try:
            self._check_alive()
            from kento import attach as attach_mod

            # Run the interactive session. The wrapped tool's exit code is not the
            # method's RETURN value (§11.3: attach is a console, not a status
            # check), but we STORE it on the handle + log it (Jei-ruled refinement)
            # so it is not lost. Pass only the name — attach.attach re-resolves
            # across both namespaces; the dead-handle case is pre-empted by
            # _check_alive above.
            code = attach_mod.attach(self.name)
            self._attach_exit_code = code
            _instances_logger.info(
                "attach session for %s exited with code %s", self.name, code,
            )
            return Ok(value=None)
        except KentoError as exc:
            return _error_from(exc)

    @property
    def attach_exit_code(self) -> int | None:
        """The wrapped tool's exit code from the last :meth:`attach` (M12).

        ``None`` until ``attach`` has run on this handle; afterward it is the
        ``lxc-attach``/``pct enter``/``qm terminal``/serial-relay returncode from
        the most recent attach session. Read-only (getter-only — §11.2 M9). The
        code is informational: ``attach`` itself returns ``None`` (a console
        session is not a status check), but the code is preserved here for callers
        that want it (Jei-ruled M12 refinement).
        """
        return self._attach_exit_code


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
    def create(
        cls,
        name: str,
        image: "str | OciReference | Image",
        *,
        hostname: str | None = None,
        platform: PlatformProfile | None = None,
        mid: int | None = None,
        network: NetworkConnection | None = None,
        forwards: "Mapping[HostBinding, GuestTarget] | None" = None,
        resources: "Mapping[str, int] | None" = None,
        environment: "Mapping[str, str] | None" = None,
        start: bool = False,
        storage: StorageMode = StorageMode.OVERLAY,
        unprivileged: bool = False,
        nesting: bool = False,
        lxc_args: "Sequence[str]" = (),
        extra_args: "Sequence[str]" = (),
        searchdomain: str | None = None,
        timezone: str | None = None,
        ssh_keys: "Sequence[str] | None" = None,
        ssh_key_user: str = "root",
        ssh_host_keys: bool = False,
        ssh_host_key_dir: str | None = None,
        config_mode: str = "auto",
        force: bool = False,
    ) -> "SystemContainer":
        """Create a new LXC system container (M15, §11.4) — wraps ``create.create``.

        Decomposes the typed parameter objects into the flat ``create.py:create``
        keyword arguments (which returns ``None``), then re-snapshots the live
        instance via :meth:`get` (kind-checked) — the additive wrapper stance
        (§2): no live runtime logic is re-implemented here.

        The KIND fixes the base ``mode`` to ``"lxc"`` (``create.py`` promotes it
        to ``"pve"`` internally when ``platform`` selects PVE or auto-detection
        finds a PVE host); ``platform`` decomposes into ``create.py``'s ``pve``
        tri-state + ``vmid`` (JC2). ``image`` accepts a ``str`` / ``OciReference``
        / ``Image`` (rendered to the ref string create.py wants, JC3). ``network``
        / ``resources`` / ``environment`` decompose faithfully (JC5). ``storage``
        other than ``OVERLAY`` raises (JC4 — the only 1.0-supported backend; not
        silently ignored). ``unprivileged`` / ``lxc_args`` are the LXC-only params;
        ``extra_args`` is the PVE ``--pve-arg`` pass-through.

        The **create-time long tail** (§11.4 M15's enumerated ``# + timezone,
        ssh_user, …`` tail — Director-authorized): ``forwards`` (the typed
        port-forward map, §5.7, rendered to ``create.py``'s ``port`` spec list)
        plus the CREATE-input passthroughs ``searchdomain`` / ``timezone`` /
        ``ssh_keys`` / ``ssh_key_user`` / ``ssh_host_keys`` / ``ssh_host_key_dir``
        / ``config_mode`` / ``force``. Each defaults to ``create.py``'s own
        default, so leaving any unset is BYTE-IDENTICAL to before. ``searchdomain``
        is a create input, NOT a ``NetworkConnection`` field (§5.3).
        """
        from kento import create as create_mod

        kwargs = _build_create_kwargs(
            kind_mode="lxc",
            name=name,
            image=image,
            hostname=hostname,
            platform=platform,
            mid=mid,
            network=network,
            forwards=forwards,
            resources=resources,
            environment=environment,
            start=start,
            storage=storage,
            nesting=nesting,
            extra_args=extra_args,
            searchdomain=searchdomain,
            timezone=timezone,
            ssh_keys=ssh_keys,
            ssh_key_user=ssh_key_user,
            ssh_host_keys=ssh_host_keys,
            ssh_host_key_dir=ssh_host_key_dir,
            config_mode=config_mode,
            force=force,
        )
        kwargs["unprivileged"] = unprivileged
        kwargs["lxc_args"] = list(lxc_args) if lxc_args else None
        create_mod.create(**kwargs)
        return cls.get(name)

    @classmethod
    @contextmanager
    def transient(
        cls,
        name: str,
        image: "str | OciReference | Image",
        *,
        hostname: str | None = None,
        platform: PlatformProfile | None = None,
        mid: int | None = None,
        network: NetworkConnection | None = None,
        forwards: "Mapping[HostBinding, GuestTarget] | None" = None,
        resources: "Mapping[str, int] | None" = None,
        environment: "Mapping[str, str] | None" = None,
        start: bool = False,
        storage: StorageMode = StorageMode.OVERLAY,
        unprivileged: bool = False,
        nesting: bool = False,
        lxc_args: "Sequence[str]" = (),
        extra_args: "Sequence[str]" = (),
        searchdomain: str | None = None,
        timezone: str | None = None,
        ssh_keys: "Sequence[str] | None" = None,
        ssh_key_user: str = "root",
        ssh_host_keys: bool = False,
        ssh_host_key_dir: str | None = None,
        config_mode: str = "auto",
        force: bool = False,
    ) -> "Iterator[SystemContainer]":
        """Context-manager create for a throwaway container (M27, §11.4).

        Same parameters as :meth:`create` (including the create-time long tail);
        the handle is scoped to a ``with`` block and **guaranteed torn down on
        exit** — ``destroy(force=True)`` (M7's force-stop-then-remove) runs
        whether the block exits normally or via an exception. ``transient`` is
        the ONLY context-manager entry: a plain ``create``/``get`` handle is NOT
        a context manager (no ``__enter__``/``__exit__`` on ``Instance``), so
        ``with SystemContainer.create(...)`` raises ``TypeError`` — teardown
        happens iff the caller typed ``transient`` (JC6, footgun-free).
        """
        inst = cls.create(
            name, image,
            hostname=hostname, platform=platform, mid=mid, network=network,
            forwards=forwards, resources=resources, environment=environment,
            start=start, storage=storage, unprivileged=unprivileged,
            nesting=nesting, lxc_args=lxc_args, extra_args=extra_args,
            searchdomain=searchdomain, timezone=timezone, ssh_keys=ssh_keys,
            ssh_key_user=ssh_key_user, ssh_host_keys=ssh_host_keys,
            ssh_host_key_dir=ssh_host_key_dir, config_mode=config_mode,
            force=force,
        )
        try:
            yield inst
        finally:
            # ``destroy`` now returns a ``Result`` (Block S3); ``.unwrap()`` so a
            # FAILED teardown still RAISES out of this ``finally`` exactly as it
            # did before the conversion — a silently-discarded ``Error`` would
            # leak the transient instance (gate A/C).
            inst.destroy(force=True).unwrap()

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
    ) -> Result[int]:
        """Run ``command`` in the guest, returning its exit code (M13, §11.3).

        ON ``SystemContainer`` only (the base lacks it, and a VM has no in-guest
        agent — §11.3). Wraps ``exec_cmd.exec_cmd`` (the operator-authorized
        minimal touch threads ``tty``/``user``/``env`` through):

        * ``tty`` — best-effort (``lxc-attach``/``pct exec`` inherit a pty from
          this process's stdio; a pty cannot be fabricated when the caller has no
          terminal — see ``exec_cmd`` for the honest limit).
        * ``user`` — run as that guest user (``runuser -u <user> -- ``).
        * ``env`` — set in the guest (in-guest ``env K=V … `` prefix).

        Public Result boundary (Result-propagation sweep, Block S3): returns
        ``Ok(<exit code>)``, and an ``Ok`` carries the command's exit code even
        when it is NON-ZERO — a non-zero code does NOT raise and is NOT an
        ``Error`` (§11.9, M13: non-zero is normal information — ``grep`` returning
        1 is a result, not an exception — the caller decides what it means). Only a
        genuine inability to RUN the command (instance gone via ``_check_alive``,
        ``require_root``, empty command, or ``exec_cmd``'s ``ModeError`` backstop
        for vm/pve-vm — unreachable here since ``exec`` lives only on
        ``SystemContainer``) is a ``KentoError``, caught at this boundary and
        returned as an ``Error`` with the real kind. ``exec_cmd.exec_cmd`` STAYS
        RAISING; a non-``KentoError`` panics.
        """
        try:
            self._check_alive()
            from kento import exec_cmd as exec_cmd_mod

            return Ok(value=exec_cmd_mod.exec_cmd(
                self.name, list(command), tty=tty, user=user, env=env,
            ))
        except KentoError as exc:
            return _error_from(exc)

    # ------------------------------------------------------------------- #
    # M14 — logs: line-oriented journal stream (§11.3). ADDITIVE generator —
    # does NOT wrap ``logs.logs`` (that streams to stdout + returns int; it is
    # un-wrappable into an Iterator[str]). Reimplements the small LXC-only
    # mode-dispatch with PIPED stdout, like Block 07's direct-podman query.
    # ------------------------------------------------------------------- #
    def logs(
        self,
        *,
        follow: bool = False,
        lines: int | None = None,
        args: "Sequence[str]" = (),
    ) -> "Result[Iterator[str]]":
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

        ``args`` (Jei-ruled M14 refinement — preserve the CLI's journalctl
        pass-through): an OPTIONAL sequence of extra ``journalctl`` arguments
        appended verbatim after the ``follow``/``lines`` flags (e.g.
        ``("--since", "yesterday")``, ``("-u", "sshd")``). ``follow``/``lines``
        stay typed conveniences; with ``args=()`` (the default) the invocation is
        BYTE-IDENTICAL to before. ``args`` is the raw pass-through escape hatch for
        the full journalctl surface the typed flags do not model.

        ADDITIVE (does NOT touch ``logs.py``): ``logs.logs`` streams to inherited
        stdout and returns an int — un-wrappable into an iterator — so this
        reimplements the same LXC-only ``lxc-attach``/``pct exec journalctl``
        dispatch with PIPED stdout (mirroring how Block 07's ``prune`` queried
        podman directly). Lines are decoded UTF-8 with ``errors="replace"`` (a
        log line is human text; a stray non-UTF-8 byte must not crash the stream,
        and there is no raw-bytes API in 1.0 — §11.3 defers encoding).

        ``_check_alive`` runs eagerly (a destroyed handle's
        ``InstanceNotFoundError`` is caught at THIS boundary → ``Error`` at the
        call, not lazily on first ``next()``); the subprocess is spawned lazily
        inside the generator so the child's lifetime is bounded by iteration.

        Public Result boundary (Result-propagation sweep, Block S3): the EAGER
        ``KentoError``s — ``_check_alive``'s ``InstanceNotFoundError`` and
        ``_logs_argv``'s ``ValidationError`` for a negative ``lines`` — are caught
        here and returned as an ``Error`` with the real kind. The success value is
        the ``Iterator[str]`` itself, returned in ``Ok``. A subprocess-spawn
        failure occurs LAZILY inside the generator on first ``next()`` and is NOT
        caught here (the generator is the value, not part of this boundary's
        body) — the same lazy contract this method had before the conversion.
        """
        try:
            self._check_alive()
            argv = self._logs_argv(follow=follow, lines=lines, args=args)
            return Ok(value=_stream_lines(argv))
        except KentoError as exc:
            return _error_from(exc)

    def _logs_argv(
        self, *, follow: bool, lines: int | None, args: "Sequence[str]" = (),
    ) -> list[str]:
        """Build the host argv that runs ``journalctl`` in the guest (M14).

        Mirrors ``logs.py``'s LXC-only dispatch (plain-lxc ``lxc-attach -n
        <name>`` / pve-lxc ``pct exec <vmid>``), then appends ``journalctl`` with
        the args derived from ``follow``/``lines`` and finally the verbatim
        ``args`` pass-through:

        * ``follow=True``  -> ``-f`` (live tail; the open-iterator case).
        * ``lines`` set    -> ``-n <N>`` (tail the last N — the spec's ``lines=N``
          snapshot semantics; valid with or without ``-f``).
        * ``args``         -> appended verbatim (the journalctl pass-through).

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
        jargs += list(args)  # verbatim journalctl pass-through (Jei-ruled M14)

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
    def create(
        cls,
        name: str,
        image: "str | OciReference | Image",
        *,
        hostname: str | None = None,
        platform: PlatformProfile | None = None,
        mid: int | None = None,
        network: NetworkConnection | None = None,
        forwards: "Mapping[HostBinding, GuestTarget] | None" = None,
        resources: "Mapping[str, int] | None" = None,
        environment: "Mapping[str, str] | None" = None,
        start: bool = False,
        storage: StorageMode = StorageMode.OVERLAY,
        nesting: bool = False,
        qemu_args: "Sequence[str]" = (),
        kernel: "str | Path | None" = None,
        initramfs: "str | Path | None" = None,
        extra_args: "Sequence[str]" = (),
        searchdomain: str | None = None,
        timezone: str | None = None,
        ssh_keys: "Sequence[str] | None" = None,
        ssh_key_user: str = "root",
        ssh_host_keys: bool = False,
        ssh_host_key_dir: str | None = None,
        config_mode: str = "auto",
        force: bool = False,
    ) -> "VirtualMachine":
        """Create a new QEMU/KVM virtual machine (M16, §11.4) — wraps ``create.create``.

        Same decomposition shape as :meth:`SystemContainer.create`, with the VM
        differences (M16): the KIND fixes the base ``mode`` to ``"vm"``
        (``create.py`` promotes to ``"pve-vm"`` for PVE); there is NO
        ``unprivileged``/``lxc_args`` (VMs have their own isolation), and the
        backend pass-through is ``qemu_args`` (``--qemu-arg``) instead.
        ``kernel``/``initramfs`` are the OPT-IN boot-source override (§8 Phase A):
        LOCAL filesystem paths to a kernel / initramfs that supersede the
        in-image ``/boot`` for VM direct-kernel-boot, each side independent
        (``None`` → in-image fallback). They are COPIED into the instance state
        dir at create (reaped on destroy) and echoed on the resolved
        :attr:`image`. ``machine`` STAYS a non-param image-contract constant
        (M16, §11.0). ``storage`` other than ``OVERLAY`` raises (JC4). The
        create-time long tail
        (``forwards``/``searchdomain``/``timezone``/``ssh_*``/``config_mode``/
        ``force``) is the SAME as :meth:`SystemContainer.create`.
        """
        from kento import create as create_mod

        kwargs = _build_create_kwargs(
            kind_mode="vm",
            name=name,
            image=image,
            hostname=hostname,
            platform=platform,
            mid=mid,
            network=network,
            forwards=forwards,
            resources=resources,
            environment=environment,
            start=start,
            storage=storage,
            nesting=nesting,
            extra_args=extra_args,
            searchdomain=searchdomain,
            timezone=timezone,
            ssh_keys=ssh_keys,
            ssh_key_user=ssh_key_user,
            ssh_host_keys=ssh_host_keys,
            ssh_host_key_dir=ssh_host_key_dir,
            config_mode=config_mode,
            force=force,
        )
        kwargs["qemu_args"] = list(qemu_args) if qemu_args else None
        kwargs["kernel"] = str(kernel) if kernel is not None else None
        kwargs["initramfs"] = str(initramfs) if initramfs is not None else None
        create_mod.create(**kwargs)
        return cls.get(name)

    @classmethod
    @contextmanager
    def transient(
        cls,
        name: str,
        image: "str | OciReference | Image",
        *,
        hostname: str | None = None,
        platform: PlatformProfile | None = None,
        mid: int | None = None,
        network: NetworkConnection | None = None,
        forwards: "Mapping[HostBinding, GuestTarget] | None" = None,
        resources: "Mapping[str, int] | None" = None,
        environment: "Mapping[str, str] | None" = None,
        start: bool = False,
        storage: StorageMode = StorageMode.OVERLAY,
        nesting: bool = False,
        qemu_args: "Sequence[str]" = (),
        kernel: "str | Path | None" = None,
        initramfs: "str | Path | None" = None,
        extra_args: "Sequence[str]" = (),
        searchdomain: str | None = None,
        timezone: str | None = None,
        ssh_keys: "Sequence[str] | None" = None,
        ssh_key_user: str = "root",
        ssh_host_keys: bool = False,
        ssh_host_key_dir: str | None = None,
        config_mode: str = "auto",
        force: bool = False,
    ) -> "Iterator[VirtualMachine]":
        """Context-manager create for a throwaway VM (M27, §11.4).

        Same parameters as :meth:`create` (including the create-time long tail
        and the §8 Phase A ``kernel``/``initramfs`` boot-source override); the
        handle is ``with``-scoped and **guaranteed torn down on exit** via
        ``destroy(force=True)`` (normal OR exceptional). The ONLY context-manager
        entry — a plain ``create``/``get`` handle raises ``TypeError`` under
        ``with`` (JC6, §11.4).
        """
        inst = cls.create(
            name, image,
            hostname=hostname, platform=platform, mid=mid, network=network,
            forwards=forwards, resources=resources, environment=environment,
            start=start, storage=storage, nesting=nesting,
            qemu_args=qemu_args, kernel=kernel, initramfs=initramfs,
            extra_args=extra_args,
            searchdomain=searchdomain, timezone=timezone, ssh_keys=ssh_keys,
            ssh_key_user=ssh_key_user, ssh_host_keys=ssh_host_keys,
            ssh_host_key_dir=ssh_host_key_dir, config_mode=config_mode,
            force=force,
        )
        try:
            yield inst
        finally:
            # ``destroy`` now returns a ``Result`` (Block S3); ``.unwrap()`` so a
            # FAILED teardown still RAISES out of this ``finally`` exactly as it
            # did before the conversion — a silently-discarded ``Error`` would
            # leak the transient VM (gate A/C).
            inst.destroy(force=True).unwrap()

    # ------------------------------------------------------------------- #
    # M17 — suspend: pause vCPUs to RAM; VM-only; self-update status (§11.4).
    # ------------------------------------------------------------------- #
    def suspend(self) -> Result[None]:
        """Pause the VM's vCPUs to RAM (M17, §11.4) — wraps ``suspend.suspend``.

        Public Result boundary (Result-propagation sweep, Block S3). VM-only (this
        method lives only on ``VirtualMachine``). Wraps ``suspend.suspend`` (QMP
        ``stop`` for plain vm / ``qm suspend`` for pve-vm) — a *pause to RAM*, NOT
        a shutdown: the VM process keeps running and its memory is retained.
        ``suspend.suspend`` raises ``StateError`` if the VM is not running and
        ``SubprocessError`` if the QMP/qm call fails; both are caught at this
        boundary and returned as an ``Error`` with the real kind (no silent no-op,
        §2 principle 5). ``suspend.suspend`` STAYS RAISING. On success returns
        ``Ok(None)``.

        Self-updates ``status`` to ``SUSPENDED`` (M17 — brief JC4): on success we
        write the ``_status`` BACKING field directly (the public ``status`` is
        getter-only — §11.2 M9), INSIDE the try after the delegate returns so a
        failed suspend never mutates the cached status. Unlike ``start``/``stop``,
        we set the LITERAL ``Status.SUSPENDED`` rather than re-resolving via
        ``_resolve_status``, because that resolver CANNOT see SUSPENDED yet (it
        wraps a plain ``is_running`` bool that reports a paused VM as RUNNING —
        disclosed in ``_resolve_status``'s own docstring, §7.3). Re-resolving would
        incorrectly overwrite the just-set SUSPENDED with RUNNING; the literal is
        the honest cached value after a successful suspend.
        """
        try:
            self._check_alive()
            from kento import suspend as suspend_mod

            suspend_mod.suspend(self.name)
            self._status = Status.SUSPENDED
            return Ok(value=None)
        except KentoError as exc:
            return _error_from(exc)

    # ------------------------------------------------------------------- #
    # M18 — resume: un-pause vCPUs; VM-only; self-update status (§11.4).
    # ------------------------------------------------------------------- #
    def resume(self) -> Result[None]:
        """Un-pause the VM's vCPUs (M18, §11.4) — wraps ``suspend.resume``.

        Public Result boundary (Result-propagation sweep, Block S3). VM-only.
        Wraps ``suspend.resume`` (QMP ``cont`` / ``qm resume``). Mirrors
        :meth:`suspend`: ``suspend.resume`` raises ``StateError`` (not running) /
        ``SubprocessError`` (call failed); both are caught here and returned as an
        ``Error`` with the real kind. ``suspend.resume`` STAYS RAISING. On success
        returns ``Ok(None)``.

        Self-updates ``status`` to ``RUNNING`` (M18 — brief JC4) by writing the
        ``_status`` backing field with the LITERAL ``Status.RUNNING`` (inside the
        try after the delegate returns) — the same rationale as ``suspend``:
        ``_resolve_status`` already reports a paused VM as RUNNING, so a literal
        RUNNING after a successful un-pause is correct and avoids a redundant
        probe.
        """
        try:
            self._check_alive()
            from kento import suspend as suspend_mod

            suspend_mod.resume(self.name)
            self._status = Status.RUNNING
            return Ok(value=None)
        except KentoError as exc:
            return _error_from(exc)


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
# Typed create-args -> create.py:create(**kwargs) decomposition (M15/M16, §11.4).
#
# The concrete ``create`` is an ADDITIVE wrapper (§2): decompose the typed
# parameter objects into the flat ``create.py:create`` keyword args, call it
# (it returns None), then re-snapshot via ``cls.get(name)``. These helpers do
# the typed->flat mapping; getting it right is the crux (JC2/JC3/JC5) — a wrong
# mapping silently mis-creates. ``create.py`` then validates + builds.
# --------------------------------------------------------------------------- #


def _image_to_ref(image: "str | OciReference | Image") -> str:
    """Render an ``image`` argument to the ref string ``create.py`` wants (JC3).

    Accepts (lean surface, §11.4):

    * ``str``         — passed VERBATIM. ``create.py`` resolves/pulls it through
      podman exactly as the CLI does (we do NOT re-parse-then-render a string —
      that would normalize/alter what the caller typed; the str is the create
      boundary's own input, §2 principle 3 keeps parsing where it already lives).
    * ``OciReference`` — rendered via :meth:`OciReference.render` (the canonical
      reference string; never re-split by hand, §2 principle 3).
    * ``Image``        — a resolved image handle; we render its ``source``
      locator (``OciReference.render``). This lets a caller pass an
      ``OciImage`` it already pulled without re-typing the ref.

    Any other type is a typed ``ValidationError`` (§2 principle 5 — a clear
    boundary error, not a stringify-and-hope).
    """
    from kento.errors import ValidationError

    if isinstance(image, str):
        return image
    if isinstance(image, OciReference):
        return image.render()
    if isinstance(image, Image):
        return image.source.render()
    raise ValidationError(
        f"image must be a str, OciReference, or Image; got "
        f"{type(image).__name__}."
    )


def _platform_to_create_args(
    platform: PlatformProfile | None, mid: int | None,
) -> "tuple[bool | None, int]":
    """Decompose ``platform`` + ``mid`` into ``create.py``'s ``pve`` + ``vmid`` (JC2).

    ``create.py:create`` takes a BASE ``mode`` (``"lxc"``/``"vm"`` — supplied by
    the KIND) plus a ``pve`` TRI-STATE that it resolves internally (§6, create.py
    PVE-promotion block):

    * ``platform=None``           -> ``pve=None``  (let create.py AUTO-DETECT a
      PVE host — its current default behavior; the typed default preserves it).
    * ``PlatformMode.STANDARD``   -> ``pve=False`` (force NON-PVE; create.py does
      no promotion).
    * ``PlatformMode.PVE``        -> ``pve=True``  (force PVE; create.py raises
      ``ModeError`` if the host is not actually PVE — surfaced, not swallowed).

    ``vmid`` comes from ``mid`` (the top-level param) OR ``platform.mid``; ``mid``
    (if given) takes precedence, else ``platform.mid``, else ``0`` (= create.py's
    auto-allocate sentinel). A conflicting ``mid`` vs ``platform.mid`` is a typed
    ``ValidationError`` rather than a silent pick (§2 principle 5).
    """
    from kento.errors import ValidationError

    profile_mid = platform.mid if platform is not None else None
    if mid is not None and profile_mid is not None and mid != profile_mid:
        raise ValidationError(
            f"conflicting instance id: mid={mid} but platform.mid="
            f"{profile_mid}. Pass the id in exactly one place."
        )
    chosen_mid = mid if mid is not None else profile_mid
    vmid = chosen_mid if chosen_mid is not None else 0

    if platform is None:
        return None, vmid
    if platform.mode is PlatformMode.PVE:
        return True, vmid
    return False, vmid


def _network_to_create_params(
    conn: NetworkConnection | None, *, kind_mode: str,
) -> "dict[str, object]":
    """Decompose a ``NetworkConnection`` into ``create.py`` net params (JC5, §5).

    ``create.py:create`` takes ``net_type`` (``bridge``/``host``/``usermode``/
    ``none``) + ``bridge`` (L2) + ``ip``/``gateway``/``dns``/``searchdomain`` +
    ``mac`` (NOT ``set_cmd``'s ``network=`` string — the create boundary's own
    param shape). ``conn=None`` passes NOTHING, leaving ``create.py`` to
    auto-detect a bridge (its current default — the same behavior as ``kento
    create`` with no ``--network``):

    * ``DHCP``     -> ``net_type="bridge"`` (+ bridge if named); lease supplies L3.
    * ``STATIC``   -> ``net_type="bridge"`` (+ bridge) + ``ip=address[/subnet]``
      + ``gateway`` + ``dns`` (from ``ip_config``).
    * ``USER``     -> ``net_type="usermode"``.
    * ``HOST``     -> ``net_type="host"``.
    * ``DISABLED`` -> ``net_type="none"``.

    ``mac`` (``link_config[mac]``) is VM-only: ``create.py`` writes ``kento-mac``
    only for ``vm``/``pve-vm`` and SILENTLY IGNORES it on LXC. So a mac on an LXC
    create is REJECTED here with ``ValidationError`` (gate C — symmetry with the
    Block-11 setter, which raises ``ModeError`` for mac-on-LXC; silently dropping
    an explicitly-set mac would be data loss). On VM modes the mac is passed
    through. ``dns2`` cannot round-trip (``create.py`` writes a single ``dns=``
    line) — a value carrying ``dns2`` is REJECTED with ``ValidationError`` rather
    than silently lost (mirrors the Block-11 setter decomposition).
    ``searchdomain`` is not in the typed model (§5.3) and is intentionally absent.
    """
    from kento.errors import ValidationError

    if conn is None:
        return {}

    if "dns2" in conn.ip_config:
        raise ValidationError(
            "NetworkConnection.ip_config carries 'dns2', but create persists a "
            "single DNS server. Drop 'dns2' (a second resolver is not yet "
            "round-trippable through create)."
        )

    bridge = conn.link_config.get("bridge")
    mac = conn.link_config.get("mac")
    if mac is not None and not _is_vm_mode(kind_mode):
        raise ValidationError(
            "a MAC address is VM-only (link_config['mac'] is not applicable to "
            "a SystemContainer's plain-LXC NIC). Drop 'mac' from link_config "
            "for an LXC create."
        )
    params: dict[str, object] = {}

    if conn.mode is NetworkMode.STATIC:
        params["net_type"] = "bridge"
        if bridge:
            params["bridge"] = bridge
        address = conn.ip_config.get("address")
        subnet = conn.ip_config.get("subnet")
        if address is not None:
            params["ip"] = f"{address}/{subnet}" if subnet else address
        if conn.ip_config.get("gateway"):
            params["gateway"] = conn.ip_config["gateway"]
        if conn.ip_config.get("dns1"):
            params["dns"] = conn.ip_config["dns1"]
    elif conn.mode is NetworkMode.DHCP:
        params["net_type"] = "bridge"
        if bridge:
            params["bridge"] = bridge
    else:
        params["net_type"] = {
            NetworkMode.USER: "usermode",
            NetworkMode.HOST: "host",
            NetworkMode.DISABLED: "none",
        }[conn.mode]

    if mac is not None:
        params["mac"] = mac
    return params


def _resources_to_create_params(
    resources: "Mapping[str, int] | None",
) -> "dict[str, object]":
    """Decompose the ``resources`` bag into ``create.py``'s ``memory``/``cores``.

    ``memory`` (MiB) and ``cores`` map to the ``create.py`` scalar params; any
    other key is rejected (the bag is open at the type level, §2 principle 8, but
    only memory/cores are creatable — an unknown key would silently vanish, so we
    fail loud per §2 principle 5). A non-int value is likewise a typed
    ``ValidationError``. ``None``/absent => the param stays ``None`` (create.py's
    own default sizing applies).
    """
    from kento.errors import ValidationError

    if resources is None:
        return {}
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


def _environment_to_env_list(
    environment: "Mapping[str, str] | None",
) -> "list[str] | None":
    """Decompose the ``environment`` map into ``create.py``'s ``env`` list (§11.0).

    ``create.py`` takes ``env`` as a list of ``"KEY=VALUE"`` strings (it validates
    each — embedded newline / missing ``=`` / bad key — before any write). We
    render the dict into that shape; ``None``/empty => ``None`` (no env file).
    """
    if not environment:
        return None
    return [f"{key}={value}" for key, value in environment.items()]


def _forwards_to_create_port(
    forwards: "Mapping[HostBinding, GuestTarget] | None",
) -> "list[str] | None":
    """Render the typed ``forwards`` map into ``create.py``'s ``port`` list (§5.7).

    ``create.py:create`` accepts ``port`` as ``str | list[str] | None`` (Block 14
    made it accept a list) — the §5.7A spec strings (``hostPort:guestPort`` /
    ``...:guestPort/udp``) it writes to ``kento-port``. We render each typed
    binding back to its canonical spec via ``render_forward_spec`` (the SAME
    renderer the live ``forwards`` setter + ``set_cmd`` use, so the on-disk form
    is identical regardless of the entry point). ``None``/empty => ``None`` (no
    port file). An address-carrying binding raises ``ForwardAddressNotImplemented``
    inside ``render_forward_spec`` (1.0 has no per-address bind), surfaced at the
    create boundary rather than silently dropped (gate C).
    """
    if not forwards:
        return None
    from kento._network import render_forward_spec

    return [render_forward_spec(binding, target)
            for binding, target in forwards.items()]


def _resolve_extra_args(
    extra_args: "Sequence[str]", platform: PlatformProfile | None,
) -> "list[str] | None":
    """Resolve the ``--pve-arg`` pass-through from the param + ``platform`` (JC2).

    Both the M15/M16 top-level ``extra_args`` param AND ``PlatformProfile.
    extra_args`` feed ``create.py``'s ``pve_args`` (§6 == M15 — they are the same
    ``--pve-arg`` axis). Mirror the ``mid`` conflict logic so neither source is
    silently lost (gate C):

    * only the top-level param set      -> use it;
    * only ``platform.extra_args`` set  -> use it (else a loaded profile's
      pve-args would be silently DROPPED — exactly the footgun ``mid`` guards);
    * BOTH set and they DIFFER          -> ``ValidationError`` (pass in one place);
    * both set and EQUAL                -> use the (identical) value;
    * neither set                       -> ``None`` (no pve-args).
    """
    from kento.errors import ValidationError

    param = list(extra_args)
    profile = list(platform.extra_args) if platform is not None else []
    if param and profile and param != profile:
        raise ValidationError(
            f"conflicting --pve-arg pass-through: extra_args={param} but "
            f"platform.extra_args={profile}. Pass it in exactly one place."
        )
    chosen = param or profile
    return chosen or None


def _build_create_kwargs(
    *,
    kind_mode: str,
    name: str,
    image: "str | OciReference | Image",
    hostname: str | None,
    platform: PlatformProfile | None,
    mid: int | None,
    network: NetworkConnection | None,
    resources: "Mapping[str, int] | None",
    environment: "Mapping[str, str] | None",
    start: bool,
    storage: StorageMode,
    nesting: bool,
    extra_args: "Sequence[str]",
    forwards: "Mapping[HostBinding, GuestTarget] | None" = None,
    searchdomain: str | None = None,
    timezone: str | None = None,
    ssh_keys: "Sequence[str] | None" = None,
    ssh_key_user: str = "root",
    ssh_host_keys: bool = False,
    ssh_host_key_dir: str | None = None,
    config_mode: str = "auto",
    force: bool = False,
) -> "dict[str, object]":
    """Build the shared ``create.py:create(**kwargs)`` arg set (M15/M16).

    The common decomposition for both kinds — ``kind_mode`` is the base mode the
    KIND supplies (``"lxc"``/``"vm"``); the per-kind backend pass-through
    (``lxc_args``/``unprivileged`` vs ``qemu_args``) is added by the caller.

    ``storage`` other than ``OVERLAY`` RAISES (JC4): ``create.py`` has no storage
    param (OVERLAY is the only 1.0 backend = a no-op), so a non-OVERLAY value
    would otherwise be silently ignored. We reject it up front (§2 principle 5;
    gate C) rather than create something the caller didn't ask for.

    The **create-time long tail** (§11.4 M15's enumerated ``# + timezone,
    ssh_user, …`` tail — Director-authorized run 33+): ``forwards`` (port
    forwards, rendered to ``create.py``'s ``port`` spec list — §5.7), plus the
    CREATE-input passthroughs ``searchdomain`` / ``timezone`` / ``ssh_keys`` /
    ``ssh_key_user`` / ``ssh_host_keys`` / ``ssh_host_key_dir`` / ``config_mode``
    / ``force``. These are forwarded verbatim to ``create.py:create``; each
    defaults to ``create.py``'s OWN default, so leaving them unset is BYTE-
    IDENTICAL to before they existed. ``searchdomain`` is intentionally a create
    INPUT, not a ``NetworkConnection`` field (the model dropped it, §5.3) — no
    model conflict, no capability regression.
    """
    if storage is not StorageMode.OVERLAY:
        raise NotImplementedError(
            f"storage={storage!r} is not supported in 1.0; only "
            f"StorageMode.OVERLAY (the default) is available."
        )

    pve, vmid = _platform_to_create_args(platform, mid)
    kwargs: dict[str, object] = {
        "image": _image_to_ref(image),
        "name": name,
        "hostname": hostname,
        "mode": kind_mode,
        "pve": pve,
        "vmid": vmid,
        "start": start,
        "nesting": nesting,
        "env": _environment_to_env_list(environment),
        "pve_args": _resolve_extra_args(extra_args, platform),
        # Create-time long tail (forwarded verbatim; create.py-default when unset).
        "port": _forwards_to_create_port(forwards),
        "searchdomain": searchdomain,
        "timezone": timezone,
        "ssh_keys": list(ssh_keys) if ssh_keys else None,
        "ssh_key_user": ssh_key_user,
        "ssh_host_keys": ssh_host_keys,
        "ssh_host_key_dir": ssh_host_key_dir,
        "config_mode": config_mode,
        "force": force,
    }
    kwargs.update(_network_to_create_params(network, kind_mode=kind_mode))
    kwargs.update(_resources_to_create_params(resources))
    return kwargs


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


def _try_resolve_other_kind(name: str) -> Instance | None:
    """Resolve ``name`` across BOTH namespaces, returning the loaded snapshot.

    Used by a subclass ``get`` AFTER its own namespace missed, to discover
    whether ``name`` exists as the OTHER kind so the spec-required kind-mismatch
    message can be raised (rather than a bare not-found). Returns the loaded
    ``Instance`` if it exists in either namespace, or ``None`` if it is genuinely
    absent. An ``ambiguous`` (dup-name) case cannot occur here — the subclass
    namespace-scoped resolve already matched its own kind on a dup — but if it
    somehow did, treat it as "not the other kind" (``None``) and let the original
    not-found surface.
    """
    from kento import KentoError, resolve_any

    try:
        container_dir, mode = resolve_any(name)
    except KentoError:
        # Not found, or ambiguous: no single other-kind instance to name.
        return None
    return _load_snapshot(container_dir, mode)


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
    # † hostname: load the kento-hostname key, fallback to name. The create-WRITE
    # back-fill is now done (Block 12, authorized run 33); the read-fallback stays
    # correct — a pre-back-fill instance has no hostname key, so name is the honest
    # value (§11.0 †). The key is ``kento-hostname`` — the SAME key create writes
    # and ``set_cmd`` reads/writes (``_read_line``/``_write_meta`` prefix
    # ``kento-``), so create/set/read all agree (Block-08 read the bare
    # ``hostname`` file, which neither create nor set ever wrote — a latent seam
    # this block closes alongside the create back-fill it enables).
    inst._hostname = _read_meta(container_dir, "kento-hostname") or name
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
    inst._hold = _load_hold(name)
    # Overlay paths (§12.2): resolve the ``kento-state`` redirect ONCE and cache
    # both derived paths, so ``upper``/``work`` are I/O-free on access (principle
    # 2). state_dir = the redirect if present, else the container dir — the SAME
    # derivation the legacy ``info`` wire uses (info.py: state_text or dir).
    state_text = _read_meta(container_dir, "kento-state")
    state_dir = Path(state_text) if state_text else container_dir
    inst._upper = state_dir / "upper"
    inst._work = state_dir / "work"

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
    return (OciReference.parse(image).unwrap(),)


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


# --------------------------------------------------------------------------- #
# Live-forward appliers (§5.7C) — the per-mode add/remove primitives the running
# ``forwards`` setter drives. Each exposes ``add(binding, target)`` /
# ``remove(binding, target)`` for ONE forward; the engine composes the diff and
# the catch-reverse undo stack on top. Bridged uses the host firewall
# (portfwd.run_install/run_remove) against the live-resolved guest IP; VM-usermode
# uses QMP hostfwd_add/remove over qmp.sock. The actual rule commands live in
# ``kento.portfwd`` (the SINGLE source the parity test pins to the hook).
# --------------------------------------------------------------------------- #


class _BridgedForwardsApplier:
    """Live nft/iptables DNAT applier for a running container (lxc / pve-lxc).

    Resolves the guest IP ONCE at construction (static ``kento-net`` fast path,
    else ``lxc-info -n <name> -iH`` — the same resolution the boot hook uses) and
    the NAT backend once (nft -> iptables -> none, mirroring
    ``hook.sh:kento_nat_backend``). If the guest IP cannot be determined (DHCP not
    yet settled) or no backend is present, the FIRST ``add``/``remove`` raises a
    ``StateError``/``SubprocessError`` — surfaced by the engine and rolled back —
    rather than silently persisting-only behind the caller's back (§2 principle 5:
    a running bridged set MUST apply live or fail honestly).
    """

    def __init__(self, inst: "Instance") -> None:
        self._inst = inst
        self._name = inst._name
        self._ip = _resolve_guest_ip(inst._dir)
        from kento.portfwd import resolve_backend

        self._backend = resolve_backend()

    def _require_ready(self) -> tuple[str, str]:
        from kento.errors import StateError, SubprocessError

        if self._ip is None:
            raise StateError(
                f"cannot apply port forwards live to running '{self._name}': "
                "its guest IP is not yet known (DHCP may not have settled). "
                "Retry once the instance has an address, or stop it first so the "
                "change persists and applies at next boot."
            )
        if self._backend is None:
            raise SubprocessError(
                "cannot apply port forwards live: neither nft nor iptables is "
                "available on the host to install DNAT rules."
            )
        return self._backend, self._ip

    def add(self, binding: "HostBinding", target: "GuestTarget") -> None:
        backend, ip = self._require_ready()
        from kento.portfwd import run_install

        protocol, _haddr, host_port = binding
        _gaddr, guest_port = target
        run_install(backend, ip, protocol, host_port, guest_port, self._name)

    def remove(self, binding: "HostBinding", target: "GuestTarget") -> None:
        backend, _ip = self._require_ready()
        from kento.portfwd import run_remove

        protocol, _haddr, host_port = binding
        run_remove(backend, protocol, host_port, self._name)


class _QmpForwardsApplier:
    """Live QMP ``hostfwd_add``/``hostfwd_remove`` applier for a usermode VM.

    Issues the HMP forward commands over ``qmp.sock`` (the existing QMP monitor
    socket). A missing socket / non-usermode VM surfaces as ``SubprocessError``
    on the first op (the VM has no slirp netdev to mutate), surfaced and rolled
    back by the engine. Bridged-VM forwarding is out of scope (§5.7C).
    """

    def __init__(self, sock_path: Path, name: str) -> None:
        self._sock = sock_path
        self._name = name

    def _require_sock(self) -> Path:
        from kento.errors import SubprocessError

        if not self._sock.exists():
            raise SubprocessError(
                f"cannot apply port forwards live to running '{self._name}': "
                f"QMP socket not found ({self._sock}). The VM is not running with "
                "usermode networking, or predates QMP support."
            )
        return self._sock

    def add(self, binding: "HostBinding", target: "GuestTarget") -> None:
        sock = self._require_sock()
        from kento.portfwd import vm_hostfwd_add

        protocol, _haddr, host_port = binding
        _gaddr, guest_port = target
        vm_hostfwd_add(sock, protocol, host_port, guest_port)

    def remove(self, binding: "HostBinding", target: "GuestTarget") -> None:
        sock = self._require_sock()
        from kento.portfwd import vm_hostfwd_remove

        protocol, _haddr, host_port = binding
        vm_hostfwd_remove(sock, protocol, host_port)


# --------------------------------------------------------------------------- #
# Live-resources appliers (Block 16, §11.2 M9) — the per-mode primitive the
# running ``resources`` setter drives. Each exposes ``apply(key, value)`` for ONE
# knob ("memory" MiB / "cores" count); the engine composes the both-or-neither
# undo stack on top. Plain LXC writes the running container's cgroup-v2 knobs via
# ``lxc-cgroup``; pve-lxc uses ``pct set`` (PVE's live-capable path). The byte
# values MIRROR what create/boot writes (set_cmd.py:_apply_lxc / pve.py): memory
# -> ``memory.max`` = MiB*1048576 bytes; cores -> ``cpu.max`` = "N*100000 100000"
# CFS quota; pve cores -> cpuset (``cores``) + ``cpulimit`` (-> cpu.max quota).
# --------------------------------------------------------------------------- #

# MiB -> bytes (the create/hook factor for lxc.cgroup2.memory.max).
_MIB_BYTES = 1048576
# CFS period the runtime pairs with the cores*period quota for cpu.max.
_CFS_PERIOD = 100000


class _LxcResourcesApplier:
    """Live cgroup-v2 applier for a running PLAIN LXC container (Block 16).

    Writes the running container's cgroup knobs via ``lxc-cgroup -n <name> KEY
    VALUE`` — the canonical liblxc tool (same lxc-utils package as the
    ``lxc-info``/``lxc-attach`` the runtime already requires). The KEY/VALUE pairs
    are the EXACT cgroup-v2 knobs the boot ``config`` + start-host hook use:

    * ``memory`` (MiB) -> ``memory.max`` = ``MiB * 1048576`` (bytes).
    * ``cores`` (count) -> ``cpu.max`` = ``"cores*100000 100000"`` (CFS quota /
      period) — matching ``set_cmd._apply_lxc`` / ``hook.sh`` exactly.

    A failed write (tool absent, value rejected) raises ``SubprocessError`` via
    ``run_or_die`` — surfaced by the engine and rolled back (§2 principle 5: a
    running set MUST apply live or fail honestly, never silently persist-only).
    """

    def __init__(self, name: str) -> None:
        self._name = name

    def apply(self, key: str, value: int) -> None:
        from kento.subprocess_util import run_or_die

        if key == "memory":
            cgroup_key = "memory.max"
            cgroup_val = str(int(value) * _MIB_BYTES)
        elif key == "cores":
            cgroup_key = "cpu.max"
            cgroup_val = f"{int(value) * _CFS_PERIOD} {_CFS_PERIOD}"
        else:  # pragma: no cover — engine only passes memory/cores
            raise ValueError(f"unsupported live resource knob {key!r}")
        run_or_die(
            ["lxc-cgroup", "-n", self._name, cgroup_key, cgroup_val],
            f"apply {key} live to running container",
            name=self._name,
        )


class _PveLxcResourcesApplier:
    """Live applier for a running pve-lxc container via ``pct set`` (Block 16).

    pve-lxc has no plain-LXC ``config``: the source of truth is the PVE ``.conf``,
    and ``pct set`` is PVE's live-capable mutation path (it rewrites the conf AND
    hotplugs the running container's cgroup). The mapping MIRRORS the create-time
    one (``pve.py:generate_pve_config``):

    * ``memory`` (MiB) -> ``pct set <vmid> -memory <MiB>`` (live cgroup
      ``memory.max``).
    * ``cores`` (count) -> ``pct set <vmid> -cores <N> -cpulimit <N>``: ``cores``
      is the cpuset (which CPUs) and ``cpulimit`` is the quota that drives
      ``cpu.max`` — kento sets BOTH at create, so we set both here to keep the
      live + persisted state coherent with a freshly-created CT. (DISCLOSED JC2:
      ``pct set`` applies memory + cpulimit live on a running CT; ``cores``
      cpuset likewise. This is genuinely live, not next-start-only.)

    Each knob is a SEPARATE ``pct set`` so the engine can roll back exactly the
    one(s) it applied. A failed ``pct set`` raises ``SubprocessError`` via
    ``run_or_die`` — surfaced + rolled back by the engine.

    Note: ``pct set`` rewrites the conf, dropping kento's raw ``lxc.cgroup2.*``
    lines; the engine's post-apply ``_persist_resources`` (``_apply_pve_lxc``)
    re-writes the canonical kento conf, restoring them for the next boot.
    """

    def __init__(self, container_dir: Path, name: str) -> None:
        self._dir = container_dir
        self._name = name

    def _vmid(self) -> str:
        # pve-lxc vmid is the container dir name (mirrors set_cmd:_pve_lxc_conf
        # _path's fallback + the boot hook's CONTAINER_ID); a kento-vmid file
        # overrides it if present (defensive parity with set_cmd).
        vmid_file = self._dir / "kento-vmid"
        if vmid_file.is_file():
            text = vmid_file.read_text().strip()
            if text:
                return text
        return self._dir.name

    def apply(self, key: str, value: int) -> None:
        from kento.subprocess_util import run_or_die

        vmid = self._vmid()
        if key == "memory":
            args = ["-memory", str(int(value))]
        elif key == "cores":
            args = ["-cores", str(int(value)), "-cpulimit", str(int(value))]
        else:  # pragma: no cover — engine only passes memory/cores
            raise ValueError(f"unsupported live resource knob {key!r}")
        run_or_die(
            ["pct", "set", vmid, *args],
            f"apply {key} live to running container",
            name=self._name,
        )


def _resolve_guest_ip(container_dir: Path) -> str | None:
    """Resolve a running container's guest IPv4 the way the boot hook does (§5.7C).

    Static fast path: ``kento-net`` ``ip=`` (decompose the CIDR -> bare address).
    Else ``lxc-info -n <name> -iH`` filtered to the first IPv4 (the DHCP path the
    hook polls). Returns ``None`` if no address is known (DHCP not yet settled /
    tool absent) — the caller decides (error on a running bridged set, never a
    silent persist-only). The container directory name is the lxc name (plain
    LXC) or the vmid (pve-lxc), which is exactly what ``lxc-info`` expects on PVE.
    """
    net_file = container_dir / "kento-net"
    if net_file.is_file():
        for line in net_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("ip="):
                addr = line[len("ip="):].split("/", 1)[0].strip()
                if addr:
                    return addr
    try:
        result = subprocess.run(
            ["lxc-info", "-n", container_dir.name, "-iH"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for token in result.stdout.split():
        # IPv4 only — our DNAT rules live in the ip family (the hook ignores the
        # interleaved IPv6 addresses lxc-info prints). A plain dotted quad.
        parts = token.split(".")
        if len(parts) == 4 and all(p.isdigit() for p in parts):
            return token
    return None


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


def _load_hold(name: str) -> "Hold | None":
    """Load THIS instance's own prune-protection hold, or ``None`` (§12.3).

    The instance's hold is the stopped podman container ``kento-hold.<name>``.
    This is a SINGLE targeted ``podman container inspect`` for that one hold
    (NOT ``Hold.list()`` filtered) so that ``Instance.list()`` of N instances
    stays O(N) podman calls, not O(N²) — eager-loading per principle 2 without
    making the collection path quadratic (brief JC1).

    Faithful to BOTH hold shapes (§2 principle 1), via the SAME parsing the
    global ``Hold.list()`` uses:

    * a non-empty ``io.kento.hold-image-id`` label → MODERN id-pin → ``Digest``
      (``_digest_from_podman_id``);
    * else the ``.Image`` field → LEGACY pin → ``OciReference`` (or a ``Digest``
      for a bare-id ``.Image``, ``_parse_legacy_pinned`` / JC3).

    TOTAL: no hold (the inspect fails — most instances have a hold, but an
    adopted/legacy/pre-hold guest may not) → ``None``; a hold with neither an
    id label nor a ``.Image`` → ``None`` with a log (no faithful pin to build,
    mirroring ``Hold.list``'s skip-and-log). Reuses the existing
    ``layers._hold_pinned_id`` id-label reader; the ``.Image`` is read with one
    more inspect only on the legacy branch (the common modern path is one call).
    """
    from kento._images import Hold, _digest_from_podman_id, _parse_legacy_pinned
    from kento.layers import _hold_pinned_id, _podman_cmd

    hold_name = f"kento-hold.{name}"
    image_id = _hold_pinned_id(name)
    if image_id:
        return Hold(instance=name, pinned=_digest_from_podman_id(image_id))

    # No id label → legacy (or no hold at all). Read the hold's .Image; a failed
    # inspect (no such container) means this instance has no hold → None.
    try:
        result = subprocess.run(
            [*_podman_cmd(), "container", "inspect", hold_name,
             "--format", "{{.Image}}"],
            capture_output=True, text=True,
        )
    except Exception:  # noqa: BLE001 — a missing podman is "no observable hold"
        return None
    if result.returncode != 0:
        return None
    image = result.stdout.strip()
    if not image or image == "<no value>":
        _instances_logger.warning(
            "hold %s has no image-id label and no .Image; treating as no hold",
            hold_name,
        )
        return None
    return Hold(instance=name, pinned=_parse_legacy_pinned(image))


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
