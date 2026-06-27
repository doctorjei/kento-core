"""Diagnosis & reclaim report value types — the inert result objects.

These are **pure, inert, frozen value types** (spec §2 principle 2): no I/O,
ever; a constructed value is plain data you can pass, copy, and reason about.
They are the *return* types of the handle methods that DO perform I/O
(``instance.diagnose()`` / ``image.diagnose()`` / ``kento.diagnose()`` and
``Image.prune`` / ``Instance.prune_orphans``) — those handle methods live in a
later block; this module ships only the inert objects they hand back.

The public surface (``DiagnosisDomain``, ``CheckLevel``, ``PruneScope``,
``Finding``, ``Diagnosis``, ``ReclaimReport``) is re-exported flat from
``kento`` — refer to ``kento.Diagnosis``, not ``kento._diagnosis.Diagnosis``.

The domain model (spec §11.8 D3): there are three diagnostic **domains**
(instance / image / host); a single scan emits **one ``Finding`` per subject**
including healthy ones (a healthy subject gets one ``OK`` finding). Coverage
("what was scanned") is therefore visible *in the findings*, and ``Diagnosis``
stores **no count fields** — ``ok`` and ``problems`` are **derived** from the
findings. Wire-format projections (``check``->``category``,
``WARNING``->``warn``, the ``problem_count``/``instances_scanned`` stats) are a
CLI-edge concern (§11.8 D1), deliberately NOT modelled here.

Spec: ``~/workspace/kento-core-api-design.md`` §2, §11.6, §11.8 (D3), §11.9.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "DiagnosisDomain",
    "CheckLevel",
    "PruneScope",
    "Finding",
    "Diagnosis",
    "ReclaimReport",
]


# --------------------------------------------------------------------------- #
# Enums (§11.8 D3, §11.9).
#
# All three subclass ``str`` so their members compare/serialize as their wire
# value (``DiagnosisDomain.HOST == "host"``) — the same ``str, Enum`` shape the
# spec writes verbatim, matching the library's idiom for closed value sets.
# --------------------------------------------------------------------------- #


class DiagnosisDomain(str, Enum):
    """The diagnostic domain a finding belongs to (§11.8 D3).

    The three domains already implicit in today's flat output, given structure:
    INSTANCE (status/network/mount/portfwd/cloudinit), IMAGE (hold-drift +
    future dangling/stale/missing-layers), HOST (apparmor + future podman/nft/
    store/disk, and cross-cutting registry/collection state — vmid/orphan).
    """

    INSTANCE = "instance"
    IMAGE = "image"
    HOST = "host"


class CheckLevel(str, Enum):
    """The severity of a single finding (§11.8 D3).

    Library-first naming: ``ERROR`` is **restored** (the scan emits it) and the
    idiomatic ``WARNING`` is kept — the wire format's ``warn`` is a CLI mapping
    (§11.8 D1), not the library's vocabulary. ``OK`` marks a healthy subject.
    """

    OK = "ok"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class PruneScope(str, Enum):
    """What a prune/reclaim operation targets (§11 M22, §11.9).

    The shape is locked now — ``Image.prune`` takes a single ``scope:
    PruneScope`` (replacing the awkward two-bool + builtin-shadowing ``all=``).
    The method signature is LOCKED with a named default:
    ``Image.prune(*, scope: PruneScope = PruneScope.DANGLING)`` (spec line
    1298), so ``DANGLING`` — the default scope the locked signature references —
    ships now. The **further provenance scopes** (the kento-pulled-only vs
    include-all distinction) are what "VALUES finalize with the EPIC" (§11.9
    M22) defers; they land with the image-lifecycle EPIC, non-breakingly.
    """

    DANGLING = "dangling"
    # Further provenance scopes land WITH the image-lifecycle EPIC (§11.9 M22)
    # — do NOT invent them here. DANGLING is present because the LOCKED M22
    # signature (line 1298) names it as the default; pre-deciding the
    # remaining provenance semantics is what the EPIC resolution defers.


# --------------------------------------------------------------------------- #
# Finding — one diagnostic observation about one subject (§11.8 D3).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Finding:
    """One diagnostic observation — flat ``domain`` + ``subject`` (§11.8 D3).

    ``subject`` is a **flat string** identity (instance name / image ref), not a
    nested type, per the flatten-a-closed-discriminator principle (§11.8 D3
    micro-choice 1); it is ``None`` for HOST-domain findings (which are about
    the host itself, not a named subject). ``check`` carries the **library**
    word (``status``/``network``/``mount``/``portfwd``/``cloudinit``/
    ``hold-drift``/``apparmor``/``vmid``/``orphan``); the wire's ``category`` is
    a CLI mapping. ``remediation`` (how to fix) is optional — present on
    actionable findings, ``None`` otherwise.
    """

    domain: DiagnosisDomain
    subject: str | None
    check: str
    level: CheckLevel
    message: str
    remediation: str | None = None


# --------------------------------------------------------------------------- #
# Diagnosis — a collection of findings; ok/problems DERIVED (§11.8 D3).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Diagnosis:
    """The result of a diagnostic scan — a bag of findings (§11.8 D3).

    Holds only ``findings``. There are **no stored count fields**: a scan emits
    one finding per subject (one ``OK`` finding per healthy subject), so
    coverage is visible in the findings themselves and any statistic a consumer
    wants — ``problem_count``, ``instances_scanned`` — is derived from them.
    Keeping wire stats out of the type is deliberate (§11.8 D3 micro-choice 2).

    ``findings`` is a ``tuple`` so the value stays immutable end to end; a
    ``list`` argument is accepted and frozen at construction.
    """

    findings: tuple[Finding, ...] = ()

    def __post_init__(self) -> None:
        # Accept any finite iterable (the natural caller builds a list) and
        # freeze it to a tuple so the value type stays genuinely immutable —
        # a frozen dataclass freezes the *binding*, not a mutable list behind
        # it. object.__setattr__ is the standard frozen-dataclass coercion idiom.
        if not isinstance(self.findings, tuple):
            object.__setattr__(self, "findings", tuple(self.findings))

    @property
    def problems(self) -> tuple[Finding, ...]:
        """The findings that represent a problem — WARNING or ERROR level.

        Derived, never stored. ``OK``/``INFO`` findings (healthy subjects and
        informational notes) are not problems.
        """
        return tuple(
            f for f in self.findings
            if f.level in (CheckLevel.WARNING, CheckLevel.ERROR)
        )

    @property
    def ok(self) -> bool:
        """Whether the scan found no problems — derived, never stored.

        ``True`` iff there are no WARNING/ERROR findings. An all-``OK``
        diagnosis (every scanned subject healthy) is ``ok``; so is an empty
        diagnosis (nothing scanned). ``ok`` is exactly ``not self.problems``.
        """
        return not self.problems


# --------------------------------------------------------------------------- #
# ReclaimReport — the prune/reclaim result (§11.6 M25).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ReclaimReport:
    """The result of a prune/reclaim batch — ONE shared type (§11.6 M25).

    Returned by BOTH ``Image.prune`` (M22) and ``Instance.prune_orphans`` (M4):
    a dry-run-able batch reclaim with surfaced failures. Targets are plain
    string identifiers (image ref / instance name).

    - ``dry_run``  — when ``True``, ``reclaimed`` is *would-remove*, nothing was
      actually removed; when ``False`` it is what was removed.
    - ``reclaimed`` — the targets removed (or would-be-removed under dry-run).
    - ``failed``   — ``(target, reason)`` pairs for failures surfaced rather
      than swallowed (the 1.6.2 failure-surfacing contract).

    ``ok`` is **derived** (``not self.failed``) — no stored flag. Both tuple
    fields accept any iterable at construction and are frozen to tuples so the
    value stays immutable.
    """

    dry_run: bool
    reclaimed: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        # Freeze iterable arguments to tuples (see Diagnosis.__post_init__).
        if not isinstance(self.reclaimed, tuple):
            object.__setattr__(self, "reclaimed", tuple(self.reclaimed))
        if not isinstance(self.failed, tuple):
            object.__setattr__(
                self, "failed", tuple(tuple(pair) for pair in self.failed)
            )

    @property
    def ok(self) -> bool:
        """Whether the reclaim had no failures — derived, never stored."""
        return not self.failed
