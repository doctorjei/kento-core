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
    inst.status = Status.RUNNING
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
