"""Platform & lifecycle-status value types — ``PlatformProfile`` + ``Status``.

These are **pure, inert, frozen value types** (spec §2 principle 2): no I/O,
ever. Once constructed, a value is plain data you can pass, copy, and reason
about. They are the scalar value axes that become **fields on the base
``Instance``** — ``platform_profile: PlatformProfile`` (§6) and ``status:
Status`` (§7) — but the resolvers that READ the environment to build them
(``/etc/pve`` detection, ``resolve_status``, ``_pve_node_name``) are runtime
I/O on a handle and live in a later block; this module ships only the inert
types they hand back.

The public surface (``PlatformMode``, ``PlatformProfile``, ``Status``) is
re-exported flat from ``kento`` — refer to ``kento.PlatformProfile``, not
``kento._platform.PlatformProfile``.

Spec: ``~/workspace/kento-core-api-design.md`` §6 (PlatformProfile) and §7
(Status). We model the two orthogonal axes every instance kind shares — *where*
it runs (§6) and its *lifecycle state* (§7) — as flat discriminators, not
variant trees (§2 principle 8; the same posture as ``NetworkConnection`` §5 and
``PlatformMode``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from kento.errors import ValidationError

__all__ = [
    "PlatformMode",
    "PlatformProfile",
    "Status",
]


# --------------------------------------------------------------------------- #
# PlatformMode — the orthogonal "where it runs" axis (§6.1).
#
# Subclasses ``str`` so members compare/serialize as their wire value
# (``PlatformMode.PVE == "pve"``) — the same ``str, Enum`` idiom the spec writes
# verbatim and the rest of the library uses for closed value sets (NetworkMode,
# Status, StorageMode). The value IS the string the rest of kento keys on:
# ``standard`` = plain lxc/vm (no orchestrator), ``pve`` = the ``/etc/pve``
# Proxmox detection (§6.3).
# --------------------------------------------------------------------------- #


class PlatformMode(str, Enum):
    """Where an instance runs — a plain host or under Proxmox VE (§6.1).

    A flat binary axis, not a ``Standard | Pve`` sum (§6.2, principle 8): a
    closed two-mode axis reads as a discriminator, not a variant tree. The
    backend/workload axis (SystemContainer / VirtualMachine / AppContainer) is
    the instance **class**, orthogonal to this platform axis (§6).
    """

    STANDARD = "standard"  # plain lxc / plain vm — no orchestrator
    PVE = "pve"            # Proxmox VE — pct/qm + pmxcfs


# PVE reserves vmids 1-99 for its own internal use; a kento-managed PVE instance
# always carries a ``mid`` >= this floor. Mirrors ``pve.validate_vmid`` (the
# create-boundary check). Named here so the coherence invariant (§6.2) and the
# diagnostic message share one source of truth rather than a bare literal.
PVE_MID_FLOOR = 100


# --------------------------------------------------------------------------- #
# PlatformProfile — flat {mode, mid, extra_args} (§6.1).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PlatformProfile:
    """The platform a single instance runs on — a flat, inert value (§6.1).

    A ``mode`` enum plus the two platform-scoped fields it governs:

    * ``mid`` — "machine ID": the PVE vmid (always ``>= 100``; PVE reserves
      1-99) in PVE mode, ``None`` in STANDARD (plain lxc/vm identify by name,
      with no integer id). ``None`` states the absence directly rather than a
      ``0`` sentinel (§6.2), porting to ``Option<u32>`` / ``Optional``.
    * ``extra_args`` — the ``--pve-arg`` pct/qm pass-through, shared by pve-lxc
      and pve-vm. Uniform across modes (flat-struct discipline, §6.2), but empty
      in STANDARD today — PVE's is the only platform pass-through. A ``tuple`` so
      the value stays genuinely immutable; a ``list`` argument is accepted and
      frozen at construction.

    There is no ``conf_path`` / ``node`` (§6.2): the pmxcfs ``.conf`` path is an
    internal mechanic derived ambiently from ``_pve_node_name()`` (kento is
    single-node-local), not per-instance state — a consumer keys on ``mid``.

    **Cross-field coherence is enforced** (§6.2, gate C): the ``mode`` and the
    two fields it governs must agree, so an incoherent profile is
    *unrepresentable* rather than a latent foot-gun for a downstream consumer.
    STANDARD ⇒ ``mid is None`` and ``extra_args == ()``; PVE ⇒ ``mid`` is an
    ``int >= 100``. Validation is a pure check (no I/O) — the value stays inert
    (§2 principle 2). A violation raises ``ValidationError`` (§2 principle 5: a
    typed failure, never a silent fallback).
    """

    mode: PlatformMode
    mid: int | None
    extra_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Freeze an iterable argument to a tuple first (see the Diagnosis /
        # ReclaimReport idiom): a frozen dataclass freezes the *binding*, not a
        # mutable list behind it. object.__setattr__ is the standard
        # frozen-dataclass coercion idiom. Do this before the coherence check so
        # ``extra_args == ()`` compares against the frozen form.
        if not isinstance(self.extra_args, tuple):
            object.__setattr__(self, "extra_args", tuple(self.extra_args))

        # ``mode`` is the discriminator; reject anything that isn't a member so a
        # raw string or a typo can't smuggle past the coherence check below.
        if not isinstance(self.mode, PlatformMode):
            raise ValidationError(
                f"PlatformProfile.mode must be a PlatformMode, "
                f"got {self.mode!r}"
            )

        # Cross-field coherence (§6.2). Each mode fixes what the other two fields
        # may be; an incoherent combination is a programming error surfaced now,
        # not a silent inconsistency carried into the rest of the system.
        if self.mode is PlatformMode.STANDARD:
            if self.mid is not None:
                raise ValidationError(
                    f"STANDARD PlatformProfile must have mid=None "
                    f"(plain lxc/vm have no integer id), got mid={self.mid!r}"
                )
            if self.extra_args != ():
                raise ValidationError(
                    f"STANDARD PlatformProfile must have empty extra_args "
                    f"(--pve-arg is PVE-only), got {self.extra_args!r}"
                )
        else:  # PlatformMode.PVE
            # ``bool`` is an ``int`` subclass in Python; reject it explicitly so
            # ``mid=True`` can't masquerade as a vmid of 1.
            if not isinstance(self.mid, int) or isinstance(self.mid, bool):
                raise ValidationError(
                    f"PVE PlatformProfile must have an int mid (the vmid), "
                    f"got {self.mid!r}"
                )
            if self.mid < PVE_MID_FLOOR:
                raise ValidationError(
                    f"PVE PlatformProfile mid must be >= {PVE_MID_FLOOR}, got "
                    f"{self.mid} (vmids 1-99 are reserved by Proxmox)."
                )


# --------------------------------------------------------------------------- #
# Status — the instance lifecycle-state axis (§7.1).
#
# A flat enum; ``str``-backed so the value IS the wire string. Faithful to the
# fuller lifecycle the backends actually have (today's binary ``is_running()``
# bool + synthesized orphan collapses this).
# --------------------------------------------------------------------------- #


class Status(str, Enum):
    """An instance's lifecycle state — a flat enum, a base ``Instance`` field.

    The fuller lifecycle the backends actually have (§7), beyond today's binary
    ``is_running()`` bool:

    * ``SUSPENDED`` — vCPUs paused in RAM (VM-modes only; ``kento
      suspend``/``resume``), distinct from ``STOPPED`` (§7.2). LXC suspend is
      explicitly unsupported.
    * ``ORPHAN`` — kento state present but the backing PVE ``.conf`` is gone
      (PVE-modes only); semi-orthogonal (config-presence, not run-state) but
      kept in the one flat enum to match how ``list``/``diagnose`` already
      project it (§7.2).
    * ``UNKNOWN`` — status genuinely indeterminate (a PVE query timed out, or
      needs root). A real **domain** state (an unreachable node has unobservable
      status), NOT the ``resolve_image_id() -> ""`` error-as-data anti-pattern
      (§7.2, §2 principle 5): it keeps ``resolve_status`` total so a ``list()``
      over many instances doesn't blow up — or silently lie — on one unreachable
      instance.
    """

    RUNNING = "running"
    STOPPED = "stopped"
    SUSPENDED = "suspended"  # vCPUs paused in RAM (VM-modes only)
    ORPHAN = "orphan"        # state present, backing PVE .conf gone (PVE-only)
    UNKNOWN = "unknown"      # status indeterminate — query timed out / needs root
