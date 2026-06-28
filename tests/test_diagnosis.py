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


# --------------------------------------------------------------------------- #
# Block 10 — the pure mapper: run_diagnostics result dict -> Diagnosis.
#
# The mapper is the ONE place the flat procedural findings become the typed
# domain model (§11.8 D3): category->domain, severity->CheckLevel, scope->
# subject. Pure / no I/O — fed plain dicts in the run_diagnostics shape.
# --------------------------------------------------------------------------- #

from kento._diagnosis import (  # noqa: E402  (Block-10 internal helpers)
    _CATEGORY_TO_DOMAIN,
    _SEVERITY_TO_LEVEL,
    _domain_for_category,
    _subject_for_finding,
    diagnosis_from_report,
)


def _raw(category, severity, scope, message="m", remediation=None):
    """A flat run_diagnostics finding dict (the exact runtime shape)."""
    return {
        "category": category,
        "severity": severity,
        "scope": scope,
        "message": message,
        "remediation": remediation,
    }


def _report(*findings):
    return {
        "checks": list(findings),
        "problem_count": sum(1 for f in findings
                             if f["severity"] in ("warn", "error")),
        "instances_scanned": 0,
    }


# -- category -> domain (the single source of truth, §11.8 D3) ----------------


def test_category_domain_map_covers_all_nine_runtime_categories():
    # The nine categories the runtime emits, mapped to the three domains.
    assert _CATEGORY_TO_DOMAIN == {
        "status": DiagnosisDomain.INSTANCE,
        "network": DiagnosisDomain.INSTANCE,
        "mount": DiagnosisDomain.INSTANCE,
        "portfwd": DiagnosisDomain.INSTANCE,
        "cloudinit": DiagnosisDomain.INSTANCE,
        "hold": DiagnosisDomain.IMAGE,
        "apparmor": DiagnosisDomain.HOST,
        "vmid": DiagnosisDomain.HOST,
        "orphan": DiagnosisDomain.HOST,
    }


@pytest.mark.parametrize("category,domain", [
    ("status", DiagnosisDomain.INSTANCE),
    ("network", DiagnosisDomain.INSTANCE),
    ("mount", DiagnosisDomain.INSTANCE),
    ("portfwd", DiagnosisDomain.INSTANCE),
    ("cloudinit", DiagnosisDomain.INSTANCE),
    ("hold", DiagnosisDomain.IMAGE),
    ("apparmor", DiagnosisDomain.HOST),
    ("vmid", DiagnosisDomain.HOST),
    ("orphan", DiagnosisDomain.HOST),
])
def test_domain_for_category_known(category, domain):
    assert _domain_for_category(category) is domain


def test_domain_for_unknown_category_defaults_host_total(caplog):
    # An unknown/future category must NOT crash — defaults to HOST with a log.
    import logging
    with caplog.at_level(logging.WARNING, logger="kento"):
        assert _domain_for_category("brand-new-check") is DiagnosisDomain.HOST
    assert any("unrecognized diagnose category" in r.message
               for r in caplog.records)


# -- severity -> CheckLevel (warn -> WARNING; unknown -> INFO, total) ---------


def test_severity_to_level_map():
    assert _SEVERITY_TO_LEVEL == {
        "ok": CheckLevel.OK,
        "info": CheckLevel.INFO,
        "warn": CheckLevel.WARNING,   # the wire word maps to the library word
        "error": CheckLevel.ERROR,
    }


def test_warn_maps_to_warning_not_a_literal_warning_string():
    d = diagnosis_from_report(_report(_raw("orphan", "warn", "ghost")))
    assert d.findings[0].level is CheckLevel.WARNING


def test_unknown_severity_defaults_info_total(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="kento"):
        d = diagnosis_from_report(_report(_raw("apparmor", "weird", "host")))
    assert d.findings[0].level is CheckLevel.INFO
    assert any("unrecognized diagnose severity" in r.message
               for r in caplog.records)


# -- subject derivation (§11.8 D3, brief #2) ---------------------------------


def test_subject_host_apparmor_and_vmid_are_none():
    # scope=="host" => subject None (about the host, not a named subject).
    assert _subject_for_finding(DiagnosisDomain.HOST, "host", "apparmor") is None
    assert _subject_for_finding(DiagnosisDomain.HOST, "host", "vmid") is None


def test_subject_instance_findings_carry_the_instance_name():
    for cat in ("status", "network", "mount", "portfwd", "cloudinit"):
        assert _subject_for_finding(
            DiagnosisDomain.INSTANCE, "mybox", cat) == "mybox"


def test_subject_host_orphan_carries_instance_name():
    # orphan is HOST domain but its runtime scope IS the instance name — carried
    # through (subject != None is allowed for HOST; brief #2).
    assert _subject_for_finding(
        DiagnosisDomain.HOST, "ghost", "orphan") == "ghost"


def test_subject_image_hold_is_none_no_message_parsing():
    # hold findings have scope "host"; the image ref is only in the message text
    # (not parsed) -> subject None (documented limitation; brief #2).
    assert _subject_for_finding(DiagnosisDomain.IMAGE, "host", "hold") is None


# -- the full mapper: a representative mixed report ---------------------------


def test_mapper_translates_every_field_faithfully():
    d = diagnosis_from_report(_report(
        _raw("status", "ok", "mybox", "running"),
        _raw("orphan", "warn", "ghost", "config gone", "kento adopt ghost"),
        _raw("apparmor", "error", "host", "parser missing", "install it"),
        _raw("hold", "ok", "host", "no stale holds"),
    ))
    by_check = {f.check: f for f in d.findings}
    # category carried verbatim into check.
    assert set(by_check) == {"status", "orphan", "apparmor", "hold"}
    # status: INSTANCE, subject=name, OK, remediation carried (None).
    s = by_check["status"]
    assert (s.domain, s.subject, s.level, s.remediation) == (
        DiagnosisDomain.INSTANCE, "mybox", CheckLevel.OK, None)
    # orphan: HOST domain, subject = the instance name, WARNING, remediation.
    o = by_check["orphan"]
    assert (o.domain, o.subject, o.level, o.remediation) == (
        DiagnosisDomain.HOST, "ghost", CheckLevel.WARNING, "kento adopt ghost")
    # apparmor: HOST, subject None, ERROR.
    a = by_check["apparmor"]
    assert (a.domain, a.subject, a.level) == (
        DiagnosisDomain.HOST, None, CheckLevel.ERROR)
    # hold: IMAGE, subject None.
    h = by_check["hold"]
    assert (h.domain, h.subject, h.level) == (
        DiagnosisDomain.IMAGE, None, CheckLevel.OK)
    # derived ok/problems honor the mapped levels.
    assert d.ok is False
    assert {f.check for f in d.problems} == {"orphan", "apparmor"}


def test_mapper_empty_report_is_empty_diagnosis():
    d = diagnosis_from_report({"checks": []})
    assert d.findings == ()
    assert d.ok is True


def test_mapper_missing_keys_are_tolerated():
    # A finding missing optional keys still maps (message/remediation default).
    d = diagnosis_from_report({"checks": [{"category": "vmid",
                                           "severity": "info", "scope": "host"}]})
    f = d.findings[0]
    assert f.message == "" and f.remediation is None
    assert f.domain is DiagnosisDomain.HOST and f.subject is None


# -- the optional narrowing filters (domain / subject) -----------------------


def test_filter_by_domain_keeps_only_that_domain():
    rep = _report(
        _raw("status", "ok", "mybox"),
        _raw("hold", "ok", "host"),
        _raw("apparmor", "ok", "host"),
    )
    d = diagnosis_from_report(rep, domain=DiagnosisDomain.IMAGE)
    assert [f.check for f in d.findings] == ["hold"]


def test_filter_by_domain_and_subject_drops_other_instances():
    # instance.diagnose filters on BOTH domain==INSTANCE AND subject==name, so a
    # foreign instance's INSTANCE finding AND a foreign orphan(HOST) both drop.
    rep = _report(
        _raw("status", "ok", "mybox"),
        _raw("network", "ok", "other"),       # different INSTANCE subject
        _raw("orphan", "warn", "mybox"),       # HOST domain — dropped by domain
        _raw("apparmor", "ok", "host"),        # HOST — dropped by domain
    )
    d = diagnosis_from_report(
        rep, domain=DiagnosisDomain.INSTANCE, subject="mybox")
    assert [f.check for f in d.findings] == ["status"]
    assert all(f.subject == "mybox" for f in d.findings)


def test_no_filters_keeps_all_findings():
    rep = _report(
        _raw("status", "ok", "mybox"),
        _raw("hold", "ok", "host"),
        _raw("apparmor", "ok", "host"),
    )
    d = diagnosis_from_report(rep)
    assert len(d.findings) == 3
