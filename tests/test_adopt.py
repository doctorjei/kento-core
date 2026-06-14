"""Tests for kento.reconcile.adopt / emit_pve_config — heal one orphan.

Style mirrors test_reconcile.py / test_destroy.py / test_pve.py: realistic
``kento-*`` metadata under tmp_path state dirs, the PVE writers + config probe
mocked. adopt resolves through kento.reconcile.{resolve_any,pve_config_exists}
and gates on kento.reconcile.require_root, so these patch those names.

adopt must produce a ``.conf`` byte-compatible with create's, so the happy
paths assert the create-time generators/writers/hook-regen were invoked with
the recovered metadata; the refusal paths assert the right KentoError subclass.
"""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from kento.errors import ModeError, StateError
from kento.reconcile import adopt, emit_pve_config

from contextlib import contextmanager


# --- fixtures ------------------------------------------------------------


def _mk_pve_lxc(base, vmid, *, name="ghost", net_type="bridge",
                bridge="vmbr0", **extra):
    """Create a realistic pve-lxc orphan state dir (dir name == vmid)."""
    d = base / str(vmid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "kento-image").write_text("img:latest\n")
    (d / "kento-mode").write_text("pve\n")
    (d / "kento-name").write_text(name + "\n")
    (d / "kento-layers").write_text("/l1:/l2\n")
    (d / "kento-state").write_text(str(d) + "\n")
    if net_type is not None:
        (d / "kento-net-type").write_text(net_type + "\n")
    if bridge is not None:
        (d / "kento-bridge").write_text(bridge + "\n")
    (d / "kento-nesting").write_text("0\n")
    for fname, val in extra.items():
        (d / f"kento-{fname}").write_text(str(val) + "\n")
    return d


def _mk_pve_vm(base, dir_name, vmid, *, name="myvm", net_type="usermode",
               **extra):
    d = base / dir_name
    d.mkdir(parents=True, exist_ok=True)
    (d / "kento-image").write_text("img:latest\n")
    (d / "kento-mode").write_text("pve-vm\n")
    (d / "kento-name").write_text(name + "\n")
    (d / "kento-layers").write_text("/l1:/l2\n")
    (d / "kento-state").write_text(str(d) + "\n")
    (d / "kento-vmid").write_text(str(vmid) + "\n")
    if net_type is not None:
        (d / "kento-net-type").write_text(net_type + "\n")
    (d / "kento-memory").write_text("512\n")
    (d / "kento-cores").write_text("1\n")
    for fname, val in extra.items():
        (d / f"kento-{fname}").write_text(str(val) + "\n")
    return d


# --- happy path: pve-lxc -------------------------------------------------


def test_adopt_pve_lxc_happy_path(tmp_path):
    d = _mk_pve_lxc(tmp_path, 101, name="ghost", memory=512)

    with patch("kento.reconcile.require_root"), \
         patch("kento.reconcile.resolve_any", return_value=(d, "pve")), \
         patch("kento.reconcile.pve_config_exists", return_value=False), \
         patch("kento.hook.write_hook") as m_hook, \
         patch("kento.pve.generate_pve_config",
               return_value="arch: amd64\n") as m_gen, \
         patch("kento.pve.write_pve_config") as m_write, \
         patch("kento.lxc_hook.write_lxc_snippets_wrapper",
               return_value="local:snippets/kento-lxc-101.sh") as m_wrap:
        result = adopt("ghost")

    assert result == {"name": "ghost", "vmid": 101, "mode": "pve"}
    # Hook regenerated from surviving state.
    m_hook.assert_called_once()
    assert m_hook.call_args.args[0] == d  # container_dir
    # Wrapper regenerated (kento-memory present) -> ref threaded into generator.
    m_wrap.assert_called_once()
    assert m_wrap.call_args.args[0] == 101
    # Config generated with the recovered vmid + net metadata, then written.
    m_gen.assert_called_once()
    assert m_gen.call_args.args[0] == "ghost"
    assert m_gen.call_args.args[1] == 101
    assert m_gen.call_args.kwargs["net_type"] == "bridge"
    assert m_gen.call_args.kwargs["bridge"] == "vmbr0"
    assert m_gen.call_args.kwargs["hookscript_ref"] == \
        "local:snippets/kento-lxc-101.sh"
    m_write.assert_called_once_with(101, "arch: amd64\n")


def test_adopt_pve_lxc_no_wrapper_when_no_port_memory_cores(tmp_path):
    """Wrapper is regenerated ONLY when port/memory/cores exists (reset.py
    condition). A plain pve-lxc orphan gets hookscript_ref=None."""
    d = _mk_pve_lxc(tmp_path, 102, name="plain")  # no port/memory/cores

    with patch("kento.reconcile.require_root"), \
         patch("kento.reconcile.resolve_any", return_value=(d, "pve")), \
         patch("kento.reconcile.pve_config_exists", return_value=False), \
         patch("kento.hook.write_hook"), \
         patch("kento.pve.generate_pve_config", return_value="x\n") as m_gen, \
         patch("kento.pve.write_pve_config"), \
         patch("kento.lxc_hook.write_lxc_snippets_wrapper") as m_wrap:
        adopt("plain")

    m_wrap.assert_not_called()
    assert m_gen.call_args.kwargs["hookscript_ref"] is None


def test_adopt_pve_lxc_unprivileged_refreshes_idmap_range(tmp_path):
    d = _mk_pve_lxc(tmp_path, 103, name="unpriv", memory=512, unprivileged=1)

    conf_text = "arch: amd64\nunprivileged: 1\n"
    with patch("kento.reconcile.require_root"), \
         patch("kento.reconcile.resolve_any", return_value=(d, "pve")), \
         patch("kento.reconcile.pve_config_exists", return_value=False), \
         patch("kento.hook.write_hook"), \
         patch("kento.pve.generate_pve_config", return_value=conf_text), \
         patch("kento.pve.write_pve_config"), \
         patch("kento.lxc_hook.write_lxc_snippets_wrapper",
               return_value="ref"):
        adopt("unpriv")

    rng = (d / "kento-idmap-range").read_text().strip()
    assert rng == "100000 65536"  # PVE unprivileged default


# --- happy path: pve-vm --------------------------------------------------


def test_adopt_pve_vm_happy_path(tmp_path):
    d = _mk_pve_vm(tmp_path, "myvm", 200, name="myvm")

    defaults = {"memory": 512, "cores": 1, "machine": "q35", "kvm": True}
    with patch("kento.reconcile.require_root"), \
         patch("kento.reconcile.resolve_any", return_value=(d, "pve-vm")), \
         patch("kento.reconcile.pve_config_exists", return_value=False), \
         patch("kento.vm_hook.write_vm_hook") as m_hook, \
         patch("kento.vm_hook.write_snippets_wrapper",
               return_value="local:snippets/kento-vm-200.sh") as m_wrap, \
         patch("kento.defaults.get_vm_defaults",
               return_value=defaults) as m_def, \
         patch("kento.pve.generate_qm_config",
               return_value="name: myvm\n") as m_gen, \
         patch("kento.pve.write_qm_config") as m_write:
        result = adopt("myvm")

    assert result == {"name": "myvm", "vmid": 200, "mode": "pve-vm"}
    m_hook.assert_called_once()
    m_wrap.assert_called_once()
    assert m_wrap.call_args.args[0] == 200
    # machine/kvm came from get_vm_defaults (not persisted).
    m_def.assert_called_once()
    m_gen.assert_called_once()
    assert m_gen.call_args.kwargs["machine"] == "q35"
    assert m_gen.call_args.kwargs["kvm"] is True
    assert m_gen.call_args.kwargs["hookscript_ref"] == \
        "local:snippets/kento-vm-200.sh"
    m_write.assert_called_once_with(200, "name: myvm\n")


# --- refusals ------------------------------------------------------------


def test_adopt_refuses_non_orphan(tmp_path):
    """Config present => not an orphan => StateError."""
    d = _mk_pve_lxc(tmp_path, 101)

    with patch("kento.reconcile.require_root"), \
         patch("kento.reconcile.resolve_any", return_value=(d, "pve")), \
         patch("kento.reconcile.pve_config_exists", return_value=True):
        with pytest.raises(StateError, match="not an orphan"):
            adopt("ghost")


def test_adopt_refuses_vmid_occupied(tmp_path):
    """This instance's config is gone but the vmid is occupied in the OTHER
    namespace => collision => StateError."""
    d = _mk_pve_lxc(tmp_path, 101)

    def fake_exists(vmid, mode):
        # pve (this instance's kind): gone. pve-vm: a foreign occupant.
        return mode == "pve-vm"

    with patch("kento.reconcile.require_root"), \
         patch("kento.reconcile.resolve_any", return_value=(d, "pve")), \
         patch("kento.reconcile.pve_config_exists", side_effect=fake_exists):
        with pytest.raises(StateError, match="already occupied"):
            adopt("ghost")


def test_adopt_refuses_plain_lxc(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()

    with patch("kento.reconcile.require_root"), \
         patch("kento.reconcile.resolve_any", return_value=(d, "lxc")):
        with pytest.raises(ModeError, match="only applies to PVE"):
            adopt("plain")


def test_adopt_refuses_plain_vm(tmp_path):
    d = tmp_path / "plainvm"
    d.mkdir()

    with patch("kento.reconcile.require_root"), \
         patch("kento.reconcile.resolve_any", return_value=(d, "vm")):
        with pytest.raises(ModeError, match="only applies to PVE"):
            adopt("plainvm")


def test_adopt_fails_closed_when_net_type_missing(tmp_path):
    """A pre-1.6.0 orphan (no kento-net-type) is not faithfully recoverable;
    adopt fails closed rather than emit a divergent config."""
    d = _mk_pve_lxc(tmp_path, 101, name="old", net_type=None)

    with patch("kento.reconcile.require_root"), \
         patch("kento.reconcile.resolve_any", return_value=(d, "pve")), \
         patch("kento.reconcile.pve_config_exists", return_value=False), \
         patch("kento.pve.generate_pve_config") as m_gen:
        with pytest.raises(StateError, match="not recoverable"):
            adopt("old")

    # Fail-closed: no config was emitted.
    m_gen.assert_not_called()


def test_adopt_uses_passed_container_dir_and_mode(tmp_path):
    """When container_dir/mode are passed, the resolver is not consulted."""
    d = _mk_pve_lxc(tmp_path, 105, name="direct", memory=512)

    with patch("kento.reconcile.require_root"), \
         patch("kento.reconcile.resolve_any") as m_resolve, \
         patch("kento.reconcile.pve_config_exists", return_value=False), \
         patch("kento.hook.write_hook"), \
         patch("kento.pve.generate_pve_config", return_value="x\n"), \
         patch("kento.pve.write_pve_config"), \
         patch("kento.lxc_hook.write_lxc_snippets_wrapper", return_value="r"):
        result = adopt("direct", container_dir=d, mode="pve")

    m_resolve.assert_not_called()
    assert result["vmid"] == 105


# --- indeterminate probe: refuse cleanly as StateError -------------------


def test_adopt_orphan_check_indeterminate_raises_stateerror(tmp_path):
    """If the orphan-check probe raises PermissionError (e.g. /etc/pve
    unreadable), adopt must refuse with StateError — NOT a raw OSError —
    and emit no config."""
    d = _mk_pve_lxc(tmp_path, 101, name="ghost")

    with patch("kento.reconcile.require_root"), \
         patch("kento.reconcile.resolve_any", return_value=(d, "pve")), \
         patch("kento.reconcile.pve_config_exists",
               side_effect=PermissionError(13, "Permission denied")), \
         patch("kento.reconcile.emit_pve_config") as m_emit:
        with pytest.raises(StateError, match="is an orphan"):
            adopt("ghost")

    # Indeterminate => never act: no config writer was invoked.
    m_emit.assert_not_called()


def test_adopt_vmid_occupancy_indeterminate_raises_stateerror(tmp_path):
    """If the vmid-occupied scan's probe raises OSError, adopt must refuse
    with StateError — NOT a raw OSError — and emit no config. The first
    probe (orphan-check) returns a definitive False; only the occupancy
    scan raises."""
    d = _mk_pve_lxc(tmp_path, 101, name="ghost")

    calls = {"n": 0}

    def flaky_exists(vmid, mode):
        calls["n"] += 1
        if calls["n"] == 1:
            return False  # orphan-check: definitively gone
        raise OSError(13, "Permission denied")  # occupancy scan: indeterminate

    with patch("kento.reconcile.require_root"), \
         patch("kento.reconcile.resolve_any", return_value=(d, "pve")), \
         patch("kento.reconcile.pve_config_exists", side_effect=flaky_exists), \
         patch("kento.reconcile.emit_pve_config") as m_emit:
        with pytest.raises(StateError, match="occupancy"):
            adopt("ghost")

    m_emit.assert_not_called()


# --- locking: the check->write critical section is held under kento_lock --


def test_adopt_holds_kento_lock_around_critical_section(tmp_path):
    """adopt must enter kento_lock() (the same lock create holds) and must
    still be inside it when emit_pve_config runs — so a concurrent create
    can't write a fresh config at this vmid between the orphan-check and the
    write (TOCTOU)."""
    d = _mk_pve_lxc(tmp_path, 101, name="ghost")

    events = []

    @contextmanager
    def recording_lock():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    def record_emit(container_dir, mode):
        events.append("emit")
        return 101

    with patch("kento.reconcile.require_root"), \
         patch("kento.reconcile.resolve_any", return_value=(d, "pve")), \
         patch("kento.reconcile.pve_config_exists", return_value=False), \
         patch("kento.reconcile.kento_lock", side_effect=recording_lock), \
         patch("kento.reconcile.emit_pve_config", side_effect=record_emit):
        adopt("ghost")

    # Lock entered, emit ran while inside it, then lock released.
    assert events == ["enter", "emit", "exit"]


# --- emit_pve_config direct ----------------------------------------------


def test_emit_pve_config_pve_lxc_returns_vmid(tmp_path):
    d = _mk_pve_lxc(tmp_path, 111, memory=512)

    with patch("kento.hook.write_hook"), \
         patch("kento.pve.generate_pve_config", return_value="x\n"), \
         patch("kento.pve.write_pve_config") as m_write, \
         patch("kento.lxc_hook.write_lxc_snippets_wrapper", return_value="r"):
        vmid = emit_pve_config(d, "pve")

    assert vmid == 111
    m_write.assert_called_once()
