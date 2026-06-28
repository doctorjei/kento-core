"""Block 10 — the module-level ``kento.diagnose()`` entry point + the
name-collision regression (gate C foot-gun).

``kento.diagnose()`` is the global host-wide diagnostic op (HOST + every image +
every instance, both namespaces — §11.8 D3 b), mirroring the future
``kento.version()``: a top-level FUNCTION, not a handle method. It wraps the
existing ``kento.diagnose.run_diagnostics(None)`` and projects ALL findings (all
three domains) into a typed ``Diagnosis``.

The hazard this file pins: there is a sibling SUBMODULE ``kento/diagnose.py``
(the procedural runtime; the CLI does ``from kento.diagnose import
run_diagnostics``). A top-level ``def diagnose`` in ``kento/__init__.py`` and the
submodule ``kento.diagnose`` share the same parent-package attribute name. The
resolution (import the submodule into ``sys.modules`` BEFORE binding the
function) must keep BOTH usable: ``kento.diagnose`` resolves to the FUNCTION, and
``from kento.diagnose import run_diagnostics`` still finds the MODULE — in EITHER
import order. These tests prove the coexistence.

Spec: ~/workspace/kento-core-api-design.md §11.8 D3 (b); brief judgment call #4.
"""

import types
from unittest.mock import patch

import pytest

import kento
from kento import CheckLevel, Diagnosis, DiagnosisDomain


# --------------------------------------------------------------------------- #
# The name-collision regression — kento.diagnose stays the FUNCTION while
# from kento.diagnose import ... still finds the MODULE (both orders).
# --------------------------------------------------------------------------- #


def test_kento_diagnose_is_the_function_not_the_module():
    assert callable(kento.diagnose)
    assert not isinstance(kento.diagnose, types.ModuleType)
    # And it is in the curated public surface.
    assert "diagnose" in kento.__all__


def test_submodule_import_after_function_binding_coexists():
    # The CLI's exact import. It must find the MODULE's run_diagnostics, and it
    # must NOT clobber the top-level function back to the module.
    from kento.diagnose import run_diagnostics
    assert callable(run_diagnostics)
    assert callable(kento.diagnose)
    assert not isinstance(kento.diagnose, types.ModuleType)


def test_from_kento_import_diagnose_is_the_function():
    # `from kento import diagnose` resolves the function (the package attribute),
    # not the module — even after the submodule has been imported above.
    from kento import diagnose as d
    assert callable(d)
    assert d is kento.diagnose
    assert not isinstance(d, types.ModuleType)


def test_submodule_run_diagnostics_reachable_via_sys_modules():
    # kento.diagnose (the attr) is the function, but the cached submodule still
    # exposes run_diagnostics through sys.modules / importlib.
    import importlib
    import sys

    mod = importlib.import_module("kento.diagnose")
    assert isinstance(mod, types.ModuleType)
    assert hasattr(mod, "run_diagnostics")
    assert sys.modules["kento.diagnose"] is mod
    # The function attribute and the module are distinct objects.
    assert kento.diagnose is not mod


def test_reverse_order_in_subprocess_keeps_function():
    # A FRESH interpreter that imports the submodule FIRST, then the package,
    # then calls the function — proves the binding survives independent of order
    # (the in-process tests above can't reset module state). Run out-of-process.
    import subprocess
    import sys

    code = (
        "from kento.diagnose import run_diagnostics\n"
        "import kento\n"
        "import types\n"
        "assert callable(kento.diagnose), 'kento.diagnose not callable'\n"
        "assert not isinstance(kento.diagnose, types.ModuleType), 'is module!'\n"
        "assert callable(run_diagnostics), 'run_diagnostics not callable'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


# --------------------------------------------------------------------------- #
# kento.diagnose() behavior — global scan, ALL domains, typed Diagnosis.
# --------------------------------------------------------------------------- #


def _df(category, severity, scope, message="m", remediation=None):
    return {"category": category, "severity": severity, "scope": scope,
            "message": message, "remediation": remediation}


def _report(*findings):
    return {"checks": list(findings),
            "problem_count": sum(1 for f in findings
                                 if f["severity"] in ("warn", "error")),
            "instances_scanned": 0}


def test_module_diagnose_runs_global_scan_and_keeps_all_domains():
    report = _report(
        _df("apparmor", "ok", "host"),          # HOST
        _df("hold", "ok", "host"),              # IMAGE
        _df("status", "ok", "mybox"),           # INSTANCE
        _df("orphan", "warn", "ghost"),         # HOST (subject=name)
    )
    with patch("kento.diagnose.run_diagnostics",
               return_value=report) as mock_run:
        result = kento.diagnose()
    # Global: run_diagnostics(None); ALL findings projected (no filter).
    mock_run.assert_called_once_with(None)
    assert isinstance(result, Diagnosis)
    assert {f.check for f in result.findings} == {
        "apparmor", "hold", "status", "orphan"}
    domains = {f.domain for f in result.findings}
    assert domains == {DiagnosisDomain.HOST, DiagnosisDomain.IMAGE,
                       DiagnosisDomain.INSTANCE}
    # Derived ok/problems honor the mapped levels.
    assert result.ok is False
    assert {f.check for f in result.problems} == {"orphan"}
    # orphan's HOST finding carries the instance name as subject.
    orphan = next(f for f in result.findings if f.check == "orphan")
    assert orphan.domain is DiagnosisDomain.HOST
    assert orphan.subject == "ghost"
    assert orphan.level is CheckLevel.WARNING


def test_module_diagnose_empty_is_ok():
    with patch("kento.diagnose.run_diagnostics", return_value=_report()):
        result = kento.diagnose()
    assert result.findings == ()
    assert result.ok is True


def test_module_diagnose_returns_typed_value_not_dict():
    with patch("kento.diagnose.run_diagnostics", return_value=_report()):
        result = kento.diagnose()
    assert isinstance(result, Diagnosis)
    assert not isinstance(result, dict)
