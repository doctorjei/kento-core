"""The ``Result`` value family — the library's return surface for predictable outcomes.

This module implements the Rust-style split (spec ``result-type-design.md`` §1):

* **Exceptions = panic ONLY** — catastrophic / unpredictable / a bug (API misuse,
  a broken invariant, a genuinely-impossible state). Still the ``KentoError``
  hierarchy in ``kento.errors``.
* **``Result`` for every PREDICTABLE outcome** — each failure a reasonable caller
  is expected to handle, plus *success-with-warnings*.

``Result[T]`` is a frozen, abstract base with three flat sibling subclasses —
``Ok[T]`` (clean success), ``Warning[T]`` (success with caveats), ``Error``
(failure, carrying NO value). Flat (not ``Warning(Ok)``) so ``match`` arms are
cleanly mutually exclusive; ``is_ok()`` covers the "did I get a value?" check
that spans ``Ok`` and ``Warning``.

These are **pure, inert, frozen value types** (spec §2 principle 2): no I/O, no
domain logic. A ``Condition`` is a PLAIN VALUE — never an ``Exception``, never
raised; it is only ever *carried* in a ``Result``. ``unwrap()`` is the single
sanctioned crossing from the ``Result`` channel back to the panic channel
(``Condition`` → ``ResultError``).

The public surface (``Result``, ``Ok``, ``Warning``, ``Error``, ``Condition``,
``Severity``, ``ConditionKind``, ``ResultError``) is re-exported flat from
``kento`` — refer to ``kento.Result``, not ``kento._result.Result``.

Spec: ``~/playbook/plans/result-type-design.md`` (Jei-ratified, run 39).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from types import MappingProxyType
from typing import Generic, TypeVar

from kento.errors import (
    ImageNotFoundError,
    InstanceExistsError,
    InstanceNotFoundError,
    KentoError,
    ModeError,
    StateError,
    StopTimeout,
    SubprocessError,
    ValidationError,
)

__all__ = [
    "Severity",
    "ConditionKind",
    "Condition",
    "Result",
    "Ok",
    "Warning",
    "Error",
    "ResultError",
]

T = TypeVar("T")
U = TypeVar("U")


# --------------------------------------------------------------------------- #
# Severity — the ORDERED condition-severity axis.
#
# ``IntEnum`` because the overall verdict of a condition stack is ``max(...)`` of
# the members' severities, so the members must compare/order. INFO/NOTE are
# SUB-WARNING (they ride on ``Ok``); WARNING is the lowest ACTIONABLE tier
# (makes a Result a ``Warning``); ERROR makes it an ``Error``.
# --------------------------------------------------------------------------- #


class Severity(IntEnum):
    """How serious a ``Condition`` is — an ordered axis (spec §2, Q3).

    Ordered (``IntEnum``) so ``max(severities)`` yields the overall verdict of a
    condition stack. The subclass of a ``Result`` reflects the *highest
    ACTIONABLE tier* present:

    * ``INFO`` / ``NOTE`` are sub-WARNING annotations — they ride on ``Ok``
      (an ``Ok`` is "no condition >= WARNING", not "no conditions at all").
    * ``WARNING`` is the lowest actionable tier — one makes a ``Result`` a
      ``Warning``.
    * ``ERROR`` makes it an ``Error``.

    Order ``INFO < NOTE`` follows the syslog convention (NOTICE > INFO); it is a
    director default, not load-bearing.
    """

    INFO = 1
    NOTE = 2
    WARNING = 3
    ERROR = 4


# --------------------------------------------------------------------------- #
# ConditionKind — the programmatic discriminator (seadog/CLI branch on it).
#
# House enum style ``(str, Enum)`` (matches NetworkMode/Status etc.) so a member
# IS its snake_case string value: type-safe in our code, string-valued for
# ``--json`` (json.dumps emits the value). CENTRAL + DIRECTOR-OWNED SEAM — this
# block seeds ONLY the kinds it + the URL-VM pilot need; later blocks add their
# own, merged by the director (spec §6, Q1).
# --------------------------------------------------------------------------- #


class ConditionKind(str, Enum):
    """The programmatic discriminator for a ``Condition`` (spec §2, Q1).

    A ``(str, Enum)`` (house style) so each member IS its string value —
    type-safe for code, string-valued for ``--json``/seadog branching. This is a
    CENTRAL, director-owned merge seam: only the kinds Block R1 and the imminent
    URL-VM pilot need are seeded here; later blocks add their own (do NOT
    speculatively enumerate the whole library).
    """

    MALFORMED_REFERENCE = "malformed_reference"
    FRAGMENT_DROPPED = "fragment_dropped"
    FETCH_TIMEOUT = "fetch_timeout"
    SIZE_EXCEEDED = "size_exceeded"
    NON_HTTPS = "non_https"
    FETCH_FAILED = "fetch_failed"
    HTTP_ERROR = "http_error"

    # --- Result-propagation-sweep kinds (Block S1). One kind per KentoError
    # subclass — same granularity a caller gets from the exception type today.
    # Consumed by ``_error_from`` (the boundary helper below); seadog/CLI branch
    # on the string value. ``INTERNAL`` is the total fallback for a bare/unmapped
    # ``KentoError`` so the type→kind map is exhaustive for the whole hierarchy.
    VALIDATION = "validation"
    INSTANCE_NOT_FOUND = "instance_not_found"
    INSTANCE_EXISTS = "instance_exists"
    IMAGE_NOT_FOUND = "image_not_found"
    MODE_ERROR = "mode_error"
    INVALID_STATE = "invalid_state"
    STOP_TIMEOUT = "stop_timeout"
    SUBPROCESS_FAILED = "subprocess_failed"
    INTERNAL = "internal"


# --------------------------------------------------------------------------- #
# Condition — the structured payload of a Result. A PLAIN VALUE, never raised.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Condition:
    """One entry in a ``Result``'s condition stack — a plain, immutable value.

    Carries the structured detail of a single outcome step: how serious it is
    (``severity``), what kind it is (``kind`` — the programmatic discriminator),
    a human-/CLI-renderable ``message``, and ``context`` (structured extras for
    ``--json``, e.g. ``{"url": ..., "cap": ..., "got": ...}``).

    A ``Condition`` is **never** an ``Exception`` and is **never** raised — it is
    only ever carried inside a ``Result`` (the channel-separation rule, spec §3).

    ``context`` is immutable: whatever mapping is passed in is wrapped in a
    read-only ``types.MappingProxyType`` in ``__post_init__``. The default is a
    fresh empty proxy per instance — no shared mutable default (spec §6, Q2).
    """

    severity: Severity
    kind: ConditionKind
    message: str
    context: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Wrap context in a read-only view so the stored value cannot be mutated
        # through any reference the caller kept. We copy into a fresh dict first
        # so that later mutation of the *caller's* dict does not leak in either.
        # ``object.__setattr__`` is the established frozen-dataclass mutation
        # idiom (a frozen dataclass freezes attribute binding).
        object.__setattr__(
            self, "context", MappingProxyType(dict(self.context))
        )


# --------------------------------------------------------------------------- #
# ResultError — the raised form of an unwrapped Error (the panic bridge).
# --------------------------------------------------------------------------- #


class ResultError(KentoError):
    """The exception raised by ``Error.unwrap()`` — the Result→panic crossing.

    Built from an ``Error``'s conditions: the message is the first ``ERROR``
    condition's ``message``, and the full ``conditions`` tuple is attached as
    ``.conditions`` for context. ``unwrap()`` is the single sanctioned crossing
    from the ``Result`` channel to the panic (exception) channel (spec §3).

    Block R1 raises the generic ``ResultError``; mapping a specific ``kind`` to a
    specific ``KentoError`` subtype is a later block (out of scope here).
    """

    def __init__(self, message: str, *, conditions: tuple[Condition, ...]):
        super().__init__(message)
        self.conditions = conditions


# --------------------------------------------------------------------------- #
# Result — the abstract, frozen base of the three-subclass family.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, kw_only=True)
class Result(ABC, Generic[T]):
    """The outcome of a predictable operation — abstract; frozen.

    Three flat sibling subclasses (spec §2): ``Ok`` (clean success), ``Warning``
    (success with caveats), ``Error`` (failure, no value). The subclass reflects
    the highest *actionable* tier of ``conditions``; ``status`` is the derived
    wire/``--json`` tag. ``conditions`` is the accumulated stack (may be empty).

    Constructed with keyword arguments (``kw_only`` — the house idiom for a
    frozen base + subclasses that add a non-default field; see ``_images.py``).
    """

    conditions: tuple[Condition, ...] = ()

    @property
    def status(self) -> Severity | None:
        """The overall verdict tag — ``max`` of condition severities, or ``None``.

        ``None`` for an empty stack (only possible on an ``Ok``). This is the
        single value the wire / ``--json`` edge surfaces as the result's tag.
        """
        if not self.conditions:
            return None
        return max(c.severity for c in self.conditions)

    def is_ok(self) -> bool:
        """True when a value is present — i.e. ``Ok`` OR ``Warning`` (not ``Error``)."""
        return not isinstance(self, Error)

    def is_error(self) -> bool:
        """True only for ``Error`` (no value present)."""
        return isinstance(self, Error)

    @abstractmethod
    def unwrap(self) -> T:
        """Return the value if present, else raise (the panic bridge, spec §3).

        ``Ok``/``Warning`` return ``value``; ``Error`` raises ``ResultError``.
        """

    @abstractmethod
    def unwrap_or(self, default: T) -> T:
        """Return the value if present, else ``default`` (only ``Error`` substitutes)."""

    @abstractmethod
    def map(self, fn: Callable[[T], U]) -> "Result[U]":
        """Apply ``fn`` to the value if present, carrying ``conditions`` forward.

        ``Ok``/``Warning`` apply ``fn`` and keep their subclass + conditions;
        ``Error`` carries no value and is returned unchanged. Pure.
        """

    @staticmethod
    def of(
        value: T, conditions: tuple[Condition, ...] = ()
    ) -> "Result[T]":
        """Collapse a condition stack to the right subclass by MAX-SEVERITY threshold.

        The ergonomic constructor for boundary code that accumulates conditions
        down a call chain (fetch → extract → mount): pass the accumulated stack
        plus the value-so-far and the verdict is derived (spec §2.2):

        * ``max >= ERROR`` → ``Error(conditions)`` (the value is DROPPED; the
          whole preceding stack — INFO/NOTE/WARNING steps that ran before the
          fatal one — is preserved).
        * ``max == WARNING`` → ``Warning(value, conditions)``.
        * ``max <= NOTE`` or empty → ``Ok(value, conditions)``.
        """
        conditions = tuple(conditions)
        if conditions:
            top = max(c.severity for c in conditions)
            if top >= Severity.ERROR:
                return Error(conditions=conditions)
            if top == Severity.WARNING:
                return Warning(value=value, conditions=conditions)
        return Ok(value=value, conditions=conditions)


# --------------------------------------------------------------------------- #
# Ok — clean success (no condition >= WARNING; may carry INFO/NOTE notes).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, kw_only=True)
class Ok(Result[T]):
    """Success — a value is present and no condition reaches the actionable tier.

    Invariant (spec §2.1): no condition ``>= WARNING`` (the stack is empty or
    holds only INFO/NOTE notes). A breach is a *bug*, so ``__post_init__``
    raises (a panic, consistent with the doctrine).
    """

    value: T

    def __post_init__(self) -> None:
        if any(c.severity >= Severity.WARNING for c in self.conditions):
            raise ValueError(
                "Ok may not carry a condition >= WARNING "
                "(use Warning/Error, or Result.of)"
            )

    def unwrap(self) -> T:
        return self.value

    def unwrap_or(self, default: T) -> T:
        return self.value

    def map(self, fn: Callable[[T], U]) -> "Result[U]":
        return Ok(value=fn(self.value), conditions=self.conditions)


# --------------------------------------------------------------------------- #
# Warning — success WITH caveats (max severity == WARNING).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, kw_only=True)
class Warning(Result[T]):
    """Success with caveats — a value is present, but the stack tops out at WARNING.

    Invariant (spec §2.1): ``conditions`` non-empty AND ``max == WARNING`` (at
    least one WARNING, no ERROR). A breach is a *bug* → ``__post_init__`` raises.

    NOTE: this name shadows the builtin ``Warning``; the spec chose it. Consumers
    import ``kento.Warning`` explicitly. Within this module the builtin warning
    category is never referenced, so the shadow is harmless.
    """

    value: T

    def __post_init__(self) -> None:
        if not self.conditions:
            raise ValueError("Warning requires at least one WARNING condition")
        top = max(c.severity for c in self.conditions)
        if top != Severity.WARNING:
            raise ValueError(
                "Warning's max condition severity must be exactly WARNING "
                f"(got {top.name}; use Ok for notes, Error for failures)"
            )

    def unwrap(self) -> T:
        # A Warning's caveats were already surfaced; they do not block the value.
        return self.value

    def unwrap_or(self, default: T) -> T:
        return self.value

    def map(self, fn: Callable[[T], U]) -> "Result[U]":
        return Warning(value=fn(self.value), conditions=self.conditions)


# --------------------------------------------------------------------------- #
# Error — failure. NO ``value`` attribute AT ALL (reading one is a structural
# impossibility, not a None you forgot to check — spec §2.1).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, kw_only=True)
class Error(Result[T]):
    """Failure — carries conditions but **no value attribute at all** (spec §2.1).

    Invariant: ``conditions`` non-empty AND ``max == ERROR`` (at least one
    ERROR). A breach is a *bug* → ``__post_init__`` raises. Having no ``value``
    field is the point: "read the value off an ``Error``" is a structural error
    you can't write (``AttributeError``), not a ``None`` you forgot to check.

    Generic in ``T`` only for return-type compatibility — a function returning
    ``Result[Foo]`` may return an ``Error``.
    """

    def __post_init__(self) -> None:
        if not self.conditions:
            raise ValueError("Error requires at least one ERROR condition")
        top = max(c.severity for c in self.conditions)
        if top != Severity.ERROR:
            raise ValueError(
                "Error's max condition severity must be ERROR "
                f"(got {top.name}; use Ok/Warning for sub-error outcomes)"
            )

    def unwrap(self) -> T:
        first_error = next(
            c for c in self.conditions if c.severity == Severity.ERROR
        )
        raise ResultError(first_error.message, conditions=self.conditions)

    def unwrap_or(self, default: T) -> T:
        return default

    def map(self, fn: Callable[[T], U]) -> "Result[U]":
        # No value to map; an Error is carried through unchanged.
        return self


# --------------------------------------------------------------------------- #
# Boundary helper — the inverse of ``Error.unwrap()`` (Result-propagation sweep,
# Block S1). A PUBLIC verb (S2+) wraps its body at its boundary as
# ``try: ... return Ok(value) except KentoError as e: return _error_from(e)`` —
# the ~195 internal raises stay as control flow, caught at the nearest public
# boundary and converted here to a one-condition ``Error``. A NON-``KentoError``
# (a broken invariant, an unexpected ``RuntimeError``) is a PANIC and never
# reaches this helper — it propagates.
#
# Mirrors ``_references._malformed_error`` (the P1 parse-boundary precedent):
# message preserved verbatim (so ``unwrap()`` re-raises the same text),
# structured attrs → ``context``.
# --------------------------------------------------------------------------- #


# Type → kind map. ORDER IS IRRELEVANT to correctness — ``_error_from`` walks the
# exception's ``__mro__`` and takes the FIRST member found in this dict, so a
# subclass (``StopTimeout``) resolves to its own kind before its parent
# (``StateError``) regardless of insertion order. ``KentoError`` is present as the
# total fallback, making the walk exhaustive for any ``KentoError`` subclass —
# including a future one nobody mapped (it lands on ``INTERNAL``).
#
# ``MalformedReference`` is deliberately ABSENT: ``_references`` imports from this
# module, so importing it back here would be a cycle. It does not need an entry —
# ``*Reference.parse`` already converts it at the parse boundary (its own
# ``MALFORMED_REFERENCE`` Error via ``_malformed_error``), so a raised
# ``MalformedReference`` is normally pre-converted and never reaches
# ``_error_from``. If a verb DID surface one here, it is a ``KentoError`` and maps
# to ``INTERNAL`` via the fallback — acceptable (documented in the block brief).
_ERROR_KIND_MAP: dict[type[KentoError], ConditionKind] = {
    ValidationError: ConditionKind.VALIDATION,
    InstanceNotFoundError: ConditionKind.INSTANCE_NOT_FOUND,
    InstanceExistsError: ConditionKind.INSTANCE_EXISTS,
    ImageNotFoundError: ConditionKind.IMAGE_NOT_FOUND,
    ModeError: ConditionKind.MODE_ERROR,
    StopTimeout: ConditionKind.STOP_TIMEOUT,
    StateError: ConditionKind.INVALID_STATE,
    SubprocessError: ConditionKind.SUBPROCESS_FAILED,
    KentoError: ConditionKind.INTERNAL,
}


def _error_from(exc: KentoError) -> Error:
    """Convert a caught ``KentoError`` to a boundary ``Error`` (the §2 pr.5 inverse).

    The single boundary crossing from the panic channel back to the ``Result``
    channel for the verb sweep (S2+): a PUBLIC verb catches its internal
    ``KentoError`` and calls this to produce a one-condition ``Error``.

    Resolution is **most-specific-first**: the walk over ``type(exc).__mro__``
    takes the first type present in ``_ERROR_KIND_MAP``, so a ``StopTimeout``
    (which subclasses ``StateError``) maps to ``STOP_TIMEOUT``, not
    ``INVALID_STATE`` (cf. HTTPError-before-URLError in the B2 fetch boundary).
    ``KentoError`` itself is in the map (``INTERNAL``), so the walk is total for
    any ``KentoError`` subclass — the loop always finds a kind.

    The message is preserved verbatim (``str(exc)``) so an ``unwrap()`` of the
    returned ``Error`` re-raises the same text. For a ``SubprocessError`` the
    structured ``returncode`` + ``cmd`` ride in ``context`` (the CLI reads
    ``returncode`` in S7 to pick exit code 1-vs-2); both are included verbatim,
    ``None`` and all, so the key set is stable for the consumer. All other
    subclasses carry empty context (their message holds the detail).

    All produced conditions are ``Severity.ERROR`` — ``_error_from`` only ever
    converts failures. (Success-with-caveat ``Warning`` conditions come from the
    succeed-with-caveat paths, never from here.)
    """
    kind = next(
        _ERROR_KIND_MAP[t]
        for t in type(exc).__mro__
        if t in _ERROR_KIND_MAP
    )
    if isinstance(exc, SubprocessError):
        context: Mapping[str, object] = {
            "returncode": exc.returncode,
            "cmd": exc.cmd,
        }
    else:
        context = {}
    return Error(
        conditions=(
            Condition(
                severity=Severity.ERROR,
                kind=kind,
                message=str(exc),
                context=context,
            ),
        ),
    )
