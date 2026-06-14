"""Tests for kento.reconcile — shared orphan detection (find_orphans).

Style mirrors test_list.py / test_diagnose.py: per-instance state dirs under
tmp_path, module-level LXC_BASE/VM_BASE patched, pve_config_exists mocked.

reconcile.find_orphans probes through kento.reconcile.pve_config_exists, so
these tests patch that name. The safety invariant under test: an instance is
returned ONLY when its PVE config is *definitively* gone; an indeterminate
probe (PermissionError/OSError) is never classified as an orphan.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from kento.reconcile import (find_orphans, format_reap, reap_orphans,
                             _is_orphan, _orphan_vmid)


# --- fixtures ------------------------------------------------------------


def _mk_instance(base, dir_name, *, image="img:latest", mode="lxc",
                 name=None, vmid=None):
    d = base / dir_name
    d.mkdir(parents=True, exist_ok=True)
    (d / "kento-image").write_text(image + "\n")
    (d / "kento-mode").write_text(mode + "\n")
    if name is not None:
        (d / "kento-name").write_text(name + "\n")
    if vmid is not None:
        (d / "kento-vmid").write_text(str(vmid) + "\n")
    (d / "kento-state").write_text(str(d) + "\n")
    return d


def _patch_bases(lxc, vm):
    """Patch the bases find_orphans enumerates over."""
    return patch.multiple("kento.reconcile", LXC_BASE=lxc, VM_BASE=vm)


@pytest.fixture
def bases(tmp_path):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    return lxc, vm


# --- detection: pve-lxc --------------------------------------------------


def test_pve_lxc_orphan_detected(bases):
    lxc, vm = bases
    # pve-lxc dir name IS the vmid.
    _mk_instance(lxc, "101", mode="pve", name="ghost")

    with _patch_bases(lxc, vm), \
         patch("kento.reconcile.pve_config_exists", return_value=False):
        orphans = find_orphans()

    assert len(orphans) == 1
    o = orphans[0]
    assert o["name"] == "ghost"
    assert o["vmid"] == 101
    assert o["mode"] == "pve"
    assert o["container_dir"] == lxc / "101"
    assert o["image"] == "img:latest"


def test_pve_lxc_orphan_name_defaults_to_dir(bases):
    """No kento-name file => display name falls back to the dir name."""
    lxc, vm = bases
    _mk_instance(lxc, "150", mode="pve")  # no name

    with _patch_bases(lxc, vm), \
         patch("kento.reconcile.pve_config_exists", return_value=False):
        orphans = find_orphans()

    assert [o["name"] for o in orphans] == ["150"]
    assert orphans[0]["vmid"] == 150


# --- detection: pve-vm ---------------------------------------------------


def test_pve_vm_orphan_detected_via_vmid_file(bases):
    lxc, vm = bases
    _mk_instance(vm, "myvm", mode="pve-vm", name="myvm", vmid=200)

    with _patch_bases(lxc, vm), \
         patch("kento.reconcile.pve_config_exists", return_value=False):
        orphans = find_orphans()

    assert len(orphans) == 1
    o = orphans[0]
    assert o["name"] == "myvm"
    assert o["vmid"] == 200
    assert o["mode"] == "pve-vm"
    assert o["container_dir"] == vm / "myvm"


def test_pve_vm_missing_vmid_file_is_orphan(bases):
    """A pve-vm with no kento-vmid => vmid is None => treated as gone
    (mirrors the inline `check_vmid is None or ...` logic)."""
    lxc, vm = bases
    _mk_instance(vm, "novmid", mode="pve-vm", name="novmid")  # no vmid file

    with _patch_bases(lxc, vm), \
         patch("kento.reconcile.pve_config_exists", return_value=True):
        # config check should never even be consulted (vmid is None)
        orphans = find_orphans()

    assert len(orphans) == 1
    assert orphans[0]["name"] == "novmid"
    assert orphans[0]["vmid"] is None


# --- healthy instances NOT returned --------------------------------------


def test_healthy_pve_instance_not_returned(bases):
    lxc, vm = bases
    _mk_instance(lxc, "102", mode="pve", name="live")

    with _patch_bases(lxc, vm), \
         patch("kento.reconcile.pve_config_exists", return_value=True):
        orphans = find_orphans()

    assert orphans == []


def test_plain_lxc_and_vm_never_returned(bases):
    lxc, vm = bases
    _mk_instance(lxc, "box", mode="lxc", name="box")
    _mk_instance(vm, "qbox", mode="vm", name="qbox")

    # Even if the config probe would say "gone", plain modes can't orphan.
    with _patch_bases(lxc, vm), \
         patch("kento.reconcile.pve_config_exists", return_value=False) as cfg:
        orphans = find_orphans()

    assert orphans == []
    # Plain modes must not even consult the PVE config.
    cfg.assert_not_called()


# --- safety invariant: indeterminate probe is NOT an orphan --------------


def test_indeterminate_permission_error_not_orphan(bases):
    lxc, vm = bases
    _mk_instance(lxc, "103", mode="pve", name="maybe")

    with _patch_bases(lxc, vm), \
         patch("kento.reconcile.pve_config_exists",
               side_effect=PermissionError("needs root")):
        orphans = find_orphans()

    assert orphans == []


def test_indeterminate_oserror_not_orphan(bases):
    lxc, vm = bases
    _mk_instance(vm, "vmmaybe", mode="pve-vm", name="vmmaybe", vmid=300)

    with _patch_bases(lxc, vm), \
         patch("kento.reconcile.pve_config_exists",
               side_effect=OSError("pmxcfs hiccup")):
        orphans = find_orphans()

    assert orphans == []


# --- robustness: garbage dirs, missing bases -----------------------------


def test_non_integer_pve_lxc_dir_tolerated(bases):
    """A pve-lxc dir whose name isn't an int still enumerates; vmid falls
    back to the raw string (the gone-check uses the dir name verbatim,
    exactly as the live code does)."""
    lxc, vm = bases
    _mk_instance(lxc, "weird-name", mode="pve", name="weird")

    with _patch_bases(lxc, vm), \
         patch("kento.reconcile.pve_config_exists", return_value=False):
        orphans = find_orphans()

    assert len(orphans) == 1
    assert orphans[0]["name"] == "weird"
    # Non-integer vmid preserved as the raw string, not coerced/dropped.
    assert orphans[0]["vmid"] == "weird-name"


def test_missing_base_dirs_ok(tmp_path):
    """Neither base exists => no crash, empty result."""
    lxc = tmp_path / "nope-lxc"
    vm = tmp_path / "nope-vm"

    with patch.multiple("kento.reconcile", LXC_BASE=lxc, VM_BASE=vm), \
         patch("kento.reconcile.pve_config_exists", return_value=False):
        orphans = find_orphans()

    assert orphans == []


def test_stray_non_instance_dirs_skipped(bases):
    """A dir without kento-image is not an instance and is skipped silently."""
    lxc, vm = bases
    (lxc / "junk").mkdir()  # no kento-image
    _mk_instance(lxc, "104", mode="pve", name="real")

    with _patch_bases(lxc, vm), \
         patch("kento.reconcile.pve_config_exists", return_value=False):
        orphans = find_orphans()

    assert [o["name"] for o in orphans] == ["real"]


# --- mixed set: only the true orphans returned ---------------------------


def test_mixed_returns_only_true_orphans(bases):
    lxc, vm = bases
    _mk_instance(lxc, "101", mode="pve", name="ghost")       # orphan
    _mk_instance(lxc, "102", mode="pve", name="live")        # healthy
    _mk_instance(lxc, "box", mode="lxc", name="box")         # plain lxc
    _mk_instance(vm, "ghostvm", mode="pve-vm", name="ghostvm", vmid=201)  # orphan
    _mk_instance(vm, "livevm", mode="pve-vm", name="livevm", vmid=202)    # healthy
    _mk_instance(vm, "qbox", mode="vm", name="qbox")         # plain vm

    def cfg(vmid, mode):
        # "live"/"livevm" present; the ghosts gone.
        return vmid in ("102", "202")

    with _patch_bases(lxc, vm), \
         patch("kento.reconcile.pve_config_exists", side_effect=cfg):
        orphans = find_orphans()

    names = sorted(o["name"] for o in orphans)
    assert names == ["ghost", "ghostvm"]


# --- scope narrowing -----------------------------------------------------


def test_scope_lxc_only_scans_lxc(bases):
    lxc, vm = bases
    _mk_instance(lxc, "101", mode="pve", name="ghost")
    _mk_instance(vm, "ghostvm", mode="pve-vm", name="ghostvm", vmid=201)

    with _patch_bases(lxc, vm), \
         patch("kento.reconcile.pve_config_exists", return_value=False):
        orphans = find_orphans(scope="lxc")

    assert [o["name"] for o in orphans] == ["ghost"]


def test_scope_vm_only_scans_vm(bases):
    lxc, vm = bases
    _mk_instance(lxc, "101", mode="pve", name="ghost")
    _mk_instance(vm, "ghostvm", mode="pve-vm", name="ghostvm", vmid=201)

    with _patch_bases(lxc, vm), \
         patch("kento.reconcile.pve_config_exists", return_value=False):
        orphans = find_orphans(scope="vm")

    assert [o["name"] for o in orphans] == ["ghostvm"]


# --- the shared predicate directly ---------------------------------------


def test_is_orphan_tristate(bases):
    lxc, vm = bases
    d = _mk_instance(lxc, "101", mode="pve", name="ghost")

    assert _is_orphan(d, "lxc") is False          # non-pve can't orphan
    assert _is_orphan(d, "pve", lambda v, m: True) is False   # config present
    assert _is_orphan(d, "pve", lambda v, m: False) is True   # config gone

    def boom(v, m):
        raise PermissionError
    assert _is_orphan(d, "pve", boom) is None     # indeterminate


def test_orphan_vmid_resolution(bases):
    lxc, vm = bases
    dlxc = _mk_instance(lxc, "105", mode="pve", name="g")
    dvm = _mk_instance(vm, "vmx", mode="pve-vm", name="g2", vmid=205)
    dvm_no = _mk_instance(vm, "vmy", mode="pve-vm", name="g3")

    assert _orphan_vmid(dlxc, "pve") == "105"      # dir name
    assert _orphan_vmid(dvm, "pve-vm") == "205"    # kento-vmid file
    assert _orphan_vmid(dvm_no, "pve-vm") is None  # missing file


# --- reap_orphans: dry-run (reap=False) ----------------------------------


def test_reap_dry_run_lists_but_never_destroys(bases):
    lxc, vm = bases
    _mk_instance(lxc, "101", mode="pve", name="ghost")
    _mk_instance(vm, "ghostvm", mode="pve-vm", name="ghostvm", vmid=201)

    with _patch_bases(lxc, vm), \
         patch("kento.reconcile.pve_config_exists", return_value=False), \
         patch("kento.destroy.destroy") as destroy:
        results = reap_orphans(reap=False)

    destroy.assert_not_called()
    names = sorted(r["name"] for r in results)
    assert names == ["ghost", "ghostvm"]
    # Dry-run: every entry untouched.
    for r in results:
        assert r["reaped"] is False
        assert r["error"] is None


def test_reap_no_orphans_empty_no_destroy(bases):
    lxc, vm = bases
    _mk_instance(lxc, "102", mode="pve", name="live")  # healthy

    with _patch_bases(lxc, vm), \
         patch("kento.reconcile.pve_config_exists", return_value=True), \
         patch("kento.destroy.destroy") as destroy:
        results = reap_orphans(reap=False)
        results_yes = reap_orphans(reap=True)

    assert results == []
    assert results_yes == []
    destroy.assert_not_called()


# --- reap_orphans: reap=True ---------------------------------------------


def test_reap_destroys_each_orphan_with_force_and_resolved_args(bases):
    lxc, vm = bases
    _mk_instance(lxc, "101", mode="pve", name="ghost")
    _mk_instance(vm, "ghostvm", mode="pve-vm", name="ghostvm", vmid=201)

    with _patch_bases(lxc, vm), \
         patch("kento.reconcile.pve_config_exists", return_value=False), \
         patch("kento.destroy.destroy") as destroy:
        results = reap_orphans(reap=True)

    # One destroy per orphan, force=True, with the resolved container_dir/mode.
    assert destroy.call_count == 2
    by_name = {c.args[0]: c for c in destroy.call_args_list}
    assert set(by_name) == {"ghost", "ghostvm"}

    ghost = by_name["ghost"]
    assert ghost.kwargs["force"] is True
    assert ghost.kwargs["container_dir"] == lxc / "101"
    assert ghost.kwargs["mode"] == "pve"

    ghostvm = by_name["ghostvm"]
    assert ghostvm.kwargs["force"] is True
    assert ghostvm.kwargs["container_dir"] == vm / "ghostvm"
    assert ghostvm.kwargs["mode"] == "pve-vm"

    for r in results:
        assert r["reaped"] is True
        assert r["error"] is None


def test_reap_per_orphan_failure_isolated(bases):
    lxc, vm = bases
    _mk_instance(lxc, "101", mode="pve", name="ghost")     # will fail
    _mk_instance(vm, "ghostvm", mode="pve-vm", name="ghostvm", vmid=201)  # ok

    def fake_destroy(name, force=False, *, container_dir=None, mode=None):
        if name == "ghost":
            raise RuntimeError("boom")

    with _patch_bases(lxc, vm), \
         patch("kento.reconcile.pve_config_exists", return_value=False), \
         patch("kento.destroy.destroy", side_effect=fake_destroy) as destroy:
        results = reap_orphans(reap=True)

    # Both attempted despite the first failing (isolation).
    assert destroy.call_count == 2
    by_name = {r["name"]: r for r in results}
    assert by_name["ghost"]["reaped"] is False
    assert by_name["ghost"]["error"] == "boom"
    assert by_name["ghostvm"]["reaped"] is True
    assert by_name["ghostvm"]["error"] is None


def test_reap_only_acts_on_find_orphans_output(bases):
    """A healthy + a plain instance must never be reaped (invariant lives in
    find_orphans; reap_orphans must not enumerate independently)."""
    lxc, vm = bases
    _mk_instance(lxc, "101", mode="pve", name="ghost")    # orphan
    _mk_instance(lxc, "102", mode="pve", name="live")     # healthy
    _mk_instance(lxc, "box", mode="lxc", name="box")      # plain lxc

    def cfg(vmid, mode):
        return vmid == "102"  # only 'live' present

    with _patch_bases(lxc, vm), \
         patch("kento.reconcile.pve_config_exists", side_effect=cfg), \
         patch("kento.destroy.destroy") as destroy:
        results = reap_orphans(reap=True)

    assert [r["name"] for r in results] == ["ghost"]
    assert destroy.call_count == 1
    assert destroy.call_args.args[0] == "ghost"


# --- format_reap ---------------------------------------------------------


def test_format_reap_empty():
    assert format_reap([], reaped=False) == "Orphans: none found."
    assert format_reap([], reaped=True) == "Orphans: none found."


def test_format_reap_dry_run_hints_yes():
    results = [{"name": "ghost", "vmid": 101, "mode": "pve",
                "reaped": False, "error": None}]
    out = format_reap(results, reaped=False)
    assert "Orphans:" in out
    assert "WOULD be destroyed" in out
    assert "ghost" in out and "101" in out
    assert "--orphans --yes" in out


def test_format_reap_reaped_reports_each():
    results = [
        {"name": "ghost", "vmid": 101, "mode": "pve",
         "reaped": True, "error": None},
        {"name": "bad", "vmid": 202, "mode": "pve-vm",
         "reaped": False, "error": "boom"},
    ]
    out = format_reap(results, reaped=True)
    assert "reaped ghost" in out
    assert "101" in out
    assert "FAILED bad" in out and "boom" in out
    assert "Destroyed 1 orphan(s), 1 failed." in out
