"""Spec suite for the diagnosis & report value types (Block 04).

Covers enum membership/values, the DERIVED ``ok``/``problems`` logic (incl. the
healthy-subject-emits-OK / coverage-derivable behavior and the no-stored-counts
invariant), ``Finding``/``Diagnosis``/``ReclaimReport`` construction + frozen
immutability, and the flat-public re-export from ``kento``.

Spec: ~/workspace/kento-core-api-design.md §2, §11.6, §11.8 (D3), §11.9.
"""

import dataclasses

import pytest

from kento import (
    CheckLevel,
    Diagnosis,
    DiagnosisDomain,
    Finding,
    PruneScope,
    ReclaimReport,
)


# --------------------------------------------------------------------------- #
# Flat public re-export (Block 04 surface).
# --------------------------------------------------------------------------- #


def test_public_names_reexported_flat():
    import kento

    for name in (
        "DiagnosisDomain", "CheckLevel", "PruneScope",
        "Finding", "Diagnosis", "ReclaimReport",
    ):
        assert name in kento.__all__, f"{name} missing from kento.__all__"
        assert getattr(kento, name) is not None


def test_no_module_stutter():
    # The canonical path is kento.X, not kento._diagnosis.X (still importable
    # internally, but the public name is flat).
    import kento
    import kento._diagnosis as impl

    assert kento.Diagnosis is impl.Diagnosis
    assert kento.Finding is impl.Finding


# --------------------------------------------------------------------------- #
# Enums — membership + wire values (§11.8 D3, §11.9).
# --------------------------------------------------------------------------- #


def test_diagnosis_domain_members_and_values():
    assert {d.name for d in DiagnosisDomain} == {"INSTANCE", "IMAGE", "HOST"}
    assert DiagnosisDomain.INSTANCE.value == "instance"
    assert DiagnosisDomain.IMAGE.value == "image"
    assert DiagnosisDomain.HOST.value == "host"


def test_check_level_members_and_values():
    # ERROR is restored (M24 wrongly dropped it); WARNING kept idiomatic.
    assert {c.name for c in CheckLevel} == {"OK", "INFO", "WARNING", "ERROR"}
    assert CheckLevel.OK.value == "ok"
    assert CheckLevel.INFO.value == "info"
    assert CheckLevel.WARNING.value == "warning"
    assert CheckLevel.ERROR.value == "error"


def test_enums_are_str_enums():
    # str, Enum shape: members compare/serialize as their wire value.
    assert isinstance(DiagnosisDomain.HOST, str)
    assert isinstance(CheckLevel.ERROR, str)
    assert DiagnosisDomain.HOST == "host"
    assert CheckLevel.ERROR == "error"


def test_prune_scope_ships_dangling_default():
    # The LOCKED M22 signature names a default: prune(*, scope=PruneScope.
    # DANGLING) (spec line 1298). DANGLING must exist for that default to be
    # constructable. Further provenance scopes land with the lifecycle EPIC.
    assert issubclass(PruneScope, str)
    assert PruneScope.DANGLING.value == "dangling"
    assert PruneScope("dangling") is PruneScope.DANGLING


def test_prune_scope_only_dangling_for_now():
    # Only DANGLING ships in 1.0; the remaining provenance scopes are deferred
    # to the image-lifecycle EPIC (§11.9 M22) — not pre-decided here.
    assert {s.name for s in PruneScope} == {"DANGLING"}


# --------------------------------------------------------------------------- #
# Finding — construction + invariants (§11.8 D3).
# --------------------------------------------------------------------------- #


def test_finding_construction_full():
    f = Finding(
        domain=DiagnosisDomain.INSTANCE,
        subject="web-0",
        check="network",
        level=CheckLevel.WARNING,
        message="eth0 has no address",
        remediation="check the bridge",
    )
    assert f.domain is DiagnosisDomain.INSTANCE
    assert f.subject == "web-0"
    assert f.check == "network"
    assert f.level is CheckLevel.WARNING
    assert f.message == "eth0 has no address"
    assert f.remediation == "check the bridge"


def test_finding_remediation_defaults_none():
    f = Finding(
        domain=DiagnosisDomain.INSTANCE,
        subject="web-0",
        check="status",
        level=CheckLevel.OK,
        message="running",
    )
    assert f.remediation is None


def test_finding_host_subject_may_be_none():
    # HOST findings are about the host itself — flat subject is None.
    f = Finding(
        domain=DiagnosisDomain.HOST,
        subject=None,
        check="apparmor",
        level=CheckLevel.ERROR,
        message="apparmor_parser missing",
        remediation="install apparmor",
    )
    assert f.subject is None
    assert f.domain is DiagnosisDomain.HOST


def test_finding_subject_is_flat_string_no_nested_type():
    # Micro-choice 1: domain enum + flat subject id, no nested Subject type.
    fields = {f.name: f.type for f in dataclasses.fields(Finding)}
    assert "subject" in fields
    f = Finding(
        domain=DiagnosisDomain.IMAGE,
        subject="docker.io/library/debian:12",
        check="hold-drift",
        level=CheckLevel.INFO,
        message="hold present",
    )
    assert isinstance(f.subject, str)


def test_finding_is_frozen():
    f = Finding(
        domain=DiagnosisDomain.HOST, subject=None, check="apparmor",
        level=CheckLevel.OK, message="ok",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        f.message = "tampered"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Diagnosis — derived ok/problems, no stored counts (§11.8 D3).
# --------------------------------------------------------------------------- #


def _finding(level, *, domain=DiagnosisDomain.INSTANCE, subject="x",
             check="status", message="msg"):
    return Finding(domain=domain, subject=subject, check=check, level=level,
                   message=message)


def test_empty_diagnosis_is_ok_with_no_problems():
    d = Diagnosis()
    assert d.findings == ()
    assert d.ok is True
    assert d.problems == ()


def test_healthy_subject_emits_ok_finding_and_diagnosis_is_ok():
    # A healthy subject gets ONE OK finding; the diagnosis is still ok and the
    # OK finding is NOT a problem.
    d = Diagnosis(findings=(_finding(CheckLevel.OK, subject="web-0"),))
    assert d.ok is True
    assert d.problems == ()
    assert len(d.findings) == 1


def test_info_findings_are_not_problems():
    d = Diagnosis(findings=(_finding(CheckLevel.INFO),))
    assert d.ok is True
    assert d.problems == ()


def test_warning_finding_is_a_problem():
    warn = _finding(CheckLevel.WARNING)
    d = Diagnosis(findings=(warn,))
    assert d.ok is False
    assert d.problems == (warn,)


def test_error_finding_is_a_problem():
    err = _finding(CheckLevel.ERROR)
    d = Diagnosis(findings=(err,))
    assert d.ok is False
    assert d.problems == (err,)


def test_problems_preserves_order_and_filters_only_problems():
    ok = _finding(CheckLevel.OK, subject="a")
    info = _finding(CheckLevel.INFO, subject="b")
    warn = _finding(CheckLevel.WARNING, subject="c")
    err = _finding(CheckLevel.ERROR, subject="d")
    d = Diagnosis(findings=(ok, warn, info, err))
    assert d.problems == (warn, err)
    assert d.ok is False


def test_coverage_is_derivable_from_findings():
    # No stored count fields; coverage = distinct subjects across findings, and
    # problem_count = len(problems). Both derive from findings alone.
    findings = (
        _finding(CheckLevel.OK, subject="web-0"),
        _finding(CheckLevel.WARNING, subject="web-1"),
        _finding(CheckLevel.OK, subject="db-0"),
    )
    d = Diagnosis(findings=findings)
    scanned = {f.subject for f in d.findings}
    assert scanned == {"web-0", "web-1", "db-0"}
    assert len(d.problems) == 1  # the consumer's problem_count


def test_diagnosis_has_no_stored_count_fields():
    # The no-count-fields invariant: only `findings` is stored; ok/problems
    # are properties, not dataclass fields.
    field_names = {f.name for f in dataclasses.fields(Diagnosis)}
    assert field_names == {"findings"}
    assert "problems" not in field_names
    assert "ok" not in field_names
    assert "problem_count" not in field_names
    assert "instances_scanned" not in field_names


def test_diagnosis_ok_is_exactly_not_problems():
    for findings in (
        (),
        (_finding(CheckLevel.OK),),
        (_finding(CheckLevel.WARNING),),
        (_finding(CheckLevel.OK), _finding(CheckLevel.ERROR, subject="y")),
    ):
        d = Diagnosis(findings=findings)
        assert d.ok == (not d.problems)


def test_diagnosis_accepts_list_and_freezes_to_tuple():
    src = [_finding(CheckLevel.OK)]
    d = Diagnosis(findings=src)  # type: ignore[arg-type]
    assert isinstance(d.findings, tuple)
    # Mutating the original list does not affect the frozen value.
    src.append(_finding(CheckLevel.ERROR))
    assert len(d.findings) == 1


def test_diagnosis_is_frozen():
    d = Diagnosis(findings=(_finding(CheckLevel.OK),))
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.findings = ()  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# ReclaimReport — shared prune/reclaim result (§11.6 M25).
# --------------------------------------------------------------------------- #


def test_reclaim_report_dry_run_would_remove():
    r = ReclaimReport(dry_run=True, reclaimed=("img-a", "img-b"))
    assert r.dry_run is True
    assert r.reclaimed == ("img-a", "img-b")
    assert r.failed == ()
    assert r.ok is True


def test_reclaim_report_actual_removal():
    r = ReclaimReport(dry_run=False, reclaimed=("web-0",))
    assert r.dry_run is False
    assert r.reclaimed == ("web-0",)
    assert r.ok is True


def test_reclaim_report_failures_surfaced_and_not_ok():
    r = ReclaimReport(
        dry_run=False,
        reclaimed=("img-a",),
        failed=(("img-b", "still held"),),
    )
    assert r.ok is False
    assert r.failed == (("img-b", "still held"),)


def test_reclaim_report_ok_is_derived_not_stored():
    field_names = {f.name for f in dataclasses.fields(ReclaimReport)}
    assert field_names == {"dry_run", "reclaimed", "failed"}
    assert "ok" not in field_names


def test_reclaim_report_defaults_empty():
    r = ReclaimReport(dry_run=True)
    assert r.reclaimed == ()
    assert r.failed == ()
    assert r.ok is True


def test_reclaim_report_freezes_iterables_to_tuples():
    reclaimed = ["a"]
    failed = [["b", "reason"]]
    r = ReclaimReport(dry_run=False, reclaimed=reclaimed, failed=failed)  # type: ignore[arg-type]
    assert isinstance(r.reclaimed, tuple)
    assert isinstance(r.failed, tuple)
    assert all(isinstance(pair, tuple) for pair in r.failed)
    assert r.failed == (("b", "reason"),)
    # Mutating the originals does not affect the frozen value.
    reclaimed.append("c")
    assert r.reclaimed == ("a",)


def test_reclaim_report_is_frozen():
    r = ReclaimReport(dry_run=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.dry_run = False  # type: ignore[misc]
