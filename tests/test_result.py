"""Spec suite for the ``Result`` value family (Block R1).

Mutation-proven: each guard, threshold, and invariant has at least one test
that reddens if the guard is removed or the threshold is flipped. Pure value
types — no I/O is exercised.

Spec: ~/playbook/plans/result-type-design.md (Jei-ratified, run 39).
"""

import json

import pytest

from kento import (
    Condition,
    ConditionKind,
    Error,
    Ok,
    Result,
    ResultError,
    Severity,
    Warning,
)
from kento.errors import KentoError


# --------------------------------------------------------------------------- #
# Condition builders (severity-keyed) for readable test vectors.
# --------------------------------------------------------------------------- #


def _info(msg="info", **ctx):
    return Condition(Severity.INFO, ConditionKind.FRAGMENT_DROPPED, msg, ctx)


def _note(msg="note", **ctx):
    return Condition(Severity.NOTE, ConditionKind.FRAGMENT_DROPPED, msg, ctx)


def _warn(msg="warn", **ctx):
    return Condition(Severity.WARNING, ConditionKind.FRAGMENT_DROPPED, msg, ctx)


def _err(msg="err", kind=ConditionKind.FETCH_TIMEOUT, **ctx):
    return Condition(Severity.ERROR, kind, msg, ctx)


# --------------------------------------------------------------------------- #
# Severity — ordering (load-bearing: max() gives the verdict).
# --------------------------------------------------------------------------- #


def test_severity_ordering():
    assert Severity.ERROR > Severity.WARNING > Severity.NOTE > Severity.INFO
    assert int(Severity.INFO) == 1
    assert int(Severity.NOTE) == 2
    assert int(Severity.WARNING) == 3
    assert int(Severity.ERROR) == 4
    # max(...) over a stack yields the highest tier.
    assert max(Severity.INFO, Severity.ERROR, Severity.WARNING) is Severity.ERROR


# --------------------------------------------------------------------------- #
# ConditionKind — (str, Enum) house style: member IS its string value.
# --------------------------------------------------------------------------- #


def test_conditionkind_is_its_string_value():
    assert ConditionKind.FETCH_TIMEOUT == "fetch_timeout"
    assert ConditionKind.MALFORMED_REFERENCE == "malformed_reference"
    assert ConditionKind.FRAGMENT_DROPPED == "fragment_dropped"
    assert ConditionKind.SIZE_EXCEEDED == "size_exceeded"
    # str-valued -> json.dumps emits the string.
    assert json.dumps(ConditionKind.SIZE_EXCEEDED) == '"size_exceeded"'


def test_conditionkind_seeds_only_the_four():
    # R1 must NOT speculatively enumerate the library (director-owned seam).
    assert {k.value for k in ConditionKind} == {
        "malformed_reference",
        "fragment_dropped",
        "fetch_timeout",
        "size_exceeded",
    }


# --------------------------------------------------------------------------- #
# Condition — immutable context, no shared mutable default.
# --------------------------------------------------------------------------- #


def test_condition_context_is_read_only():
    c = _warn(url="https://x")
    with pytest.raises(TypeError):
        c.context["url"] = "tampered"
    with pytest.raises(TypeError):
        del c.context["url"]


def test_condition_context_default_is_empty_and_not_shared():
    a = Condition(Severity.INFO, ConditionKind.FRAGMENT_DROPPED, "a")
    b = Condition(Severity.INFO, ConditionKind.FRAGMENT_DROPPED, "b")
    assert dict(a.context) == {}
    assert dict(b.context) == {}
    # Distinct instances must not alias one mutable default.
    assert a.context is not b.context


def test_condition_context_decouples_from_caller_dict():
    src = {"k": 1}
    c = Condition(Severity.INFO, ConditionKind.FRAGMENT_DROPPED, "m", src)
    src["k"] = 999
    # Mutating the caller's dict afterwards must not leak into the frozen value.
    assert c.context["k"] == 1


def test_condition_is_frozen():
    c = _warn()
    with pytest.raises(Exception):
        c.message = "changed"


def test_condition_context_serializes_via_json():
    c = _err(url="https://x", cap=10, got=20)
    # MappingProxyType isn't directly json-able, but dict(...) of it is — the
    # CLI --json edge serializes the dict form.
    assert json.loads(json.dumps(dict(c.context))) == {
        "url": "https://x",
        "cap": 10,
        "got": 20,
    }


# --------------------------------------------------------------------------- #
# Factory `of` — collapse a stack to a subclass by MAX-SEVERITY threshold.
# --------------------------------------------------------------------------- #


def test_of_empty_is_ok():
    r = Result.of("v")
    assert isinstance(r, Ok)
    assert r.value == "v"
    assert r.conditions == ()


def test_of_info_note_is_ok():
    r = Result.of("v", (_info(), _note()))
    assert isinstance(r, Ok)
    assert r.value == "v"
    assert r.status is Severity.NOTE  # highest of INFO/NOTE


def test_of_warning_is_warning():
    r = Result.of("v", (_note(), _warn()))
    assert isinstance(r, Warning)
    assert r.value == "v"
    assert r.status is Severity.WARNING


def test_of_error_is_error_value_dropped():
    r = Result.of("v", (_err(),))
    assert isinstance(r, Error)
    assert not hasattr(r, "value")  # value is DROPPED on Error


def test_of_mixed_stack_with_error_is_error_preserving_whole_stack():
    stack = (_info(), _note(), _warn(), _err("fatal"))
    r = Result.of("v", stack)
    assert isinstance(r, Error)
    # The whole preceding stack is preserved on the Error (spec §2.2).
    assert r.conditions == stack
    assert not hasattr(r, "value")


def test_of_threshold_warning_not_promoted_to_error():
    # Guards the `>=`/`==` thresholds: a pure-WARNING stack must NOT be Error,
    # and an INFO/NOTE stack must NOT be Warning.
    assert isinstance(Result.of("v", (_warn(),)), Warning)
    assert isinstance(Result.of("v", (_note(),)), Ok)


def test_of_accepts_any_iterable_of_conditions():
    # `of` tuples the conditions argument; a list works and is frozen to a tuple.
    r = Result.of("v", [_warn()])
    assert isinstance(r, Warning)
    assert isinstance(r.conditions, tuple)


# --------------------------------------------------------------------------- #
# Per-subclass invariants (each guard independently reddens when removed).
# --------------------------------------------------------------------------- #


def test_ok_rejects_warning_condition():
    with pytest.raises(ValueError):
        Ok(value="v", conditions=(_warn(),))


def test_ok_rejects_error_condition():
    with pytest.raises(ValueError):
        Ok(value="v", conditions=(_err(),))


def test_ok_allows_info_note():
    ok = Ok(value="v", conditions=(_info(), _note()))
    assert ok.value == "v"


def test_warning_rejects_empty_conditions():
    # match= so the test binds to OUR guard's message, not the incidental
    # ValueError that max(()) raises if the explicit guard were removed.
    with pytest.raises(ValueError, match="at least one WARNING condition"):
        Warning(value="v", conditions=())


def test_warning_rejects_error_condition():
    with pytest.raises(ValueError):
        Warning(value="v", conditions=(_warn(), _err()))


def test_warning_rejects_only_sub_warning_conditions():
    # A stack of only INFO/NOTE is not a Warning (max < WARNING).
    with pytest.raises(ValueError):
        Warning(value="v", conditions=(_info(), _note()))


def test_warning_accepts_warning_with_notes():
    w = Warning(value="v", conditions=(_info(), _warn()))
    assert w.value == "v"
    assert w.status is Severity.WARNING


def test_error_rejects_empty_conditions():
    # match= so the test binds to OUR guard's message, not the incidental
    # ValueError that max(()) raises if the explicit guard were removed.
    with pytest.raises(ValueError, match="at least one ERROR condition"):
        Error(conditions=())


def test_error_rejects_max_below_error():
    with pytest.raises(ValueError):
        Error(conditions=(_warn(),))


def test_error_accepts_error_with_lower_steps():
    e = Error(conditions=(_warn(), _err()))
    assert e.status is Severity.ERROR


# --------------------------------------------------------------------------- #
# Error has NO value attribute (structural, not None).
# --------------------------------------------------------------------------- #


def test_error_has_no_value_attribute():
    e = Error(conditions=(_err(),))
    with pytest.raises(AttributeError):
        e.value  # noqa: B018  (intentional attribute access)


def test_error_value_is_not_a_field():
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(Error)}
    assert "value" not in field_names


# --------------------------------------------------------------------------- #
# unwrap / unwrap_or — the panic bridge.
# --------------------------------------------------------------------------- #


def test_ok_unwrap_returns_value():
    assert Ok(value=42).unwrap() == 42


def test_warning_unwrap_returns_value():
    assert Warning(value=42, conditions=(_warn(),)).unwrap() == 42


def test_error_unwrap_raises_resulterror():
    e = Error(conditions=(_warn("first warn"), _err("the failure")))
    with pytest.raises(ResultError) as exc:
        e.unwrap()
    # message == first ERROR condition's message (not the earlier WARNING).
    assert str(exc.value) == "the failure"
    # full conditions tuple attached.
    assert exc.value.conditions == e.conditions


def test_resulterror_is_a_kentoerror():
    assert issubclass(ResultError, KentoError)
    e = Error(conditions=(_err(),))
    with pytest.raises(KentoError):
        e.unwrap()


def test_error_unwrap_picks_first_error_message_among_multiple():
    e = Error(conditions=(_err("e1"), _err("e2")))
    with pytest.raises(ResultError) as exc:
        e.unwrap()
    assert str(exc.value) == "e1"


def test_unwrap_or_substitutes_only_for_error():
    assert Ok(value=1).unwrap_or(99) == 1
    assert Warning(value=1, conditions=(_warn(),)).unwrap_or(99) == 1
    assert Error(conditions=(_err(),)).unwrap_or(99) == 99


# --------------------------------------------------------------------------- #
# is_ok / is_error / status.
# --------------------------------------------------------------------------- #


def test_is_ok_is_error_across_subclasses():
    ok = Ok(value=1)
    ok_notes = Ok(value=1, conditions=(_note(),))
    warn = Warning(value=1, conditions=(_warn(),))
    err = Error(conditions=(_err(),))
    assert ok.is_ok() and not ok.is_error()
    assert ok_notes.is_ok() and not ok_notes.is_error()
    assert warn.is_ok() and not warn.is_error()
    assert err.is_error() and not err.is_ok()


def test_status_is_max_or_none():
    assert Ok(value=1).status is None
    assert Ok(value=1, conditions=(_info(), _note())).status is Severity.NOTE
    assert Warning(value=1, conditions=(_info(), _warn())).status is Severity.WARNING
    assert Error(conditions=(_warn(), _err())).status is Severity.ERROR


# --------------------------------------------------------------------------- #
# map — carries conditions, leaves Error unchanged.
# --------------------------------------------------------------------------- #


def test_map_ok_applies_and_carries_conditions():
    ok = Ok(value=2, conditions=(_note(),))
    out = ok.map(lambda x: x * 10)
    assert isinstance(out, Ok)
    assert out.value == 20
    assert out.conditions == (ok.conditions[0],)


def test_map_warning_applies_and_keeps_subclass():
    w = Warning(value=2, conditions=(_warn(),))
    out = w.map(lambda x: x + 1)
    assert isinstance(out, Warning)
    assert out.value == 3
    assert out.conditions == w.conditions


def test_map_error_is_unchanged_and_fn_not_called():
    calls = []

    def fn(x):
        calls.append(x)
        return x

    e = Error(conditions=(_err(),))
    out = e.map(fn)
    assert out is e
    assert calls == []  # fn must not run — no value to map


# --------------------------------------------------------------------------- #
# Frozen / abstract structure.
# --------------------------------------------------------------------------- #


def test_result_base_is_abstract():
    with pytest.raises(TypeError):
        Result()  # ABC with abstract methods — not instantiable


def test_subclasses_are_frozen():
    for r in (
        Ok(value=1),
        Warning(value=1, conditions=(_warn(),)),
        Error(conditions=(_err(),)),
    ):
        with pytest.raises(Exception):
            r.conditions = ()


# --------------------------------------------------------------------------- #
# Re-exports — flat from `kento`, all in `kento.__all__`.
# --------------------------------------------------------------------------- #


def test_flat_reexports():
    import kento

    for name in (
        "Result",
        "Ok",
        "Warning",
        "Error",
        "Condition",
        "Severity",
        "ConditionKind",
        "ResultError",
    ):
        assert hasattr(kento, name), name
        assert name in kento.__all__, name


def test_module_all_matches_exports():
    from kento import _result

    for name in _result.__all__:
        assert hasattr(_result, name), name
