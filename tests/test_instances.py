"""Tests for the typed Instance family (Block 08 — kento._instances).

READ-ONLY snapshot path: snapshot load per mode, get (M1), list (M2, polymorphic
+ total), refresh (M10). Mocked filesystem (tmp_path container dirs) + mocked
is_running / pve_config_exists (the house pattern — see test_info.py /
test_list.py). No live process is run.
"""

import os
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from kento import (
    Instance,
    SystemContainer,
    VirtualMachine,
    InstanceNotFoundError,
    NetworkConnection,
    NetworkMode,
    OciReference,
    PlatformMode,
    PlatformProfile,
    Status,
    StorageMode,
    ValidationError,
    ForwardProtocol,
    ModeError,
)
from kento import _instances


# --------------------------------------------------------------------------- #
# Helpers — build fake container directories with kento-* metadata.
# --------------------------------------------------------------------------- #


def _make_lxc(base: Path, name: str = "mybox", **meta) -> Path:
    """A minimal plain-LXC container dir. `meta` overrides/adds kento-* files."""
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    files = {
        "kento-name": name,
        "kento-image": "droste-hair:latest",
        "kento-mode": "lxc",
    }
    files.update(meta)
    for fname, content in files.items():
        (d / fname).write_text(content if content.endswith("\n") else content + "\n")
    return d


def _make_vm(base: Path, name: str = "myvm", **meta) -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    files = {
        "kento-name": name,
        "kento-image": "droste-hair:latest",
        "kento-mode": "vm",
    }
    files.update(meta)
    for fname, content in files.items():
        (d / fname).write_text(content if content.endswith("\n") else content + "\n")
    return d


# --------------------------------------------------------------------------- #
# Base is abstract / subclasses instantiate from a snapshot.
# --------------------------------------------------------------------------- #


def test_base_instance_is_uninstantiable():
    with pytest.raises(TypeError):
        Instance()


def test_base_create_transient_abstract():
    # Instance cannot be created. Python lets you CALL an abstract classmethod
    # directly on the abstract class (abstractmethod only blocks instantiation),
    # so the base bodies RAISE NotImplementedError rather than silently return
    # None. Concrete kinds also raise (Phase 4 stubs) but ARE instantiable from a
    # snapshot.
    with pytest.raises(NotImplementedError):
        Instance.create()
    with pytest.raises(NotImplementedError):
        Instance.transient()
    with pytest.raises(NotImplementedError):
        SystemContainer.create("x", "img")
    with pytest.raises(NotImplementedError):
        VirtualMachine.create("x", "img")
    with pytest.raises(NotImplementedError):
        SystemContainer.transient("x", "img")
    with pytest.raises(NotImplementedError):
        VirtualMachine.transient("x", "img")


def test_subclasses_instantiate_from_snapshot(tmp_path):
    d = _make_lxc(tmp_path)
    with patch("kento.is_running", return_value=False):
        inst = _instances._load_snapshot(d, "lxc")
    assert isinstance(inst, SystemContainer)
    assert isinstance(inst, Instance)


# --------------------------------------------------------------------------- #
# Snapshot load per mode — correctly-typed fields.
# --------------------------------------------------------------------------- #


def test_snapshot_lxc_minimal(tmp_path):
    d = _make_lxc(tmp_path)
    with patch("kento.is_running", return_value=True):
        inst = _instances._load_snapshot(d, "lxc")
    assert isinstance(inst, SystemContainer)
    assert inst.name == "mybox"
    # hostname fallback = name (no hostname key written; † back-fill is Phase 6)
    assert inst.hostname == "mybox"
    assert inst.sources == (OciReference.parse("droste-hair:latest"),)
    assert inst.storage is StorageMode.OVERLAY
    assert inst.status is Status.RUNNING
    assert inst.platform_profile == PlatformProfile(
        mode=PlatformMode.STANDARD, mid=None, extra_args=()
    )
    assert inst.nesting is False
    assert inst.resources == {}
    assert inst.environment == {}
    assert inst.unprivileged is False
    assert inst.lxc_args == ()
    assert isinstance(inst.created, datetime)
    assert isinstance(inst.network, NetworkConnection)


def test_snapshot_hostname_key_overrides_name(tmp_path):
    d = _make_lxc(tmp_path, hostname="custom-host")
    with patch("kento.is_running", return_value=False):
        inst = _instances._load_snapshot(d, "lxc")
    assert inst.name == "mybox"
    assert inst.hostname == "custom-host"


def test_snapshot_lxc_full_fields(tmp_path):
    d = _make_lxc(
        tmp_path,
        **{
            "kento-net-type": "bridge",
            "kento-bridge": "lxcbr0",
            "kento-mac": "00:11:22:33:44:55",
            "kento-net": "ip=10.0.0.5/24\ngateway=10.0.0.1\ndns=1.1.1.1",
            "kento-port": "8080:80",
            "kento-cores": "2",
            "kento-memory": "1024",
            "kento-nesting": "1",
            "kento-unprivileged": "1",
            "kento-lxc-args": "lxc.foo = bar\nlxc.baz = qux",
            "kento-env": "FOO=bar\nBAZ=qux=quux",
        },
    )
    with patch("kento.is_running", return_value=False):
        inst = _instances._load_snapshot(d, "lxc")

    # Network: bridge + static ip => STATIC; ip split into address/subnet.
    assert inst.network.mode is NetworkMode.STATIC
    assert inst.network.link_config == {
        "bridge": "lxcbr0", "mac": "00:11:22:33:44:55",
    }
    assert inst.network.ip_config == {
        "address": "10.0.0.5", "subnet": "24",
        "gateway": "10.0.0.1", "dns1": "1.1.1.1",
    }
    # forwards: single tcp 8080->80 (host_addr/guest_addr None).
    assert inst.forwards == {(ForwardProtocol.TCP, None, 8080): (None, 80)}
    assert inst.resources == {"cores": 2, "memory": 1024}
    assert inst.nesting is True
    assert inst.unprivileged is True
    assert inst.lxc_args == ("lxc.foo = bar", "lxc.baz = qux")
    # environment: value may itself contain '=' (split on first only).
    assert inst.environment == {"FOO": "bar", "BAZ": "qux=quux"}


def test_snapshot_bridge_without_static_is_dhcp(tmp_path):
    d = _make_lxc(tmp_path, **{"kento-net-type": "bridge", "kento-bridge": "lxcbr0"})
    with patch("kento.is_running", return_value=False):
        inst = _instances._load_snapshot(d, "lxc")
    assert inst.network.mode is NetworkMode.DHCP
    assert inst.network.ip_config == {}


def test_snapshot_net_type_mappings(tmp_path):
    cases = {
        "host": NetworkMode.HOST,
        "usermode": NetworkMode.USER,
        "none": NetworkMode.DISABLED,
    }
    for i, (net_type, expected) in enumerate(cases.items()):
        d = _make_lxc(tmp_path, name=f"box{i}", **{"kento-net-type": net_type})
        with patch("kento.is_running", return_value=False):
            inst = _instances._load_snapshot(d, "lxc")
        assert inst.network.mode is expected, net_type


def test_snapshot_unrecognized_net_type_is_disabled(tmp_path, caplog):
    d = _make_lxc(tmp_path, **{"kento-net-type": "weird"})
    with patch("kento.is_running", return_value=False):
        inst = _instances._load_snapshot(d, "lxc")
    assert inst.network.mode is NetworkMode.DISABLED


def test_snapshot_vm(tmp_path):
    d = _make_vm(tmp_path, **{"kento-qemu-args": "-cpu host\n-smp 2"})
    with patch("kento.is_running", return_value=True):
        inst = _instances._load_snapshot(d, "vm")
    assert isinstance(inst, VirtualMachine)
    assert inst.qemu_args == ("-cpu host", "-smp 2")
    assert inst.status is Status.RUNNING
    assert inst.platform_profile.mode is PlatformMode.STANDARD


def test_snapshot_pve_lxc(tmp_path):
    # pve-lxc: stored mode "pve"; dir name IS the vmid; pve-args -> extra_args.
    d = _make_lxc(tmp_path, name="100", **{
        "kento-mode": "pve", "kento-pve-args": "--onboot 1",
    })
    with patch("kento.is_running", return_value=False), \
            patch("kento.pve_config_exists", return_value=True):
        inst = _instances._load_snapshot(d, "pve")
    assert isinstance(inst, SystemContainer)
    assert inst.status is Status.STOPPED
    assert inst.platform_profile == PlatformProfile(
        mode=PlatformMode.PVE, mid=100, extra_args=("--onboot 1",)
    )


def test_snapshot_pve_vm(tmp_path):
    d = _make_vm(tmp_path, name="myvm", **{
        "kento-mode": "pve-vm", "kento-vmid": "150",
    })
    with patch("kento.is_running", return_value=True), \
            patch("kento.pve_config_exists", return_value=True):
        inst = _instances._load_snapshot(d, "pve-vm")
    assert isinstance(inst, VirtualMachine)
    assert inst.platform_profile.mode is PlatformMode.PVE
    assert inst.platform_profile.mid == 150


def test_snapshot_storage_explicit_and_unknown(tmp_path):
    d = _make_lxc(tmp_path, name="eph", **{"kento-storage": "ephemeral-image"})
    with patch("kento.is_running", return_value=False):
        inst = _instances._load_snapshot(d, "lxc")
    assert inst.storage is StorageMode.EPHEMERAL_IMAGE

    d2 = _make_lxc(tmp_path, name="weird", **{"kento-storage": "bogus"})
    with patch("kento.is_running", return_value=False):
        inst2 = _instances._load_snapshot(d2, "lxc")
    assert inst2.storage is StorageMode.OVERLAY  # total fallback


# --------------------------------------------------------------------------- #
# Status resolver — total, ORPHAN / UNKNOWN domain states.
# --------------------------------------------------------------------------- #


def test_status_orphan_pve_config_gone(tmp_path):
    d = _make_lxc(tmp_path, name="100", **{"kento-mode": "pve"})
    with patch("kento.pve_config_exists", return_value=False):
        inst = _instances._load_snapshot(d, "pve")
    assert inst.status is Status.ORPHAN


def test_status_unknown_on_probe_error(tmp_path):
    d = _make_lxc(tmp_path)
    with patch("kento.is_running", side_effect=OSError("boom")):
        inst = _instances._load_snapshot(d, "lxc")
    assert inst.status is Status.UNKNOWN


def test_status_unknown_on_indeterminate_pve_config(tmp_path):
    d = _make_lxc(tmp_path, name="100", **{"kento-mode": "pve"})
    with patch("kento.pve_config_exists", side_effect=PermissionError):
        inst = _instances._load_snapshot(d, "pve")
    assert inst.status is Status.UNKNOWN


def test_status_pve_vm_no_vmid_is_orphan(tmp_path):
    d = _make_vm(tmp_path, name="myvm", **{"kento-mode": "pve-vm"})
    # No kento-vmid recorded => orphan (mirrors reconcile._is_orphan).
    with patch("kento.is_running", return_value=False):
        inst = _instances._resolve_status(d, "pve-vm")
    assert inst is Status.ORPHAN


# --------------------------------------------------------------------------- #
# M1 get — resolve right kind, raise on absent + kind-mismatch.
# --------------------------------------------------------------------------- #


def test_get_resolves_lxc(tmp_path):
    d = _make_lxc(tmp_path)
    with patch("kento.resolve_any", return_value=(d, "lxc")), \
            patch("kento.is_running", return_value=False):
        inst = Instance.get("mybox")
    assert isinstance(inst, SystemContainer)
    assert inst.name == "mybox"


def test_get_subclass_narrows_to_right_kind(tmp_path):
    d = _make_vm(tmp_path)
    with patch("kento.resolve_any", return_value=(d, "vm")), \
            patch("kento.is_running", return_value=False):
        inst = VirtualMachine.get("myvm")
    assert isinstance(inst, VirtualMachine)


def test_get_kind_mismatch_raises(tmp_path):
    # Resolving an LXC name via VirtualMachine.get raises a kind-mismatch.
    d = _make_lxc(tmp_path)
    with patch("kento.resolve_any", return_value=(d, "lxc")), \
            patch("kento.is_running", return_value=False):
        with pytest.raises(InstanceNotFoundError) as exc:
            VirtualMachine.get("mybox")
    assert "SystemContainer" in str(exc.value)


def test_get_absent_raises(tmp_path):
    with patch("kento.resolve_any",
               side_effect=InstanceNotFoundError("no instance named 'nope'")):
        with pytest.raises(InstanceNotFoundError):
            Instance.get("nope")


# --------------------------------------------------------------------------- #
# M2 list — both namespaces, polymorphic narrowing, total over corruption.
# --------------------------------------------------------------------------- #


def test_list_both_namespaces_and_narrowing(tmp_path):
    lxc_base = tmp_path / "lxc"
    vm_base = tmp_path / "vm"
    lxc_base.mkdir()
    vm_base.mkdir()
    _make_lxc(lxc_base, name="ctr1")
    _make_vm(vm_base, name="vm1")

    # list() looks up LXC_BASE/VM_BASE lazily from the kento package, so patch
    # them there (the same point list.py's tests patch).
    with patch("kento.LXC_BASE", lxc_base), \
            patch("kento.VM_BASE", vm_base), \
            patch("kento.is_running", return_value=False):
        all_insts = Instance.list()
        ctrs = SystemContainer.list()
        vms = VirtualMachine.list()

    names = {i.name for i in all_insts}
    assert names == {"ctr1", "vm1"}
    assert [i.name for i in ctrs] == ["ctr1"]
    assert all(isinstance(i, SystemContainer) for i in ctrs)
    assert [i.name for i in vms] == ["vm1"]
    assert all(isinstance(i, VirtualMachine) for i in vms)


def test_list_total_over_corrupt_entry(tmp_path, caplog):
    lxc_base = tmp_path / "lxc"
    vm_base = tmp_path / "vm"
    lxc_base.mkdir()
    vm_base.mkdir()
    _make_lxc(lxc_base, name="good")
    # A corrupt PVE entry: mode pve, dir name not a vmid, no config -> but make
    # it raise inside the loader via a coherence violation. Easiest corruption:
    # a pve instance whose vmid can't parse and config probe says gone is ORPHAN
    # (not an error). To force a raise, use a pve-lxc dir name below the floor.
    bad = _make_lxc(lxc_base, name="50", **{"kento-mode": "pve"})

    with patch("kento.LXC_BASE", lxc_base), \
            patch("kento.VM_BASE", vm_base), \
            patch("kento.is_running", return_value=False), \
            patch("kento.pve_config_exists", return_value=True):
        insts = Instance.list()

    # The good one survives; the bad one (vmid 50 < PVE floor 100 ->
    # ValidationError from PlatformProfile) is skipped, not fatal.
    assert [i.name for i in insts] == ["good"]


def test_list_skips_missing_bases(tmp_path):
    # Neither base exists -> empty list, no crash.
    with patch("kento.LXC_BASE", tmp_path / "nope-lxc"), \
            patch("kento.VM_BASE", tmp_path / "nope-vm"):
        assert Instance.list() == []


# --------------------------------------------------------------------------- #
# M10 refresh — re-read snapshot in place.
# --------------------------------------------------------------------------- #


def test_refresh_rereads_status(tmp_path):
    d = _make_lxc(tmp_path)
    with patch("kento.resolve_any", return_value=(d, "lxc")), \
            patch("kento.is_running", return_value=False):
        inst = Instance.get("mybox")
    assert inst.status is Status.STOPPED

    # Out-of-band change: now running + a new env var added on disk.
    (d / "kento-env").write_text("ADDED=1\n")
    with patch("kento.is_running", return_value=True):
        inst.refresh()
    assert inst.status is Status.RUNNING
    assert inst.environment == {"ADDED": "1"}


def test_refresh_same_handle_identity(tmp_path):
    d = _make_lxc(tmp_path)
    with patch("kento.resolve_any", return_value=(d, "lxc")), \
            patch("kento.is_running", return_value=False):
        inst = Instance.get("mybox")
    before = id(inst)
    with patch("kento.is_running", return_value=False):
        inst.refresh()
    assert id(inst) == before


# --------------------------------------------------------------------------- #
# forwards loader — total over a malformed line.
# --------------------------------------------------------------------------- #


def test_forwards_skips_malformed_line(tmp_path):
    d = _make_lxc(tmp_path, **{"kento-port": "8080:80\nnonsense:::\n9090:90"})
    with patch("kento.is_running", return_value=False):
        inst = _instances._load_snapshot(d, "lxc")
    assert inst.forwards == {
        (ForwardProtocol.TCP, None, 8080): (None, 80),
        (ForwardProtocol.TCP, None, 9090): (None, 90),
    }


def test_resources_omits_non_integer(tmp_path):
    d = _make_lxc(tmp_path, **{"kento-cores": "notanint", "kento-memory": "512"})
    with patch("kento.is_running", return_value=False):
        inst = _instances._load_snapshot(d, "lxc")
    assert inst.resources == {"memory": 512}


# --------------------------------------------------------------------------- #
# Block 09 — lifecycle methods: start / stop / destroy / scrub (ADDITIVE).
#
# Each method WRAPS the existing mode-aware runtime func (start.start /
# stop.shutdown / destroy.destroy / reset.reset) and self-updates status. We
# patch the wrapped func to assert DELEGATION (no forked lifecycle logic) +
# the exact call shape per mode, and patch the status probe to assert the
# self-update. House pattern: snapshot built via _load_snapshot with is_running
# mocked; no live process runs.
# --------------------------------------------------------------------------- #


from kento import StopTimeout  # noqa: E402  (grouped with the Block-09 block)


def _make_for_mode(base: Path, mode: str) -> Path:
    """Build a per-mode container dir that loads into a VALID typed snapshot.

    PVE modes need a coherent vmid (the PlatformProfile coherence check, §6.2):
    pve-lxc's vmid is the dir NAME (>= the PVE floor), pve-vm reads kento-vmid.
    """
    if mode == "pve":
        return _make_lxc(base, name="200", **{"kento-mode": "pve"})
    if mode == "pve-vm":
        return _make_vm(base, name="myvm", **{"kento-mode": "pve-vm",
                                              "kento-vmid": "200"})
    if mode == "vm":
        return _make_vm(base, **{"kento-mode": "vm"})
    return _make_lxc(base, **{"kento-mode": "lxc"})


def _snapshot(d: Path, mode: str):
    """Build a typed handle for a fake container dir (status probe mocked)."""
    with patch("kento.is_running", return_value=False):
        return _instances._load_snapshot(d, mode)


# -- M5 start: delegates per mode + self-updates status ----------------------


@pytest.mark.parametrize("mode", ["lxc", "pve", "vm", "pve-vm"])
def test_start_delegates_to_mode_aware_func(tmp_path, mode):
    d = _make_for_mode(tmp_path, mode)
    inst = _snapshot(d, mode)
    with patch("kento.start.start") as mock_start, \
            patch("kento.is_running", return_value=True), \
            patch("kento.pve_config_exists", return_value=True):
        inst.start()
    # Delegation: called once with this handle's name/dir/raw mode.
    mock_start.assert_called_once_with(inst.name, container_dir=d, mode=mode)
    # Self-update from a fresh probe (running + config present => RUNNING).
    assert inst.status is Status.RUNNING


def test_start_self_update_reflects_probe_not_a_literal(tmp_path):
    # The status self-update RE-RESOLVES (does not blindly set RUNNING): an LXC
    # whose probe still says not-running after start reads STOPPED, honestly.
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    with patch("kento.start.start"), patch("kento.is_running", return_value=False):
        inst.start()
    assert inst.status is Status.STOPPED


# -- M6 stop: full LOCKED semantics (typed layer owns all timing) ------------


def test_stop_graceful_passes_graceful_only(tmp_path):
    # force=False issues a graceful (no-kill) stop: shutdown(graceful_only=True).
    # It went down within the window -> no StopTimeout, status STOPPED.
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    with patch("kento.stop.shutdown") as mock_sd, \
            patch("kento.is_running", return_value=False):
        inst.stop()
    mock_sd.assert_called_once_with(
        inst.name, graceful_only=True, container_dir=d, mode="lxc",
    )
    assert inst.status is Status.STOPPED


@pytest.mark.parametrize("mode", ["lxc", "vm"])
def test_stop_graceful_stubborn_raises_stop_timeout(tmp_path, mode):
    # LOCKED: a graceful stop that leaves the instance UP raises StopTimeout and
    # NEVER hard-kills (no second forced shutdown call). Verified for BOTH lxc
    # (--nokill) and vm (no-SIGKILL). timeout=0 keeps the poll instantaneous;
    # the still-running probe drives the raise.
    maker = _make_vm if mode == "vm" else _make_lxc
    d = maker(tmp_path, **{"kento-mode": mode})
    inst = _snapshot(d, mode)
    with patch("kento.stop.shutdown") as mock_sd, \
            patch("kento.is_running", return_value=True):
        with pytest.raises(StopTimeout, match="cannot stop; try force"):
            inst.stop(timeout=0)
    # Exactly ONE shutdown call (graceful) — no forced fallback kill.
    mock_sd.assert_called_once_with(
        inst.name, graceful_only=True, container_dir=d, mode=mode,
    )
    assert inst.status is Status.RUNNING


def test_stop_timeout_subclasses_state_error():
    # StopTimeout is catchable as StateError / KentoError (back-compat handlers).
    from kento.errors import StateError, KentoError
    assert issubclass(StopTimeout, StateError)
    assert issubclass(StopTimeout, KentoError)


def test_stop_force_immediate_kill(tmp_path):
    # force=True, timeout None/0 -> immediate shutdown(force=True). No graceful
    # phase, no timeout forwarded, single call.
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    with patch("kento.stop.shutdown") as mock_sd, \
            patch("kento.is_running", return_value=False):
        inst.stop(force=True)
    mock_sd.assert_called_once_with(
        inst.name, force=True, container_dir=d, mode="lxc",
    )
    assert inst.status is Status.STOPPED


def test_stop_force_timeout_zero_is_immediate(tmp_path):
    # Explicit timeout=0 with force is the immediate-kill case (no grace poll).
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    with patch("kento.stop.shutdown") as mock_sd, \
            patch("kento._instances._wait_until_down") as mock_wait, \
            patch("kento.is_running", return_value=False):
        inst.stop(force=True, timeout=0)
    mock_wait.assert_not_called()  # no grace window
    mock_sd.assert_called_once_with(
        inst.name, force=True, container_dir=d, mode="lxc",
    )


def test_stop_force_with_timeout_grace_then_kill(tmp_path):
    # force=True, timeout>0: graceful FIRST, then a bounded wait, then a hard
    # kill ONLY because it is still up. Two shutdown calls in order:
    # graceful_only then force. The grace window is owned by the typed layer.
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    with patch("kento.stop.shutdown") as mock_sd, \
            patch("kento._instances._wait_until_down", return_value=False), \
            patch("kento.is_running", return_value=False):
        inst.stop(force=True, timeout=10)
    assert mock_sd.call_count == 2
    assert mock_sd.call_args_list[0].kwargs.get("graceful_only") is True
    assert mock_sd.call_args_list[1].kwargs.get("force") is True
    # Neither delegated call forwards `timeout` (shutdown rejects it).
    for call in mock_sd.call_args_list:
        assert "timeout" not in call.kwargs


def test_stop_force_with_timeout_no_kill_if_it_goes_down(tmp_path):
    # force=True, timeout>0 but the guest goes down during the grace window:
    # NO hard kill (only the graceful call). grace-then-kill skips the kill.
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    with patch("kento.stop.shutdown") as mock_sd, \
            patch("kento._instances._wait_until_down", return_value=True), \
            patch("kento.is_running", return_value=False):
        inst.stop(force=True, timeout=10)
    mock_sd.assert_called_once_with(
        inst.name, graceful_only=True, container_dir=d, mode="lxc",
    )


def test_stop_graceful_default_timeout_is_15(tmp_path):
    # timeout None -> default 15s grace window passed to the waiter.
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    with patch("kento.stop.shutdown"), \
            patch("kento._instances._wait_until_down", return_value=True) as mw, \
            patch("kento.is_running", return_value=False):
        inst.stop()
    # _wait_until_down(dir, mode, 15)
    assert mw.call_args[0][2] == 15


@pytest.mark.parametrize("mode", ["lxc", "pve", "vm", "pve-vm"])
def test_stop_graceful_delegates_per_mode(tmp_path, mode):
    # The wrapped shutdown dispatches on mode internally; we just pass our raw
    # mode through. Assert the per-mode graceful call shape (delegation).
    d = _make_for_mode(tmp_path, mode)
    inst = _snapshot(d, mode)
    with patch("kento.stop.shutdown") as mock_sd, \
            patch("kento._instances._wait_until_down", return_value=True), \
            patch("kento.is_running", return_value=False), \
            patch("kento.pve_config_exists", return_value=True):
        inst.stop()
    mock_sd.assert_called_once_with(
        inst.name, graceful_only=True, container_dir=d, mode=mode,
    )


def test_stop_graceful_unobservable_probe_does_not_raise(tmp_path):
    # _wait_until_down is total: a probe that raises => treated as DOWN (we do
    # NOT raise StopTimeout when we cannot even observe the instance).
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    from kento.errors import KentoError

    def _boom(*a, **k):
        raise KentoError("node unreachable")

    with patch("kento.stop.shutdown"), patch("kento.is_running", side_effect=_boom):
        # Must not raise StopTimeout; status resolves to UNKNOWN (total probe).
        inst.stop(timeout=0)
    assert inst.status is Status.UNKNOWN


# -- M6 _wait_until_down helper ----------------------------------------------


def test_wait_until_down_returns_true_when_down(tmp_path):
    d = _make_lxc(tmp_path)
    with patch("kento.is_running", return_value=False):
        assert _instances._wait_until_down(d, "lxc", 0) is True


def test_wait_until_down_returns_false_when_still_up(tmp_path):
    d = _make_lxc(tmp_path)
    with patch("kento.is_running", return_value=True):
        # timeout=0 -> single probe, still up -> False (no real sleep).
        assert _instances._wait_until_down(d, "lxc", 0) is False


def test_wait_until_down_unobservable_is_down(tmp_path):
    d = _make_lxc(tmp_path)
    from kento.errors import KentoError
    with patch("kento.is_running", side_effect=KentoError("x")):
        assert _instances._wait_until_down(d, "lxc", 0) is True


# -- M7 destroy: delegate, force, dead-handle --------------------------------


@pytest.mark.parametrize("mode", ["lxc", "pve", "vm", "pve-vm"])
def test_destroy_delegates_per_mode(tmp_path, mode):
    d = _make_for_mode(tmp_path, mode)
    inst = _snapshot(d, mode)
    with patch("kento.destroy.destroy") as mock_destroy:
        inst.destroy()
    # destroy.destroy(name, force, *, container_dir, mode) — positional force.
    mock_destroy.assert_called_once_with(
        inst.name, False, container_dir=d, mode=mode,
    )


def test_destroy_force_forwards_force(tmp_path):
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    with patch("kento.destroy.destroy") as mock_destroy:
        inst.destroy(force=True)
    mock_destroy.assert_called_once_with(
        inst.name, True, container_dir=d, mode="lxc",
    )


def test_destroy_marks_handle_dead(tmp_path):
    # After destroy, the handle is dead: any reuse raises InstanceNotFoundError.
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    with patch("kento.destroy.destroy"):
        inst.destroy()
    assert inst._dead is True
    for call in (
        lambda: inst.start(),
        lambda: inst.stop(),
        lambda: inst.destroy(),
        lambda: inst.scrub(),
        lambda: inst.refresh(),
    ):
        with pytest.raises(InstanceNotFoundError, match="was destroyed"):
            call()


def test_destroy_failure_leaves_handle_alive(tmp_path):
    # If destroy.destroy raises, the instance is NOT gone — the handle must stay
    # alive (we only flip _dead on success), so the caller can retry.
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    from kento.errors import StateError
    with patch("kento.destroy.destroy", side_effect=StateError("running")):
        with pytest.raises(StateError):
            inst.destroy()
    assert inst._dead is False


# -- M8 scrub: delegate + re-pin (via reset) + status self-update ------------


@pytest.mark.parametrize("mode", ["lxc", "pve", "vm", "pve-vm"])
def test_scrub_delegates_per_mode(tmp_path, mode):
    d = _make_for_mode(tmp_path, mode)
    inst = _snapshot(d, mode)
    with patch("kento.reset.reset") as mock_reset, \
            patch("kento.is_running", return_value=False), \
            patch("kento.pve_config_exists", return_value=True):
        inst.scrub()
    mock_reset.assert_called_once_with(inst.name, container_dir=d, mode=mode)
    # scrub leaves the instance stopped (config present + not running).
    assert inst.status is Status.STOPPED


def test_scrub_self_updates_status(tmp_path):
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    # Pretend it started life RUNNING in the cache; scrub re-resolves to STOPPED.
    # status is getter-only (Block 11 M9) — seed the BACKING field the lifecycle
    # methods own (public assignment now raises AttributeError; see the M9 tests).
    inst._status = Status.RUNNING
    with patch("kento.reset.reset"), patch("kento.is_running", return_value=False):
        inst.scrub()
    assert inst.status is Status.STOPPED


# -- Dispatch resolution: methods live on the BASE, not forked per subclass --


def test_lifecycle_methods_defined_on_base_only(tmp_path):
    # Disclosed dispatch resolution: the base delegates to the mode-aware wrapped
    # funcs; subclasses do NOT override (no fabricated per-subclass re-calls).
    for meth in ("start", "stop", "destroy", "scrub"):
        assert meth in Instance.__dict__, f"{meth} should be on the base"
        assert meth not in SystemContainer.__dict__, f"{meth} not overridden"
        assert meth not in VirtualMachine.__dict__, f"{meth} not overridden"


# --------------------------------------------------------------------------- #
# Block 10 — recovery classmethods (M3 adopt / M4 prune_orphans) + M11
# instance.diagnose(). adopt/prune_orphans WRAP reconcile.{adopt,reap_orphans};
# diagnose WRAPS diagnose.run_diagnostics. We patch the wrapped funcs to assert
# DELEGATION (no forked logic) + the exact call shape, and the typed projection.
# --------------------------------------------------------------------------- #

from kento import Diagnosis, DiagnosisDomain, ReclaimReport  # noqa: E402


# -- M3 adopt: delegate + return a fresh kind-checked handle -----------------


def test_adopt_delegates_and_returns_handle(tmp_path):
    # adopt(name) -> reconcile.adopt(name), then cls.get(name) for a live handle.
    d = _make_lxc(tmp_path, name="200", **{"kento-mode": "pve"})
    with patch("kento.reconcile.adopt") as mock_adopt, \
            patch("kento.resolve_any", return_value=(d, "pve")), \
            patch("kento.is_running", return_value=False), \
            patch("kento.pve_config_exists", return_value=True):
        handle = Instance.adopt("200")
    mock_adopt.assert_called_once_with("200")
    assert isinstance(handle, SystemContainer)
    assert handle.name == "200"


def test_adopt_typed_raises_pass_through(tmp_path):
    # reconcile.adopt's typed raises (ModeError/StateError) propagate unchanged;
    # adopt does NOT swallow them or call get() on a failure.
    with patch("kento.reconcile.adopt",
               side_effect=ModeError("not a PVE instance")) as mock_adopt, \
            patch.object(Instance, "get") as mock_get:
        with pytest.raises(ModeError):
            Instance.adopt("plainbox")
    mock_adopt.assert_called_once()
    mock_get.assert_not_called()


def test_adopt_on_subclass_kind_checks(tmp_path):
    # Calling SystemContainer.adopt on a name that healed to a VM raises get's
    # kind-mismatch (get is polymorphic), not a wrong-typed handle.
    d = _make_vm(tmp_path, name="myvm", **{"kento-mode": "pve-vm",
                                           "kento-vmid": "200"})
    with patch("kento.reconcile.adopt"), \
            patch("kento.resolve_any", return_value=(d, "pve-vm")), \
            patch("kento.is_running", return_value=False), \
            patch("kento.pve_config_exists", return_value=True):
        with pytest.raises(InstanceNotFoundError):
            SystemContainer.adopt("myvm")
        # The base / the right kind succeed.
        assert isinstance(VirtualMachine.adopt("myvm"), VirtualMachine)


def test_adopt_is_a_classmethod():
    assert isinstance(Instance.__dict__["adopt"], classmethod)


# -- M4 prune_orphans: per-cls scope + ReclaimReport mapping -----------------


def _reap_entry(name, *, reaped=False, error=None, mode="pve"):
    return {"name": name, "vmid": 200, "mode": mode,
            "reaped": reaped, "error": error}


def test_prune_orphans_dry_run_default_maps_would_reap():
    # reap=False (default) => dry_run=True, reclaimed = would-reap names, no
    # reaping happened.
    entries = [_reap_entry("ghost1"), _reap_entry("ghost2")]
    with patch("kento.reconcile.reap_orphans", return_value=entries) as mock_reap:
        report = Instance.prune_orphans()
    mock_reap.assert_called_once_with(False, None)  # base => both namespaces
    assert isinstance(report, ReclaimReport)
    assert report.dry_run is True
    assert report.reclaimed == ("ghost1", "ghost2")
    assert report.failed == ()
    assert report.ok is True


def test_prune_orphans_reap_maps_reaped_and_failures():
    # reap=True => dry_run=False; reclaimed = successfully reaped names; failed =
    # (name, error) pairs surfaced (1.6.2 contract).
    entries = [
        _reap_entry("ghost1", reaped=True),
        _reap_entry("ghost2", reaped=False, error="pct destroy failed"),
    ]
    with patch("kento.reconcile.reap_orphans", return_value=entries) as mock_reap:
        report = Instance.prune_orphans(reap=True)
    mock_reap.assert_called_once_with(True, None)
    assert report.dry_run is False
    assert report.reclaimed == ("ghost1",)
    assert report.failed == (("ghost2", "pct destroy failed"),)
    assert report.ok is False


def test_prune_orphans_scope_mirrors_list_per_cls():
    with patch("kento.reconcile.reap_orphans", return_value=[]) as mock_reap:
        Instance.prune_orphans()
        SystemContainer.prune_orphans()
        VirtualMachine.prune_orphans()
    scopes = [c.args[1] for c in mock_reap.call_args_list]
    assert scopes == [None, "lxc", "vm"]


def test_prune_orphans_empty_is_clean_dry_run_report():
    with patch("kento.reconcile.reap_orphans", return_value=[]):
        report = Instance.prune_orphans()
    assert report == ReclaimReport(dry_run=True, reclaimed=(), failed=())


def test_prune_orphans_is_a_classmethod():
    assert isinstance(Instance.__dict__["prune_orphans"], classmethod)


# -- M11 instance.diagnose(): wraps run_diagnostics, filters INSTANCE+self ----


def _diag_finding(category, severity, scope, message="m", remediation=None):
    return {"category": category, "severity": severity, "scope": scope,
            "message": message, "remediation": remediation}


def test_instance_diagnose_filters_to_instance_domain_and_self(tmp_path):
    d = _make_lxc(tmp_path, name="mybox")
    inst = _snapshot(d, "lxc")
    report = {"checks": [
        _diag_finding("status", "ok", "mybox"),
        _diag_finding("network", "ok", "mybox"),
        _diag_finding("apparmor", "ok", "host"),      # HOST — dropped
        _diag_finding("hold", "ok", "host"),          # IMAGE — dropped
        _diag_finding("orphan", "warn", "mybox"),     # HOST domain — dropped
        _diag_finding("status", "ok", "otherbox"),    # other subject — dropped
    ], "problem_count": 1, "instances_scanned": 2}
    with patch("kento.diagnose.run_diagnostics",
               return_value=report) as mock_run:
        result = inst.diagnose()
    mock_run.assert_called_once_with("mybox")
    assert isinstance(result, Diagnosis)
    assert {f.check for f in result.findings} == {"status", "network"}
    assert all(f.domain is DiagnosisDomain.INSTANCE for f in result.findings)
    assert all(f.subject == "mybox" for f in result.findings)


def test_instance_diagnose_resolves_by_name_and_raises_on_miss(tmp_path):
    d = _make_lxc(tmp_path, name="mybox")
    inst = _snapshot(d, "lxc")
    with patch("kento.diagnose.run_diagnostics",
               side_effect=InstanceNotFoundError("gone")):
        with pytest.raises(InstanceNotFoundError):
            inst.diagnose()


def test_instance_diagnose_guards_dead_handle(tmp_path):
    d = _make_lxc(tmp_path, name="mybox")
    inst = _snapshot(d, "lxc")
    inst._dead = True
    with pytest.raises(InstanceNotFoundError):
        inst.diagnose()


def test_instance_diagnose_on_base_only(tmp_path):
    # diagnose lives on the base (shared by all kinds); not overridden.
    assert "diagnose" in Instance.__dict__
    assert "diagnose" not in SystemContainer.__dict__
    assert "diagnose" not in VirtualMachine.__dict__


# =========================================================================== #
# Block 11 — M9 settable-property mutation.
#
# The field model is now typed PROPERTIES backed by _-prefixed fields: getter-
# only fields raise AttributeError on assignment; settable fields delegate to
# set_cmd.set_cmd (mocked), lock-guarded, live-probe-gated, with catch-reverse.
# All I/O (set_cmd / is_running / kento_lock) is mocked — no live process.
# =========================================================================== #

from kento.errors import StateError  # noqa: E402


# -- Getter-only fields raise AttributeError on assignment (§11.2 M9) ---------


@pytest.mark.parametrize(
    "field, value",
    [
        ("status", Status.RUNNING),
        ("sources", ()),
        ("platform_profile", None),
        ("storage", StorageMode.OVERLAY),
        ("created", datetime.now()),
        ("environment", {}),
        ("nesting", True),
        ("name", "renamed"),
        ("forwards", {}),
    ],
)
def test_getter_only_fields_reject_assignment(tmp_path, field, value):
    # Assigning a getter-only property raises AttributeError (no setter idiom).
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    with pytest.raises(AttributeError):
        setattr(inst, field, value)


def test_unprivileged_getter_only(tmp_path):
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    with pytest.raises(AttributeError):
        inst.unprivileged = True


def test_forwards_setter_deferred_to_phase5(tmp_path):
    # M9 makes forwards LIVE-settable, but that is the Phase-5 network rework.
    # In THIS block forwards is getter-only -> assignment raises AttributeError.
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    with pytest.raises(AttributeError):
        inst.forwards = {}


# -- Settable setters: stopped-only guard via LIVE is_running probe -----------


@pytest.mark.parametrize(
    "field, value",
    [
        ("hostname", "newhost"),
        ("network", NetworkConnection(mode=NetworkMode.DHCP,
                                      link_config={"bridge": "br0"})),
        ("lxc_args", ["lxc.foo = bar"]),
    ],
)
def test_stopped_only_setters_raise_when_running(tmp_path, field, value):
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    with patch("kento.is_running", return_value=True), \
            patch("kento.set_cmd.set_cmd") as mock_set, \
            patch("kento.locking.kento_lock"):
        with pytest.raises(StateError):
            setattr(inst, field, value)
    # The stopped-only guard fired BEFORE any persistence.
    mock_set.assert_not_called()


def test_qemu_args_setter_raises_when_running(tmp_path):
    d = _make_vm(tmp_path)
    inst = _snapshot(d, "vm")
    with patch("kento.is_running", return_value=True), \
            patch("kento.set_cmd.set_cmd") as mock_set, \
            patch("kento.locking.kento_lock"):
        with pytest.raises(StateError):
            inst.qemu_args = ["-foo"]
    mock_set.assert_not_called()


# -- hostname setter: delegates to set_cmd(hostname=), updates cache ----------


def test_hostname_setter_persists_and_caches(tmp_path):
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    with patch("kento.is_running", return_value=False), \
            patch("kento.set_cmd.set_cmd", return_value=0) as mock_set, \
            patch("kento.locking.kento_lock"):
        inst.hostname = "newname"
    mock_set.assert_called_once_with(inst.name, hostname="newname")
    assert inst.hostname == "newname"  # cache updated on success


# -- network setter: faithful whole-value decomposition (JC3) -----------------


def _net_call_kwargs(mock_set):
    """The kwargs of the single set_cmd call (positional name dropped)."""
    assert mock_set.call_count == 1
    return mock_set.call_args.kwargs


def test_network_setter_static_decomposition(tmp_path):
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    conn = NetworkConnection(
        mode=NetworkMode.STATIC,
        link_config={"bridge": "br0"},
        ip_config={"address": "10.0.0.5", "subnet": "24",
                   "gateway": "10.0.0.1", "dns1": "1.1.1.1"},
    )
    with patch("kento.is_running", return_value=False), \
            patch("kento.set_cmd.set_cmd", return_value=0) as mock_set, \
            patch("kento.locking.kento_lock"):
        inst.network = conn
    kw = _net_call_kwargs(mock_set)
    assert kw == {"network": "bridge=br0", "ip": "10.0.0.5/24",
                  "gateway": "10.0.0.1", "dns": "1.1.1.1"}
    assert inst.network is conn


def test_network_setter_dhcp_clears_static(tmp_path):
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    conn = NetworkConnection(mode=NetworkMode.DHCP,
                             link_config={"bridge": "br0"})
    with patch("kento.is_running", return_value=False), \
            patch("kento.set_cmd.set_cmd", return_value=0) as mock_set, \
            patch("kento.locking.kento_lock"):
        inst.network = conn
    kw = _net_call_kwargs(mock_set)
    # DHCP: ip=dhcp clears static ip+gw; dns='' clears dns -> no stale lingers.
    assert kw == {"network": "bridge=br0", "ip": "dhcp", "dns": ""}


@pytest.mark.parametrize(
    "mode, net_str",
    [(NetworkMode.HOST, "host"),
     (NetworkMode.USER, "usermode"),
     (NetworkMode.DISABLED, "none")],
)
def test_network_setter_nonbridge_clears_l3(tmp_path, mode, net_str):
    # HOST/USER/DISABLED: must clear stale static ip/gateway/dns (ip=dhcp, dns='').
    maker = _make_vm if mode is NetworkMode.USER else _make_lxc
    md = "vm" if mode is NetworkMode.USER else "lxc"
    d = maker(tmp_path, **{"kento-mode": md})
    inst = _snapshot(d, md)
    conn = NetworkConnection(mode=mode)
    with patch("kento.is_running", return_value=False), \
            patch("kento.set_cmd.set_cmd", return_value=0) as mock_set, \
            patch("kento.locking.kento_lock"):
        inst.network = conn
    kw = _net_call_kwargs(mock_set)
    assert kw == {"network": net_str, "ip": "dhcp", "dns": ""}


def test_network_setter_passes_mac_when_present(tmp_path):
    d = _make_vm(tmp_path)
    inst = _snapshot(d, "vm")
    conn = NetworkConnection(
        mode=NetworkMode.USER,
        link_config={"mac": "52:54:00:12:34:56"},
    )
    with patch("kento.is_running", return_value=False), \
            patch("kento.set_cmd.set_cmd", return_value=0) as mock_set, \
            patch("kento.locking.kento_lock"):
        inst.network = conn
    kw = _net_call_kwargs(mock_set)
    assert kw["mac"] == "52:54:00:12:34:56"


def test_network_setter_rejects_dns2(tmp_path):
    # dns2 cannot round-trip through set_cmd's single --dns -> reject (not drop).
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    conn = NetworkConnection(
        mode=NetworkMode.STATIC,
        link_config={"bridge": "br0"},
        ip_config={"address": "10.0.0.5", "subnet": "24", "dns1": "1.1.1.1",
                   "dns2": "8.8.8.8"},
    )
    with patch("kento.is_running", return_value=False), \
            patch("kento.set_cmd.set_cmd") as mock_set, \
            patch("kento.locking.kento_lock"):
        with pytest.raises(ValidationError, match="dns2"):
            inst.network = conn
    mock_set.assert_not_called()  # rejected before persistence


# -- resources setter: Jei run-33 deferral (mode-appropriate running error) ---


def test_resources_setter_stopped_persists(tmp_path):
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    with patch("kento.is_running", return_value=False), \
            patch("kento.set_cmd.set_cmd", return_value=0) as mock_set, \
            patch("kento.locking.kento_lock"):
        inst.resources = {"memory": 2048, "cores": 4}
    mock_set.assert_called_once_with(inst.name, memory=2048, cores=4)
    assert inst.resources == {"memory": 2048, "cores": 4}


@pytest.mark.parametrize("mode", ["lxc", "pve", "vm", "pve-vm"])
def test_resources_setter_running_raises_state_error(tmp_path, mode):
    # Jei run-33: running resources raises StateError for ALL modes this block.
    d = _make_for_mode(tmp_path, mode)
    inst = _snapshot(d, mode)
    with patch("kento.is_running", return_value=True), \
            patch("kento.set_cmd.set_cmd") as mock_set, \
            patch("kento.locking.kento_lock"), \
            patch("kento.pve_config_exists", return_value=True):
        with pytest.raises(StateError) as exc:
            inst.resources = {"memory": 2048}
    mock_set.assert_not_called()
    msg = str(exc.value)
    # Capability-aware distinction kept VISIBLE in the message (not erased).
    if mode in ("vm", "pve-vm"):
        assert "hotplug" in msg
    else:
        assert "future release" in msg


def test_resources_setter_rejects_unknown_key(tmp_path):
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    # Decompose fails before lock/probe -> ValidationError, no set_cmd.
    with patch("kento.set_cmd.set_cmd") as mock_set:
        with pytest.raises(ValidationError, match="unsupported resources key"):
            inst.resources = {"swap": 512}
    mock_set.assert_not_called()


def test_resources_setter_rejects_non_int(tmp_path):
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    with pytest.raises(ValidationError, match="must be an int"):
        inst.resources = {"memory": "lots"}


# -- lxc_args / qemu_args / extra_args setters --------------------------------


def test_lxc_args_setter_replace(tmp_path):
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    with patch("kento.is_running", return_value=False), \
            patch("kento.set_cmd.set_cmd", return_value=0) as mock_set, \
            patch("kento.locking.kento_lock"):
        inst.lxc_args = ["lxc.cap.drop = sys_admin"]
    mock_set.assert_called_once_with(
        inst.name, lxc_args=["lxc.cap.drop = sys_admin"])
    assert inst.lxc_args == ("lxc.cap.drop = sys_admin",)


def test_lxc_args_setter_empty_clears(tmp_path):
    # An empty list reaches set_cmd as the CLEAR sentinel [""].
    d = _make_lxc(tmp_path, **{"kento-lxc-args": "lxc.old = 1"})
    inst = _snapshot(d, "lxc")
    with patch("kento.is_running", return_value=False), \
            patch("kento.set_cmd.set_cmd", return_value=0) as mock_set, \
            patch("kento.locking.kento_lock"):
        inst.lxc_args = []
    mock_set.assert_called_once_with(inst.name, lxc_args=[""])
    assert inst.lxc_args == ()


def test_qemu_args_setter_replace(tmp_path):
    d = _make_vm(tmp_path)
    inst = _snapshot(d, "vm")
    with patch("kento.is_running", return_value=False), \
            patch("kento.set_cmd.set_cmd", return_value=0) as mock_set, \
            patch("kento.locking.kento_lock"):
        inst.qemu_args = ["-cpu host"]
    mock_set.assert_called_once_with(inst.name, qemu_args=["-cpu host"])
    assert inst.qemu_args == ("-cpu host",)


def test_extra_args_setter_persists_pve_args_and_replaces_profile(tmp_path):
    # extra_args -> set_cmd(pve_args=); cached platform_profile rebuilt coherently.
    d = _make_for_mode(tmp_path, "pve")
    inst = _snapshot(d, "pve")
    assert inst.platform_profile.mode is PlatformMode.PVE
    old_profile = inst.platform_profile
    with patch("kento.is_running", return_value=False), \
            patch("kento.set_cmd.set_cmd", return_value=0) as mock_set, \
            patch("kento.locking.kento_lock"):
        inst.extra_args = ["lxc.cgroup2.devices.allow = c 10:200 rwm"]
    mock_set.assert_called_once_with(
        inst.name, pve_args=["lxc.cgroup2.devices.allow = c 10:200 rwm"])
    # The getter + platform_profile.extra_args reflect the new value (coherent).
    assert inst.extra_args == ("lxc.cgroup2.devices.allow = c 10:200 rwm",)
    assert inst.platform_profile.extra_args == inst.extra_args
    # Only extra_args changed; mode/mid preserved (dataclasses.replace).
    assert inst.platform_profile.mode is old_profile.mode
    assert inst.platform_profile.mid == old_profile.mid
    # platform_profile itself stays getter-only.
    with pytest.raises(AttributeError):
        inst.platform_profile = old_profile


# -- Lock held + live probe inside the lock (JC6) -----------------------------


def test_setter_holds_kento_lock_around_set_cmd(tmp_path):
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    order = []
    import contextlib

    @contextlib.contextmanager
    def fake_lock():
        order.append("lock-acquire")
        yield
        order.append("lock-release")

    def fake_set(*a, **k):
        order.append("set_cmd")
        return 0

    with patch("kento.is_running", return_value=False), \
            patch("kento.set_cmd.set_cmd", side_effect=fake_set), \
            patch("kento.locking.kento_lock", fake_lock):
        inst.hostname = "h"
    # set_cmd runs strictly between lock acquire and release.
    assert order == ["lock-acquire", "set_cmd", "lock-release"]


def test_setter_live_probe_not_cached_status(tmp_path):
    # The guard uses a LIVE is_running probe, NOT the cached status: a handle
    # whose cached status is STOPPED but whose live probe says running raises.
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    assert inst.status is Status.STOPPED  # cached
    with patch("kento.is_running", return_value=True), \
            patch("kento.set_cmd.set_cmd") as mock_set, \
            patch("kento.locking.kento_lock"):
        with pytest.raises(StateError):
            inst.hostname = "h"
    mock_set.assert_not_called()


# -- Catch-and-reverse rollback on a simulated set_cmd failure (JC5) ----------


def test_setter_catch_reverse_restores_on_failure(tmp_path):
    # set_cmd raises on the FIRST (new) call -> setter re-invokes set_cmd with the
    # OLD params (rollback) and re-raises the original error; cache unchanged.
    d = _make_lxc(tmp_path, **{"hostname": "orig"})
    inst = _snapshot(d, "lxc")
    assert inst.hostname == "orig"

    calls = []

    def flaky_set(name, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise StateError("boom mid-write")
        return 0  # the rollback re-invocation succeeds

    with patch("kento.is_running", return_value=False), \
            patch("kento.set_cmd.set_cmd", side_effect=flaky_set), \
            patch("kento.locking.kento_lock"):
        with pytest.raises(StateError, match="boom mid-write"):
            inst.hostname = "newhost"

    # Two calls: the failed new write, then the rollback with the OLD value.
    assert calls == [{"hostname": "newhost"}, {"hostname": "orig"}]
    # Cache NOT updated (the setter only caches on success).
    assert inst.hostname == "orig"


def test_setter_rollback_failure_is_swallowed_original_reraised(tmp_path, caplog):
    # If the rollback ITSELF fails, the ORIGINAL error still propagates and the
    # rollback failure is logged (best-effort, mirrors create.py:_run_cleanup).
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")

    def always_fail(name, **kwargs):
        raise StateError("primary failure")

    with patch("kento.is_running", return_value=False), \
            patch("kento.set_cmd.set_cmd", side_effect=always_fail), \
            patch("kento.locking.kento_lock"):
        with pytest.raises(StateError, match="primary failure"):
            inst.hostname = "newhost"


# -- Dead-handle guard on setters (§11.2 M7) ----------------------------------


def test_setter_guards_dead_handle(tmp_path):
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    inst._dead = True
    with patch("kento.set_cmd.set_cmd") as mock_set:
        with pytest.raises(InstanceNotFoundError):
            inst.hostname = "h"
    mock_set.assert_not_called()


# -- ModeError from set_cmd propagates (e.g. qemu_args on LXC) -----------------


def test_setter_propagates_set_cmd_mode_error(tmp_path):
    # An invalid field-for-mode is set_cmd's own ModeError, surfaced not swallowed
    # (it validates before mutating, so the rollback re-invoke also raises and is
    # logged; the original ModeError propagates). Use a VALID-on-the-type setter
    # (qemu_args exists on VirtualMachine) whose set_cmd raises ModeError.
    d = _make_vm(tmp_path)
    inst = _snapshot(d, "vm")
    with patch("kento.is_running", return_value=False), \
            patch("kento.set_cmd.set_cmd",
                  side_effect=ModeError("qemu-arg is VM-only")), \
            patch("kento.locking.kento_lock"):
        with pytest.raises(ModeError):
            inst.qemu_args = ["-foo"]


# --------------------------------------------------------------------------- #
# Block 13 — interactive/runtime methods (ADDITIVE + 1 authorized exec_cmd
# touch): attach (M12, base) / exec (M13, SC) / logs (M14, SC) / suspend+resume
# (M17/M18, VM). attach/exec/suspend/resume WRAP their runtime func; logs is an
# additive captured-line generator. Mocked runtime; no live process runs.
# --------------------------------------------------------------------------- #


# -- M12 attach: on the BASE, delegates for all modes, drops int->None --------


@pytest.mark.parametrize("mode", ["lxc", "pve", "vm", "pve-vm"])
def test_attach_delegates_all_modes(tmp_path, mode):
    d = _make_for_mode(tmp_path, mode)
    inst = _snapshot(d, mode)
    with patch("kento.attach.attach", return_value=0) as mock_attach:
        result = inst.attach()
    # Delegates to the runtime by name (it re-dispatches per mode internally).
    mock_attach.assert_called_once_with(inst.name)
    # int -> None (interactive console, not a status check; brief JC3).
    assert result is None


def test_attach_is_on_base_instance():
    # M12 placement: attach is shared by all kinds, so it lives on the base —
    # both concrete kinds inherit the SAME method object.
    assert SystemContainer.attach is Instance.attach
    assert VirtualMachine.attach is Instance.attach


def test_attach_drops_nonzero_exit_code(tmp_path):
    # Even a non-zero runtime return is dropped — attach is not a status check.
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    with patch("kento.attach.attach", return_value=3):
        assert inst.attach() is None


def test_attach_dead_handle_raises(tmp_path):
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    inst._dead = True
    with patch("kento.attach.attach") as mock_attach:
        with pytest.raises(InstanceNotFoundError):
            inst.attach()
    mock_attach.assert_not_called()


# -- M13 exec: SC-only; threads tty/user/env; returns code; no raise ----------


def test_exec_only_on_system_container():
    assert hasattr(SystemContainer, "exec")
    # A VM has no in-guest agent -> exec is NOT on VirtualMachine (§11.3).
    assert not hasattr(VirtualMachine, "exec")


def test_exec_delegates_with_defaults(tmp_path):
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    with patch("kento.exec_cmd.exec_cmd", return_value=0) as mock_exec:
        rc = inst.exec(["ls", "-la"])
    mock_exec.assert_called_once_with(
        inst.name, ["ls", "-la"], tty=False, user=None, env=None,
    )
    assert rc == 0


def test_exec_threads_tty_user_env(tmp_path):
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    with patch("kento.exec_cmd.exec_cmd", return_value=0) as mock_exec:
        inst.exec(["id"], tty=True, user="alice", env={"K": "V"})
    mock_exec.assert_called_once_with(
        inst.name, ["id"], tty=True, user="alice", env={"K": "V"},
    )


def test_exec_normalizes_command_to_list(tmp_path):
    # M13 takes a Sequence[str]; a tuple is normalized to the list exec_cmd wants.
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    with patch("kento.exec_cmd.exec_cmd", return_value=0) as mock_exec:
        inst.exec(("echo", "hi"))
    passed = mock_exec.call_args[0][1]
    assert passed == ["echo", "hi"]
    assert isinstance(passed, list)


def test_exec_returns_nonzero_without_raising(tmp_path):
    # §11.9 / M13: non-zero is normal info, returned not raised.
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    with patch("kento.exec_cmd.exec_cmd", return_value=2):
        assert inst.exec(["grep", "x", "/nope"]) == 2


def test_exec_dead_handle_raises(tmp_path):
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    inst._dead = True
    with patch("kento.exec_cmd.exec_cmd") as mock_exec:
        with pytest.raises(InstanceNotFoundError):
            inst.exec(["ls"])
    mock_exec.assert_not_called()


# -- M14 logs: SC-only; captured-line generator; argv from follow/lines -------


def test_logs_only_on_system_container():
    assert hasattr(SystemContainer, "logs")
    assert not hasattr(VirtualMachine, "logs")


def test_logs_argv_snapshot_lxc(tmp_path):
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    # follow=False, no lines: a bare snapshot.
    assert inst._logs_argv(follow=False, lines=None) == [
        "lxc-attach", "-n", inst.name, "--", "journalctl"]


def test_logs_argv_lines_tail(tmp_path):
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    assert inst._logs_argv(follow=False, lines=50) == [
        "lxc-attach", "-n", inst.name, "--", "journalctl", "-n", "50"]


def test_logs_argv_follow(tmp_path):
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    assert inst._logs_argv(follow=True, lines=None) == [
        "lxc-attach", "-n", inst.name, "--", "journalctl", "-f"]


def test_logs_argv_follow_with_lines(tmp_path):
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    assert inst._logs_argv(follow=True, lines=10) == [
        "lxc-attach", "-n", inst.name, "--", "journalctl", "-f", "-n", "10"]


def test_logs_argv_pve_lxc_uses_pct_exec_and_vmid(tmp_path):
    d = _make_for_mode(tmp_path, "pve")  # dir name "200" IS the vmid
    inst = _snapshot(d, "pve")
    assert inst._logs_argv(follow=False, lines=5) == [
        "pct", "exec", "200", "--", "journalctl", "-n", "5"]


def test_logs_negative_lines_rejected(tmp_path):
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    with pytest.raises(ValidationError, match="must be >= 0"):
        inst._logs_argv(follow=False, lines=-1)


def test_logs_dead_handle_raises(tmp_path):
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    inst._dead = True
    with pytest.raises(InstanceNotFoundError):
        inst.logs()


class _FakeStdout:
    """A fake text stdout: iterates the given lines, tracks close()."""

    def __init__(self, lines):
        self._iter = iter(lines)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._iter)

    def close(self):
        self.closed = True


class _FakeProc:
    """A fake Popen: scripted stdout + poll/terminate/wait/kill tracking."""

    def __init__(self, lines, *, finishes=True):
        self.stdout = _FakeStdout(lines)
        # finishes=True => the child exits on its own (poll() goes non-None
        # after wait); False => a `journalctl -f` that runs until terminated.
        self._finishes = finishes
        self._exited = False
        self.terminated = False
        self.killed = False
        self.waited = False

    def poll(self):
        return 0 if self._exited else None

    def wait(self, timeout=None):
        self.waited = True
        self._exited = True
        return 0

    def terminate(self):
        self.terminated = True
        self._exited = True

    def kill(self):
        self.killed = True
        self._exited = True


def test_logs_yields_decoded_lines_and_reaps_on_eof(tmp_path):
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    proc = _FakeProc(["line one\n", "line two\n"])
    with patch("kento._instances.subprocess.Popen", return_value=proc) as mp:
        out = list(inst.logs(lines=2))
    # Newlines stripped; finite snapshot collected via list().
    assert out == ["line one", "line two"]
    # Popen got the snapshot argv with piped stdout, text mode.
    args, kwargs = mp.call_args
    assert args[0] == [
        "lxc-attach", "-n", inst.name, "--", "journalctl", "-n", "2"]
    assert kwargs["text"] is True
    # On EOF the child is reaped and its pipe closed (no leak).
    assert proc.stdout.closed


def test_logs_decode_uses_errors_replace(tmp_path):
    # The decode is configured errors="replace" so a bad byte can't crash it.
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    proc = _FakeProc(["ok\n"])
    with patch("kento._instances.subprocess.Popen", return_value=proc) as mp:
        list(inst.logs())
    assert mp.call_args.kwargs["encoding"] == "utf-8"
    assert mp.call_args.kwargs["errors"] == "replace"


def test_logs_follow_terminates_child_on_early_close(tmp_path):
    # The follow=True (`journalctl -f`) child must NOT leak when the caller stops
    # iterating early: closing the generator terminates the child (brief JC2).
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    proc = _FakeProc(["a\n", "b\n", "c\n"], finishes=False)
    with patch("kento._instances.subprocess.Popen", return_value=proc):
        gen = inst.logs(follow=True)
        first = next(gen)        # pull one live line
        assert first == "a"
        gen.close()              # caller stops early -> GeneratorExit
    # The follower was terminated and its pipe closed.
    assert proc.terminated
    assert proc.stdout.closed
    assert not proc.killed       # it honored SIGTERM (no SIGKILL needed)


def test_logs_follow_break_out_of_loop_reaps(tmp_path):
    # A `break` out of the for-loop is the idiomatic early stop; Python turns the
    # generator GC/close into GeneratorExit -> the child is reaped.
    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")
    proc = _FakeProc(["x\n", "y\n", "z\n"], finishes=False)
    with patch("kento._instances.subprocess.Popen", return_value=proc):
        collected = []
        for line in inst.logs(follow=True):
            collected.append(line)
            if len(collected) == 2:
                break
    assert collected == ["x", "y"]
    assert proc.terminated
    assert proc.stdout.closed


def test_logs_follow_sigkill_if_term_ignored(tmp_path):
    # If the child ignores SIGTERM (wait times out), we escalate to SIGKILL so
    # the follower can never outlive the iterator.
    import subprocess as _sp

    d = _make_lxc(tmp_path)
    inst = _snapshot(d, "lxc")

    class _StubbornProc(_FakeProc):
        def __init__(self):
            super().__init__(["a\n"], finishes=False)
            self._term_waits = 0

        def terminate(self):
            self.terminated = True  # but does NOT exit

        def wait(self, timeout=None):
            if timeout is not None and not self.killed:
                # The post-terminate bounded wait times out (still running).
                raise _sp.TimeoutExpired(cmd="journalctl", timeout=timeout)
            self.waited = True
            self._exited = True
            return 0

    proc = _StubbornProc()
    with patch("kento._instances.subprocess.Popen", return_value=proc):
        gen = inst.logs(follow=True)
        next(gen)
        gen.close()
    assert proc.terminated
    assert proc.killed
    assert proc.stdout.closed


# -- M17/M18 suspend/resume: VM-only; delegate + self-update _status ----------


def test_suspend_resume_only_on_vm():
    assert hasattr(VirtualMachine, "suspend")
    assert hasattr(VirtualMachine, "resume")
    assert not hasattr(SystemContainer, "suspend")
    assert not hasattr(SystemContainer, "resume")


@pytest.mark.parametrize("mode", ["vm", "pve-vm"])
def test_suspend_delegates_and_sets_suspended(tmp_path, mode):
    d = _make_for_mode(tmp_path, mode)
    inst = _snapshot(d, mode)
    with patch("kento.suspend.suspend") as mock_suspend:
        inst.suspend()
    mock_suspend.assert_called_once_with(inst.name)
    # M17: self-update to the LITERAL SUSPENDED (not via _resolve_status, which
    # cannot see a paused VM — it reports RUNNING).
    assert inst.status is Status.SUSPENDED


@pytest.mark.parametrize("mode", ["vm", "pve-vm"])
def test_resume_delegates_and_sets_running(tmp_path, mode):
    d = _make_for_mode(tmp_path, mode)
    inst = _snapshot(d, mode)
    inst._status = Status.SUSPENDED
    with patch("kento.suspend.resume") as mock_resume:
        inst.resume()
    mock_resume.assert_called_once_with(inst.name)
    assert inst.status is Status.RUNNING


def test_suspend_does_not_update_status_on_failure(tmp_path):
    # If the runtime raises, the self-update must NOT happen (status untouched).
    from kento.errors import StateError

    d = _make_for_mode(tmp_path, "vm")
    inst = _snapshot(d, "vm")
    before = inst.status
    with patch("kento.suspend.suspend", side_effect=StateError("not running")):
        with pytest.raises(StateError):
            inst.suspend()
    assert inst.status is before


def test_suspend_dead_handle_raises(tmp_path):
    d = _make_for_mode(tmp_path, "vm")
    inst = _snapshot(d, "vm")
    inst._dead = True
    with patch("kento.suspend.suspend") as mock_suspend:
        with pytest.raises(InstanceNotFoundError):
            inst.suspend()
    mock_suspend.assert_not_called()
