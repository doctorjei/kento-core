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
