"""Tests for `kento diagnose` — read-only health/triage.

Style mirrors test_list.py / test_images.py: per-instance state dirs under
tmp_path, module-level LXC_BASE/VM_BASE patched, subprocess/helpers mocked.

The diagnose module reuses detection from other modules; these tests
monkeypatch the *names diagnose imports* (e.g. kento.diagnose.pve_config_exists)
so they exercise diagnose's wiring without touching the real host.
"""

import subprocess
from unittest.mock import patch

import pytest

from kento.diagnose import run_diagnostics, format_diagnostics


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
    """Patch the bases diagnose enumerates over."""
    return patch.multiple("kento.diagnose", LXC_BASE=lxc, VM_BASE=vm)


def _common_mocks(*, apparmor_active=False, parser_present=True,
                  holds=None, guest_names=None, pve_config=True,
                  next_vmid=100, recorded_vmids=None, is_running=False):
    """Bundle the host/instance helper patches diagnose calls into.

    Returns a context-manager list the caller enters with ExitStack-style
    nesting via contextlib; we just return a single patch.multiple plus
    extras combined by the caller.
    """
    return holds, guest_names


# --- a clean host: no problems ------------------------------------------


def test_clean_host_no_problems(tmp_path):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    _mk_instance(lxc, "box", name="box")

    with _patch_bases(lxc, vm), \
         patch("kento.diagnose._apparmor_active", return_value=False), \
         patch("kento.diagnose._apparmor_parser_present", return_value=True), \
         patch("kento.diagnose._holds", return_value=[]), \
         patch("kento.diagnose._guest_names", return_value={"box"}), \
         patch("kento.diagnose.is_pve", return_value=False), \
         patch("kento.diagnose.is_running", return_value=False), \
         patch("kento.diagnose.os.path.ismount", return_value=False):
        report = run_diagnostics()

    assert report["problem_count"] == 0
    assert report["instances_scanned"] == 1
    assert isinstance(report["checks"], list)
    # Every finding well-formed.
    for f in report["checks"]:
        assert set(f) == {"category", "severity", "scope", "message",
                          "remediation"}
        assert f["severity"] in ("ok", "info", "warn", "error")


def test_report_shape_keys_and_types(tmp_path):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    _mk_instance(lxc, "box", name="box")

    with _patch_bases(lxc, vm), \
         patch("kento.diagnose._apparmor_active", return_value=False), \
         patch("kento.diagnose._apparmor_parser_present", return_value=True), \
         patch("kento.diagnose._holds", return_value=[]), \
         patch("kento.diagnose._guest_names", return_value={"box"}), \
         patch("kento.diagnose.is_pve", return_value=False), \
         patch("kento.diagnose.is_running", return_value=False), \
         patch("kento.diagnose.os.path.ismount", return_value=False):
        report = run_diagnostics()

    assert set(report) == {"checks", "problem_count", "instances_scanned"}
    assert isinstance(report["problem_count"], int)
    assert isinstance(report["instances_scanned"], int)
    assert isinstance(report["checks"], list)


# --- orphan detection (pve modes) ---------------------------------------


def test_orphan_pve_lxc_detected_warn(tmp_path):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    # pve-lxc dir name IS the vmid.
    _mk_instance(lxc, "101", mode="pve", name="ghost")

    with _patch_bases(lxc, vm), \
         patch("kento.diagnose._apparmor_active", return_value=False), \
         patch("kento.diagnose._apparmor_parser_present", return_value=True), \
         patch("kento.diagnose._holds", return_value=[]), \
         patch("kento.diagnose._guest_names", return_value={"ghost"}), \
         patch("kento.diagnose.is_pve", return_value=True), \
         patch("kento.diagnose.is_running", return_value=False), \
         patch("kento.diagnose.next_vmid", return_value=100), \
         patch("kento.diagnose._kento_recorded_vmids", return_value={101}), \
         patch("kento.diagnose.os.path.ismount", return_value=False), \
         patch("kento.diagnose.pve_config_exists", return_value=False):
        report = run_diagnostics()

    orphan = [f for f in report["checks"] if f["category"] == "orphan"]
    assert orphan, "expected an orphan finding"
    assert orphan[0]["severity"] == "warn"
    assert orphan[0]["scope"] == "ghost"
    # The remediation offers both the heal (adopt) and the discard (destroy -f).
    remediation = orphan[0]["remediation"] or ""
    assert "destroy -f" in remediation
    assert "adopt" in remediation
    assert report["problem_count"] >= 1


def test_orphan_pve_vm_uses_vmid_file(tmp_path):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    _mk_instance(vm, "myvm", mode="pve-vm", name="myvm", vmid=200)

    with _patch_bases(lxc, vm), \
         patch("kento.diagnose._apparmor_active", return_value=False), \
         patch("kento.diagnose._apparmor_parser_present", return_value=True), \
         patch("kento.diagnose._holds", return_value=[]), \
         patch("kento.diagnose._guest_names", return_value={"myvm"}), \
         patch("kento.diagnose.is_pve", return_value=True), \
         patch("kento.diagnose.is_running", return_value=False), \
         patch("kento.diagnose.next_vmid", return_value=100), \
         patch("kento.diagnose._kento_recorded_vmids", return_value={200}), \
         patch("kento.diagnose.os.path.ismount", return_value=False), \
         patch("kento.diagnose.pve_config_exists", return_value=False):
        report = run_diagnostics()

    orphan = [f for f in report["checks"] if f["category"] == "orphan"]
    assert orphan and orphan[0]["scope"] == "myvm"
    assert orphan[0]["severity"] == "warn"


def test_pve_config_present_no_orphan(tmp_path):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    _mk_instance(lxc, "102", mode="pve", name="live")

    with _patch_bases(lxc, vm), \
         patch("kento.diagnose._apparmor_active", return_value=False), \
         patch("kento.diagnose._apparmor_parser_present", return_value=True), \
         patch("kento.diagnose._holds", return_value=[]), \
         patch("kento.diagnose._guest_names", return_value={"live"}), \
         patch("kento.diagnose.is_pve", return_value=True), \
         patch("kento.diagnose.is_running", return_value=False), \
         patch("kento.diagnose.next_vmid", return_value=100), \
         patch("kento.diagnose._kento_recorded_vmids", return_value={102}), \
         patch("kento.diagnose.os.path.ismount", return_value=False), \
         patch("kento.diagnose.pve_config_exists", return_value=True):
        report = run_diagnostics()

    orphan = [f for f in report["checks"] if f["category"] == "orphan"]
    # An "ok" orphan finding is fine; there must be no warn/error.
    assert all(f["severity"] not in ("warn", "error") for f in orphan)


# --- apparmor (host) -----------------------------------------------------


def test_apparmor_error_when_active_parser_absent(tmp_path):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()

    with _patch_bases(lxc, vm), \
         patch("kento.diagnose._apparmor_active", return_value=True), \
         patch("kento.diagnose._apparmor_parser_present", return_value=False), \
         patch("kento.diagnose._holds", return_value=[]), \
         patch("kento.diagnose._guest_names", return_value=set()), \
         patch("kento.diagnose.is_pve", return_value=False), \
         patch("kento.diagnose.is_running", return_value=False), \
         patch.dict("os.environ", {}, clear=False):
        report = run_diagnostics()

    aa = [f for f in report["checks"] if f["category"] == "apparmor"]
    assert aa and aa[0]["severity"] == "error"
    assert aa[0]["scope"] == "host"
    assert "apparmor_parser" in (aa[0]["remediation"] or "")
    assert report["problem_count"] >= 1


def test_apparmor_ok_when_parser_present(tmp_path):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()

    with _patch_bases(lxc, vm), \
         patch("kento.diagnose._apparmor_active", return_value=True), \
         patch("kento.diagnose._apparmor_parser_present", return_value=True), \
         patch("kento.diagnose._holds", return_value=[]), \
         patch("kento.diagnose._guest_names", return_value=set()), \
         patch("kento.diagnose.is_pve", return_value=False), \
         patch("kento.diagnose.is_running", return_value=False):
        report = run_diagnostics()

    aa = [f for f in report["checks"] if f["category"] == "apparmor"]
    assert aa and aa[0]["severity"] == "ok"


def test_apparmor_unconfined_env_is_ok(tmp_path):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()

    with _patch_bases(lxc, vm), \
         patch("kento.diagnose._apparmor_active", return_value=True), \
         patch("kento.diagnose._apparmor_parser_present", return_value=False), \
         patch("kento.diagnose._holds", return_value=[]), \
         patch("kento.diagnose._guest_names", return_value=set()), \
         patch("kento.diagnose.is_pve", return_value=False), \
         patch("kento.diagnose.is_running", return_value=False), \
         patch.dict("os.environ", {"KENTO_APPARMOR_PROFILE": "unconfined"}):
        report = run_diagnostics()

    aa = [f for f in report["checks"] if f["category"] == "apparmor"]
    assert aa and aa[0]["severity"] == "ok"


# --- port-forward state (per instance) ----------------------------------


def test_portfwd_error_marker(tmp_path):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    d = _mk_instance(lxc, "box", name="box")
    (d / "kento-portfwd-error").write_text("iptables: permission denied\n")

    with _patch_bases(lxc, vm), \
         patch("kento.diagnose._apparmor_active", return_value=False), \
         patch("kento.diagnose._apparmor_parser_present", return_value=True), \
         patch("kento.diagnose._holds", return_value=[]), \
         patch("kento.diagnose._guest_names", return_value={"box"}), \
         patch("kento.diagnose.is_pve", return_value=False), \
         patch("kento.diagnose.is_running", return_value=False), \
         patch("kento.diagnose.os.path.ismount", return_value=False):
        report = run_diagnostics()

    pf = [f for f in report["checks"] if f["category"] == "portfwd"]
    assert pf and pf[0]["severity"] in ("warn", "error")
    assert "permission denied" in pf[0]["message"]
    assert pf[0]["scope"] == "box"


def test_portfwd_active_ok(tmp_path):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    d = _mk_instance(lxc, "box", name="box")
    (d / "kento-portfwd-active").write_text("8080:80:10.0.0.5\n")
    (d / "kento-portfwd-backend").write_text("nft\n")

    with _patch_bases(lxc, vm), \
         patch("kento.diagnose._apparmor_active", return_value=False), \
         patch("kento.diagnose._apparmor_parser_present", return_value=True), \
         patch("kento.diagnose._holds", return_value=[]), \
         patch("kento.diagnose._guest_names", return_value={"box"}), \
         patch("kento.diagnose.is_pve", return_value=False), \
         patch("kento.diagnose.is_running", return_value=False), \
         patch("kento.diagnose.os.path.ismount", return_value=False):
        report = run_diagnostics()

    pf = [f for f in report["checks"] if f["category"] == "portfwd"]
    assert pf and pf[0]["severity"] == "ok"
    assert "nft" in pf[0]["message"]


# --- stale image holds (host) -------------------------------------------


def test_stale_hold_detected(tmp_path):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    _mk_instance(lxc, "box", name="box")
    # hold for "ghost" whose guest no longer exists.
    holds = [("box", "imgA:latest"), ("ghost", "imgB:latest")]

    with _patch_bases(lxc, vm), \
         patch("kento.diagnose._apparmor_active", return_value=False), \
         patch("kento.diagnose._apparmor_parser_present", return_value=True), \
         patch("kento.diagnose._holds", return_value=holds), \
         patch("kento.diagnose._guest_names", return_value={"box"}), \
         patch("kento.diagnose.is_pve", return_value=False), \
         patch("kento.diagnose.is_running", return_value=False), \
         patch("kento.diagnose.os.path.ismount", return_value=False):
        report = run_diagnostics()

    sh = [f for f in report["checks"] if f["category"] == "hold"]
    warns = [f for f in sh if f["severity"] == "warn"]
    assert warns, "expected a stale hold warn"
    assert "ghost" in warns[0]["message"]
    assert "prune" in (warns[0]["remediation"] or "")


def test_no_stale_holds_ok(tmp_path):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    _mk_instance(lxc, "box", name="box")
    holds = [("box", "imgA:latest")]

    with _patch_bases(lxc, vm), \
         patch("kento.diagnose._apparmor_active", return_value=False), \
         patch("kento.diagnose._apparmor_parser_present", return_value=True), \
         patch("kento.diagnose._holds", return_value=holds), \
         patch("kento.diagnose._guest_names", return_value={"box"}), \
         patch("kento.diagnose.is_pve", return_value=False), \
         patch("kento.diagnose.is_running", return_value=False), \
         patch("kento.diagnose.os.path.ismount", return_value=False):
        report = run_diagnostics()

    sh = [f for f in report["checks"] if f["category"] == "hold"]
    assert all(f["severity"] != "warn" for f in sh)


# --- image-hold / guest image-ID drift (host) ---------------------------


def test_hold_drift_detected(tmp_path):
    """Hold pins an old id, guest records a new id -> warn finding."""
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    d = _mk_instance(lxc, "box", name="box")
    (d / "kento-image-id").write_text("sha256:newnewnewnew0000\n")

    with _patch_bases(lxc, vm), \
         patch("kento.diagnose._apparmor_active", return_value=False), \
         patch("kento.diagnose._apparmor_parser_present", return_value=True), \
         patch("kento.diagnose._holds", return_value=[("box", "img:latest")]), \
         patch("kento.diagnose._hold_image_ids",
               return_value={"box": "sha256:oldoldoldold0000"}), \
         patch("kento.diagnose._guest_names", return_value={"box"}), \
         patch("kento.diagnose.is_pve", return_value=False), \
         patch("kento.diagnose.is_running", return_value=False), \
         patch("kento.diagnose.os.path.ismount", return_value=False):
        report = run_diagnostics()

    warns = [f for f in report["checks"]
             if f["category"] == "hold" and f["severity"] == "warn"]
    drift = [f for f in warns if "re-pin" in f["message"]]
    assert drift, "expected a hold-drift warn"
    assert "box" in drift[0]["message"]
    assert "oldoldoldold" in drift[0]["message"]
    assert "newnewnewnew" in drift[0]["message"]
    assert drift[0]["remediation"] == "kento scrub box"


def test_hold_drift_aligned_no_warn(tmp_path):
    """Hold id == guest id -> no drift warn."""
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    d = _mk_instance(lxc, "box", name="box")
    (d / "kento-image-id").write_text("sha256:samesame0000\n")

    with _patch_bases(lxc, vm), \
         patch("kento.diagnose._apparmor_active", return_value=False), \
         patch("kento.diagnose._apparmor_parser_present", return_value=True), \
         patch("kento.diagnose._holds", return_value=[("box", "img:latest")]), \
         patch("kento.diagnose._hold_image_ids",
               return_value={"box": "sha256:samesame0000"}), \
         patch("kento.diagnose._guest_names", return_value={"box"}), \
         patch("kento.diagnose.is_pve", return_value=False), \
         patch("kento.diagnose.is_running", return_value=False), \
         patch("kento.diagnose.os.path.ismount", return_value=False):
        report = run_diagnostics()

    drift = [f for f in report["checks"]
             if f["category"] == "hold" and f["severity"] == "warn"
             and "re-pin" in f["message"]]
    assert not drift


def test_hold_drift_legacy_skipped(tmp_path):
    """Guest with no kento-image-id file (or hold with no label) is skipped
    silently — no warn, no noise."""
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    _mk_instance(lxc, "box", name="box")  # no kento-image-id file

    with _patch_bases(lxc, vm), \
         patch("kento.diagnose._apparmor_active", return_value=False), \
         patch("kento.diagnose._apparmor_parser_present", return_value=True), \
         patch("kento.diagnose._holds", return_value=[("box", "img:latest")]), \
         patch("kento.diagnose._hold_image_ids", return_value={}), \
         patch("kento.diagnose._guest_names", return_value={"box"}), \
         patch("kento.diagnose.is_pve", return_value=False), \
         patch("kento.diagnose.is_running", return_value=False), \
         patch("kento.diagnose.os.path.ismount", return_value=False):
        report = run_diagnostics()

    drift = [f for f in report["checks"]
             if f["category"] == "hold" and f["severity"] == "warn"
             and "re-pin" in f["message"]]
    assert not drift


# --- per-instance scoping with name= ------------------------------------


def test_name_scopes_to_one_instance(tmp_path):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    da = _mk_instance(lxc, "a", name="a")
    (da / "kento-portfwd-active").write_text("8080:80:10.0.0.5\n")
    (da / "kento-portfwd-backend").write_text("nft\n")
    db = _mk_instance(lxc, "b", name="b")
    (db / "kento-portfwd-active").write_text("9090:90:10.0.0.6\n")

    with _patch_bases(lxc, vm), \
         patch("kento.diagnose._apparmor_active", return_value=False), \
         patch("kento.diagnose._apparmor_parser_present", return_value=True), \
         patch("kento.diagnose._holds", return_value=[]), \
         patch("kento.diagnose._guest_names", return_value={"a", "b"}), \
         patch("kento.diagnose.is_pve", return_value=False), \
         patch("kento.diagnose.is_running", return_value=False), \
         patch("kento.diagnose.os.path.ismount", return_value=False):
        report = run_diagnostics(name="a")

    assert report["instances_scanned"] == 1
    inst_scopes = {f["scope"] for f in report["checks"] if f["scope"] != "host"}
    # Only "a" is in scope; "b" must never appear.
    assert inst_scopes == {"a"}
    assert "b" not in inst_scopes


def test_name_unknown_raises(tmp_path):
    from kento.errors import InstanceNotFoundError
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    _mk_instance(lxc, "a", name="a")

    with _patch_bases(lxc, vm), \
         patch("kento.LXC_BASE", lxc), patch("kento.VM_BASE", vm), \
         patch("kento.diagnose._apparmor_active", return_value=False), \
         patch("kento.diagnose._apparmor_parser_present", return_value=True), \
         patch("kento.diagnose._holds", return_value=[]), \
         patch("kento.diagnose._guest_names", return_value={"a"}), \
         patch("kento.diagnose.is_pve", return_value=False):
        with pytest.raises(InstanceNotFoundError):
            run_diagnostics(name="nope")


# --- leaked mount (per instance) ----------------------------------------


def test_leaked_mount_on_stopped_instance(tmp_path):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    d = _mk_instance(lxc, "box", name="box")
    rootfs = d / "rootfs"
    rootfs.mkdir()

    def _ismount(p):
        return str(p).endswith("rootfs")

    with _patch_bases(lxc, vm), \
         patch("kento.diagnose._apparmor_active", return_value=False), \
         patch("kento.diagnose._apparmor_parser_present", return_value=True), \
         patch("kento.diagnose._holds", return_value=[]), \
         patch("kento.diagnose._guest_names", return_value={"box"}), \
         patch("kento.diagnose.is_pve", return_value=False), \
         patch("kento.diagnose.is_running", return_value=False), \
         patch("kento.diagnose.os.path.ismount", side_effect=_ismount):
        report = run_diagnostics()

    mnt = [f for f in report["checks"] if f["category"] == "mount"]
    warns = [f for f in mnt if f["severity"] == "warn"]
    assert warns, "expected a leaked-mount warn"
    assert warns[0]["scope"] == "box"


def test_running_instance_mount_is_ok(tmp_path):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    d = _mk_instance(lxc, "box", name="box")
    rootfs = d / "rootfs"
    rootfs.mkdir()

    with _patch_bases(lxc, vm), \
         patch("kento.diagnose._apparmor_active", return_value=False), \
         patch("kento.diagnose._apparmor_parser_present", return_value=True), \
         patch("kento.diagnose._holds", return_value=[]), \
         patch("kento.diagnose._guest_names", return_value={"box"}), \
         patch("kento.diagnose.is_pve", return_value=False), \
         patch("kento.diagnose.is_running", return_value=True), \
         patch("kento.diagnose.os.path.ismount", return_value=True):
        report = run_diagnostics()

    mnt = [f for f in report["checks"] if f["category"] == "mount"]
    # A running instance with a mounted rootfs is expected, not a leak.
    assert all(f["severity"] != "warn" for f in mnt)


# --- vmid allocation health (host, pve only) ----------------------------


def test_vmid_health_reports_next_free(tmp_path):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    _mk_instance(lxc, "101", mode="pve", name="live")

    with _patch_bases(lxc, vm), \
         patch("kento.diagnose._apparmor_active", return_value=False), \
         patch("kento.diagnose._apparmor_parser_present", return_value=True), \
         patch("kento.diagnose._holds", return_value=[]), \
         patch("kento.diagnose._guest_names", return_value={"live"}), \
         patch("kento.diagnose.is_pve", return_value=True), \
         patch("kento.diagnose.is_running", return_value=False), \
         patch("kento.diagnose.next_vmid", return_value=103), \
         patch("kento.diagnose._kento_recorded_vmids", return_value={101}), \
         patch("kento.diagnose.os.path.ismount", return_value=False), \
         patch("kento.diagnose.pve_config_exists", return_value=True):
        report = run_diagnostics()

    vh = [f for f in report["checks"] if f["category"] == "vmid"]
    assert vh, "expected a vmid health finding"
    assert "103" in vh[0]["message"]


def test_vmid_health_warns_on_reserved_orphans(tmp_path):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    _mk_instance(lxc, "101", mode="pve", name="ghost")

    with _patch_bases(lxc, vm), \
         patch("kento.diagnose._apparmor_active", return_value=False), \
         patch("kento.diagnose._apparmor_parser_present", return_value=True), \
         patch("kento.diagnose._holds", return_value=[]), \
         patch("kento.diagnose._guest_names", return_value={"ghost"}), \
         patch("kento.diagnose.is_pve", return_value=True), \
         patch("kento.diagnose.is_running", return_value=False), \
         patch("kento.diagnose.next_vmid", return_value=100), \
         patch("kento.diagnose._kento_recorded_vmids", return_value={101}), \
         patch("kento.diagnose.os.path.ismount", return_value=False), \
         patch("kento.diagnose.pve_config_exists", return_value=False):
        report = run_diagnostics()

    vh = [f for f in report["checks"] if f["category"] == "vmid"]
    assert vh and vh[0]["severity"] == "warn"
    # The remediation offers both the heal (adopt) and the discard (destroy -f).
    vh_remediation = vh[0]["remediation"] or ""
    assert "adopt" in vh_remediation
    assert "destroy -f" in vh_remediation


def test_no_vmid_check_on_non_pve(tmp_path):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    _mk_instance(lxc, "box", name="box")

    with _patch_bases(lxc, vm), \
         patch("kento.diagnose._apparmor_active", return_value=False), \
         patch("kento.diagnose._apparmor_parser_present", return_value=True), \
         patch("kento.diagnose._holds", return_value=[]), \
         patch("kento.diagnose._guest_names", return_value={"box"}), \
         patch("kento.diagnose.is_pve", return_value=False), \
         patch("kento.diagnose.is_running", return_value=False), \
         patch("kento.diagnose.os.path.ismount", return_value=False):
        report = run_diagnostics()

    assert not [f for f in report["checks"] if f["category"] == "vmid"]


# --- networkd drop-ins (per instance) -----------------------------------


def test_missing_static_dropin_warns(tmp_path):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    d = _mk_instance(lxc, "box", name="box")
    # Static instance: kento-net carries an ip= line (real create.py format),
    # but the 05-kento-static.network drop-in is absent from the overlay upper.
    (d / "kento-net").write_text("ip=10.0.0.5/24\ngateway=10.0.0.1\n")

    with _patch_bases(lxc, vm), \
         patch("kento.diagnose._apparmor_active", return_value=False), \
         patch("kento.diagnose._apparmor_parser_present", return_value=True), \
         patch("kento.diagnose._holds", return_value=[]), \
         patch("kento.diagnose._guest_names", return_value={"box"}), \
         patch("kento.diagnose.is_pve", return_value=False), \
         patch("kento.diagnose.is_running", return_value=False), \
         patch("kento.diagnose.os.path.ismount", return_value=False):
        report = run_diagnostics()

    net = [f for f in report["checks"] if f["category"] == "network"]
    assert any(f["severity"] == "warn"
               and "05-kento-static.network" in f["message"]
               for f in net), net


def test_present_static_dropin_ok(tmp_path):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    d = _mk_instance(lxc, "box", name="box")
    (d / "kento-net").write_text("ip=10.0.0.5/24\ngateway=10.0.0.1\n")
    # Drop-in present in the overlay upper (kento-state == d, set by helper).
    net_dir = d / "upper" / "etc" / "systemd" / "network"
    net_dir.mkdir(parents=True)
    (net_dir / "05-kento-static.network").write_text("[Match]\nName=eth0\n")

    with _patch_bases(lxc, vm), \
         patch("kento.diagnose._apparmor_active", return_value=False), \
         patch("kento.diagnose._apparmor_parser_present", return_value=True), \
         patch("kento.diagnose._holds", return_value=[]), \
         patch("kento.diagnose._guest_names", return_value={"box"}), \
         patch("kento.diagnose.is_pve", return_value=False), \
         patch("kento.diagnose.is_running", return_value=False), \
         patch("kento.diagnose.os.path.ismount", return_value=False):
        report = run_diagnostics()

    net = [f for f in report["checks"] if f["category"] == "network"]
    assert any(f["severity"] == "ok"
               and "static network drop-in present" in f["message"]
               for f in net), net
    assert not any(f["severity"] == "warn" for f in net), net


# --- cloud-init footgun (per instance) ----------------------------------


def test_cloudinit_root_ssh_advisory(tmp_path):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    d = _mk_instance(lxc, "box", name="box")
    (d / "kento-authorized-keys").write_text("ssh-ed25519 AAAA root\n")
    (d / "kento-layers").write_text("/layer1:/layer2\n")
    # ssh-user defaults to root (no kento-ssh-user file).

    with _patch_bases(lxc, vm), \
         patch("kento.diagnose._apparmor_active", return_value=False), \
         patch("kento.diagnose._apparmor_parser_present", return_value=True), \
         patch("kento.diagnose._holds", return_value=[]), \
         patch("kento.diagnose._guest_names", return_value={"box"}), \
         patch("kento.diagnose.is_pve", return_value=False), \
         patch("kento.diagnose.is_running", return_value=False), \
         patch("kento.diagnose.detect_cloudinit", return_value=True), \
         patch("kento.diagnose.os.path.ismount", return_value=False):
        report = run_diagnostics()

    ci = [f for f in report["checks"] if f["category"] == "cloudinit"]
    assert ci and ci[0]["severity"] in ("info", "warn")
    assert ci[0]["scope"] == "box"


def test_no_cloudinit_advisory_without_root_key(tmp_path):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    d = _mk_instance(lxc, "box", name="box")
    (d / "kento-layers").write_text("/layer1\n")
    # no authorized-keys -> no advisory even if image is cloud-init.

    with _patch_bases(lxc, vm), \
         patch("kento.diagnose._apparmor_active", return_value=False), \
         patch("kento.diagnose._apparmor_parser_present", return_value=True), \
         patch("kento.diagnose._holds", return_value=[]), \
         patch("kento.diagnose._guest_names", return_value={"box"}), \
         patch("kento.diagnose.is_pve", return_value=False), \
         patch("kento.diagnose.is_running", return_value=False), \
         patch("kento.diagnose.detect_cloudinit", return_value=True), \
         patch("kento.diagnose.os.path.ismount", return_value=False):
        report = run_diagnostics()

    ci = [f for f in report["checks"] if f["category"] == "cloudinit"]
    assert not [f for f in ci if f["severity"] in ("info", "warn")]


# --- graceful no-root degradation ---------------------------------------


def test_graceful_degradation_on_permission_error(tmp_path):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    _mk_instance(lxc, "box", name="box")

    def _boom(*a, **k):
        raise PermissionError("not root")

    with _patch_bases(lxc, vm), \
         patch("kento.diagnose._apparmor_active", side_effect=_boom), \
         patch("kento.diagnose._apparmor_parser_present", return_value=True), \
         patch("kento.diagnose._holds", side_effect=_boom), \
         patch("kento.diagnose._guest_names", return_value={"box"}), \
         patch("kento.diagnose.is_pve", return_value=False), \
         patch("kento.diagnose.is_running", return_value=False), \
         patch("kento.diagnose.os.path.ismount", side_effect=_boom):
        # Must not raise.
        report = run_diagnostics()

    # Degraded checks emit info findings rather than crashing.
    infos = [f for f in report["checks"] if f["severity"] == "info"]
    assert any("root" in f["message"].lower() or "could not" in
               f["message"].lower() for f in infos)


# --- format_diagnostics --------------------------------------------------


def test_format_renders_clean(tmp_path):
    report = {"checks": [
        {"category": "apparmor", "severity": "ok", "scope": "host",
         "message": "apparmor OK", "remediation": None},
    ], "problem_count": 0, "instances_scanned": 0}
    out = format_diagnostics(report)
    assert isinstance(out, str)
    assert out  # non-empty
    # No trailing print side effects; clean summary present.
    assert "0 problem" in out or "All clear" in out


def test_format_includes_remediation_for_warn():
    report = {"checks": [
        {"category": "orphan", "severity": "warn", "scope": "ghost",
         "message": "instance ghost is orphaned",
         "remediation": "kento destroy -f ghost"},
        {"category": "apparmor", "severity": "ok", "scope": "host",
         "message": "apparmor OK", "remediation": None},
    ], "problem_count": 1, "instances_scanned": 1}
    out = format_diagnostics(report)
    assert "ghost" in out
    assert "kento destroy -f ghost" in out
    assert "1 problem" in out


def test_format_groups_host_then_instances():
    report = {"checks": [
        {"category": "portfwd", "severity": "warn", "scope": "box",
         "message": "portfwd error", "remediation": "check it"},
        {"category": "apparmor", "severity": "error", "scope": "host",
         "message": "apparmor broken", "remediation": "install parser"},
    ], "problem_count": 2, "instances_scanned": 1}
    out = format_diagnostics(report)
    # host finding should appear before the box finding.
    assert out.index("apparmor broken") < out.index("portfwd error")
