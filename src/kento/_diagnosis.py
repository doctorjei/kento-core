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

import logging
from dataclasses import dataclass
from enum import Enum

_diagnosis_logger = logging.getLogger("kento")

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


# --------------------------------------------------------------------------- #
# The pure mapper — flat ``run_diagnostics`` findings -> typed ``Diagnosis``.
#
# This is the ONE place the procedural diagnose output is translated into the
# typed domain model (§11.8 D3). It is **pure / no I/O** (it consumes a result
# dict the I/O-performing entry points already obtained), so it lives here in
# the inert value-type module rather than in a handle module — keeping the three
# entry points (``instance.diagnose`` / ``image.diagnose`` / ``kento.diagnose``)
# DRY against a single mapping (the brief's "design these three coherently").
# --------------------------------------------------------------------------- #


# ``severity`` (wire word) -> ``CheckLevel`` (library word). The wire's ``warn``
# is the CLI mapping of the library's ``WARNING`` (§11.8 D3); every other value
# is identical to its enum value. An unknown severity is a forward/garbage value
# that must not crash the mapper — it degrades to ``CheckLevel.INFO`` with a log
# (total: one odd finding cannot blow up a whole diagnosis).
_SEVERITY_TO_LEVEL = {
    "ok": CheckLevel.OK,
    "info": CheckLevel.INFO,
    "warn": CheckLevel.WARNING,
    "error": CheckLevel.ERROR,
}

# ``category`` (the runtime flat ``category`` field, kept verbatim as
# ``Finding.check``) -> ``DiagnosisDomain``. THE single source of truth for
# domain assignment (§11.8 D3): INSTANCE = the per-instance checks; IMAGE = the
# hold checks (the runtime category is ``"hold"`` — both stale-hold and
# hold-drift use it); HOST = apparmor + the cross-cutting registry/collection
# state (vmid/orphan), which Jei explicitly kept in the HOST domain rather than
# split into INSTANCE. An unknown/future category is NOT in this map and falls
# back to a safe default in :func:`_domain_for_category` (see below).
_CATEGORY_TO_DOMAIN = {
    # INSTANCE domain — per-instance checks.
    "status": DiagnosisDomain.INSTANCE,
    "network": DiagnosisDomain.INSTANCE,
    "mount": DiagnosisDomain.INSTANCE,
    "portfwd": DiagnosisDomain.INSTANCE,
    "cloudinit": DiagnosisDomain.INSTANCE,
    # IMAGE domain — image-hold health (runtime category is "hold").
    "hold": DiagnosisDomain.IMAGE,
    # HOST domain — host pre-flight + cross-cutting registry/collection state.
    "apparmor": DiagnosisDomain.HOST,
    "vmid": DiagnosisDomain.HOST,
    "orphan": DiagnosisDomain.HOST,
}


def _domain_for_category(category: str) -> DiagnosisDomain:
    """Map a runtime ``category`` to its ``DiagnosisDomain`` (§11.8 D3) — TOTAL.

    A known category maps per :data:`_CATEGORY_TO_DOMAIN`. An UNKNOWN/future
    category (a runtime check added later, before this map is updated) MUST NOT
    crash the mapper: it degrades to ``DiagnosisDomain.HOST`` with a log. HOST is
    the safe default — it is the catch-all host/collection domain (apparmor,
    vmid, orphan already live there), so an unclassified finding surfaces under
    the host scan (``kento.diagnose``) rather than being silently dropped or
    mis-attributed to a specific instance/image it may not concern.
    """
    domain = _CATEGORY_TO_DOMAIN.get(category)
    if domain is not None:
        return domain
    _diagnosis_logger.warning(
        "unrecognized diagnose category %r; classifying as HOST domain",
        category,
    )
    return DiagnosisDomain.HOST


def _subject_for_finding(
    domain: DiagnosisDomain, scope: str, category: str,
) -> str | None:
    """Derive a ``Finding.subject`` from the runtime finding (§11.8 D3).

    The runtime ``scope`` field is either the literal ``"host"`` or an instance
    name. The typed ``subject`` is the flat string identity a finding is about
    (instance name / image ref), or ``None`` when the finding is about the host
    itself, not a named subject. Derivation (per the brief's #2):

    * INSTANCE domain — ``subject`` = the runtime ``scope`` (the instance name).
    * HOST apparmor / vmid — ``scope == "host"`` => ``subject = None`` (about the
      host, no named subject).
    * HOST ``orphan`` — its runtime ``scope`` IS the instance name even though
      its domain is HOST (Jei kept orphan in HOST as registry/collection state).
      The most faithful subject is that instance name, so we carry it through:
      ``subject != None`` is allowed for HOST (the "None for HOST" rule of §11.8
      describes apparmor-style host findings, not a hard invariant).
    * IMAGE ``hold`` — the runtime finding's ``scope`` is ``"host"`` and the
      image ref is embedded only in the message TEXT; the additive wrapper must
      NOT parse messages (§2 principle 3 — never re-split by hand), so the
      subject is ``None`` here. This is a DOCUMENTED limitation: clean per-image
      attribution lands with the lifecycle EPIC that refactors ``diagnose.py``.

    The general rule that implements all four: a literal ``"host"`` scope yields
    ``None``; any other scope is a real subject id carried through verbatim. The
    orphan case falls out for free (its scope is the instance name, not
    ``"host"``), and the hold case falls out for free (its scope IS ``"host"``).
    """
    if scope == "host":
        return None
    return scope


def diagnosis_from_report(
    report: dict,
    *,
    domain: DiagnosisDomain | None = None,
    subject: str | None = None,
) -> Diagnosis:
    """Map a ``run_diagnostics`` result dict to a typed ``Diagnosis`` (§11.8 D3).

    PURE — no I/O. Takes the flat result the (I/O-performing) entry points
    obtain from ``kento.diagnose.run_diagnostics`` and produces the typed
    ``Diagnosis``, translating each flat finding into a ``Finding``:

    * ``severity`` -> ``CheckLevel`` (``ok``/``info``/``warn``->WARNING/
      ``error``), via :data:`_SEVERITY_TO_LEVEL` (unknown -> INFO + log);
    * ``category`` carried VERBATIM into ``check`` (the library word; the wire's
      ``category`` is itself the library word here — §11.8 D3);
    * ``domain`` derived from ``category`` (:func:`_domain_for_category`);
    * ``subject`` derived from ``scope``/``domain`` (:func:`_subject_for_finding`);
    * ``message`` / ``remediation`` carried through (``remediation`` may be
      ``None``).

    The optional ``domain`` / ``subject`` keyword filters narrow the result so a
    single entry point can project just its slice WITHOUT re-running the scan:
    ``instance.diagnose()`` passes ``domain=INSTANCE, subject=<its name>``;
    ``image.diagnose()`` passes ``domain=IMAGE``; ``kento.diagnose()`` passes
    neither (all findings). A finding is kept iff it matches every supplied
    filter (a ``None`` filter is "don't filter on this axis").
    """
    findings: list[Finding] = []
    for raw in report.get("checks", []):
        category = raw.get("category", "")
        scope = raw.get("scope", "")
        severity = raw.get("severity", "")

        f_domain = _domain_for_category(category)
        f_subject = _subject_for_finding(f_domain, scope, category)
        level = _SEVERITY_TO_LEVEL.get(severity)
        if level is None:
            _diagnosis_logger.warning(
                "unrecognized diagnose severity %r (category %r); "
                "classifying as INFO", severity, category,
            )
            level = CheckLevel.INFO

        # Apply the optional narrowing filters (None = don't filter on that axis).
        if domain is not None and f_domain is not domain:
            continue
        if subject is not None and f_subject != subject:
            continue

        findings.append(Finding(
            domain=f_domain,
            subject=f_subject,
            check=category,
            level=level,
            message=raw.get("message", ""),
            remediation=raw.get("remediation"),
        ))
    return Diagnosis(findings=tuple(findings))
