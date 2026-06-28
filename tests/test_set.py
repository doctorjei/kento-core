"""Tests for the `kento set` command (scalar config mutation, stopped only)."""

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from kento.errors import ModeError, StateError, ValidationError
from kento.set_cmd import set_cmd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@contextmanager
def _env(container_dir, mode, *, running=False):
    """Patch require_root / resolve_any / is_running for set_cmd."""
    with patch("kento.set_cmd.require_root"), \
         patch("kento.set_cmd.resolve_any", return_value=(container_dir, mode)), \
         patch("kento.set_cmd.is_running", return_value=running):
        yield


def _make_pve(tmp_path, vmid, kind, conf_text):
    """Create a fake PVE tree and return (pve_dir, container_dir, conf_path).

    kind is "lxc" or "qemu-server".
    """
    pve_dir = tmp_path / "pve"
    node_dir = pve_dir / "nodes" / "testnode" / kind
    node_dir.mkdir(parents=True)
    conf_path = node_dir / f"{vmid}.conf"
    conf_path.write_text(conf_text)
    container_dir = tmp_path / str(vmid)
    container_dir.mkdir()
    (container_dir / "kento-vmid").write_text(f"{vmid}\n")
    return pve_dir, container_dir, conf_path


@contextmanager
def _pve_patch(pve_dir):
    with patch("kento.pve.PVE_DIR", pve_dir), \
         patch("kento.pve._pve_node_name", return_value="testnode"):
        yield


# ---------------------------------------------------------------------------
# FIX 1: namespace scope forwarded to resolve_any
# ---------------------------------------------------------------------------

def test_set_forwards_namespace_to_resolve_any(tmp_path):
    """`kento vm set dup ...` must scope resolution to the vm namespace so a
    duplicate name (created via --force) resolves the VM instance."""
    d = tmp_path / "dup"
    d.mkdir()
    with patch("kento.set_cmd.require_root"), \
         patch("kento.set_cmd.is_running", return_value=False), \
         patch("kento.set_cmd.resolve_any",
               return_value=(d, "vm")) as mock_resolve, \
         patch("kento.set_cmd._apply_vm"):
        rc = set_cmd("dup", memory=512, namespace="vm")
    assert rc == 0
    mock_resolve.assert_called_once_with("dup", "vm")


def test_set_default_namespace_is_none(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    with patch("kento.set_cmd.require_root"), \
         patch("kento.set_cmd.is_running", return_value=False), \
         patch("kento.set_cmd.resolve_any",
               return_value=(d, "vm")) as mock_resolve, \
         patch("kento.set_cmd._apply_vm"):
        rc = set_cmd("box", memory=512)
    assert rc == 0
    mock_resolve.assert_called_once_with("box", None)


# ---------------------------------------------------------------------------
# Guard rails: empty set, running, mac format
# ---------------------------------------------------------------------------

def test_empty_set_is_usage_error(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    with _env(d, "vm"):
        with pytest.raises(ValidationError, match="nothing to set"):
            set_cmd("box")


def test_running_instance_errors(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    with _env(d, "vm", running=True):
        with pytest.raises(StateError, match="instance is running"):
            set_cmd("box", memory=512)
    assert not (d / "kento-memory").exists()


def test_bad_mac_rejected(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    with _env(d, "vm"):
        with pytest.raises(ValidationError, match="invalid MAC address"):
            set_cmd("box", mac="zz:zz:zz:zz:zz:zz")
    assert not (d / "kento-mac").exists()


def test_nonpositive_memory_rejected(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    with _env(d, "vm"):
        with pytest.raises(ValidationError, match="--memory must be a positive integer"):
            set_cmd("box", memory=0)


# ---------------------------------------------------------------------------
# Plain VM: metadata only, no config file touched
# ---------------------------------------------------------------------------

def test_vm_writes_metadata_only(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    with _env(d, "vm"):
        rc = set_cmd("box", memory=2048, cores=4, mac="de:ad:be:ef:00:01",
                     qemu_args=["-foo", "-bar"])
    assert rc == 0
    assert (d / "kento-memory").read_text() == "2048\n"
    assert (d / "kento-cores").read_text() == "4\n"
    assert (d / "kento-mac").read_text() == "de:ad:be:ef:00:01\n"
    assert (d / "kento-qemu-args").read_text() == "-foo\n-bar\n"
    assert not (d / "config").exists()


def test_vm_set_cores_clamped_to_node_capacity(tmp_path, caplog):
    """`kento set --cores N` on a VM clamps N to the node CPU count (same rule
    as create) so we never leave an unstartable guest."""
    import logging
    d = tmp_path / "box"
    d.mkdir()
    with _env(d, "vm"), \
         patch("kento.create._host_cpu_count", return_value=4), \
         patch("kento.create._host_memory_mb", return_value=None):
        with caplog.at_level(logging.WARNING, logger="kento"):
            rc = set_cmd("box", cores=8)
    assert rc == 0
    assert (d / "kento-cores").read_text() == "4\n"
    assert any("clamping to 4" in r.getMessage() for r in caplog.records)


def test_vm_set_cores_within_capacity_not_clamped(tmp_path, caplog):
    import logging
    d = tmp_path / "box"
    d.mkdir()
    with _env(d, "vm"), \
         patch("kento.create._host_cpu_count", return_value=4), \
         patch("kento.create._host_memory_mb", return_value=None):
        with caplog.at_level(logging.WARNING, logger="kento"):
            rc = set_cmd("box", cores=2)
    assert rc == 0
    assert (d / "kento-cores").read_text() == "2\n"
    assert not any("clamping" in r.getMessage() for r in caplog.records)


def test_lxc_set_cores_never_clamped(tmp_path, caplog):
    """LXC set --cores is cgroup quota, never clamped to node capacity."""
    import logging
    d = tmp_path / "box"
    d.mkdir()
    (d / "config").write_text(_LXC_CONFIG)
    with _env(d, "lxc"), \
         patch("kento.create._host_cpu_count", return_value=1), \
         patch("kento.create._host_memory_mb", return_value=1):
        with caplog.at_level(logging.WARNING, logger="kento"):
            rc = set_cmd("box", cores=8)
    assert rc == 0
    assert (d / "kento-cores").read_text() == "8\n"
    assert not any("clamping" in r.getMessage() for r in caplog.records)


def test_vm_pve_arg_rejected(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    with _env(d, "vm"):
        with pytest.raises(ModeError):
            set_cmd("box", pve_args=["balloon: 0"])
    assert not (d / "kento-pve-args").exists()


# ---------------------------------------------------------------------------
# Plain LXC: cgroup line rewrite in native `config`; VM-only fields error
# ---------------------------------------------------------------------------

_LXC_CONFIG = (
    "lxc.uts.name = box\n"
    "lxc.rootfs.path = dir:/x/rootfs\n"
    "\n"
    "lxc.mount.auto = proc:mixed sys:ro cgroup:mixed\n"
    "lxc.tty.max = 1\n"
    "lxc.cgroup2.memory.max = 536870912\n"
    "lxc.cgroup2.cpu.max = 100000 100000\n"
)


def test_lxc_rewrites_cgroup_lines(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    (d / "config").write_text(_LXC_CONFIG)
    with _env(d, "lxc"):
        rc = set_cmd("box", memory=1024, cores=2)
    assert rc == 0
    assert (d / "kento-memory").read_text() == "1024\n"
    assert (d / "kento-cores").read_text() == "2\n"
    content = (d / "config").read_text()
    assert f"lxc.cgroup2.memory.max = {1024 * 1048576}" in content
    assert f"lxc.cgroup2.cpu.max = {2 * 100000} 100000" in content
    # Structural lines preserved.
    assert "lxc.uts.name = box" in content
    assert "lxc.rootfs.path = dir:/x/rootfs" in content


def test_lxc_appends_cgroup_line_when_absent(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    (d / "config").write_text("lxc.uts.name = box\n")
    with _env(d, "lxc"):
        assert set_cmd("box", memory=256) == 0
    content = (d / "config").read_text()
    assert f"lxc.cgroup2.memory.max = {256 * 1048576}" in content


def test_lxc_mac_rejected(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    with _env(d, "lxc"):
        with pytest.raises(ModeError):
            set_cmd("box", mac="de:ad:be:ef:00:01")
    assert not (d / "kento-mac").exists()


def test_lxc_qemu_arg_rejected(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    with _env(d, "lxc"):
        with pytest.raises(ModeError):
            set_cmd("box", qemu_args=["-foo"])


def test_lxc_pve_arg_rejected(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    with _env(d, "lxc"):
        with pytest.raises(ModeError):
            set_cmd("box", pve_args=["x: y"])


# ---------------------------------------------------------------------------
# Plain LXC: --lxc-arg pass-through block (replace / clear / skip + denylist)
# ---------------------------------------------------------------------------

def test_lxc_arg_replace(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    (d / "config").write_text(_LXC_CONFIG)
    (d / "kento-lxc-args").write_text("lxc.cap.drop = old\n")
    # Reflect the existing block in the config, as create would have appended.
    (d / "config").write_text(_LXC_CONFIG + "lxc.cap.drop = old\n")
    with _env(d, "lxc"):
        assert set_cmd("box", lxc_args=["lxc.cap.drop = sys_module"]) == 0
    content = (d / "config").read_text()
    assert "lxc.cap.drop = old" not in content
    assert "lxc.cap.drop = sys_module" in content
    assert (d / "kento-lxc-args").read_text() == "lxc.cap.drop = sys_module\n"
    # Structural lines preserved.
    assert "lxc.rootfs.path = dir:/x/rootfs" in content


def test_lxc_arg_clear(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    (d / "config").write_text(_LXC_CONFIG + "lxc.cap.drop = old\n")
    (d / "kento-lxc-args").write_text("lxc.cap.drop = old\n")
    with _env(d, "lxc"):
        assert set_cmd("box", lxc_args=[""]) == 0
    assert not (d / "kento-lxc-args").exists()
    assert "lxc.cap.drop = old" not in (d / "config").read_text()


def test_lxc_arg_skip_leaves_untouched(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    (d / "config").write_text(_LXC_CONFIG + "lxc.cap.drop = keep\n")
    (d / "kento-lxc-args").write_text("lxc.cap.drop = keep\n")
    with _env(d, "lxc"):
        # Setting only memory must not disturb the lxc-args block.
        assert set_cmd("box", memory=1024) == 0
    assert (d / "kento-lxc-args").read_text() == "lxc.cap.drop = keep\n"
    assert "lxc.cap.drop = keep" in (d / "config").read_text()


def test_lxc_arg_denied_key_rejected(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    (d / "config").write_text(_LXC_CONFIG)
    with _env(d, "lxc"):
        with pytest.raises(ValidationError, match="kento manages"):
            set_cmd("box", lxc_args=["lxc.rootfs.path = /evil"])
    assert not (d / "kento-lxc-args").exists()


def test_lxc_arg_rejected_on_vm(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    with _env(d, "vm"):
        with pytest.raises(ModeError):
            set_cmd("box", lxc_args=["lxc.cap.drop = x"])
    assert not (d / "kento-lxc-args").exists()


def test_lxc_arg_rejected_on_pve_lxc(tmp_path):
    pve_dir, d, conf = _make_pve(tmp_path, 100, "lxc", _PVE_LXC_CONF)
    with _env(d, "pve"), _pve_patch(pve_dir):
        with pytest.raises(ModeError):
            set_cmd("box", lxc_args=["lxc.cap.drop = x"])
    assert not (d / "kento-lxc-args").exists()


# ---------------------------------------------------------------------------
# PVE-LXC: surgical .conf rewrite
# ---------------------------------------------------------------------------

_PVE_LXC_CONF = (
    "arch: amd64\n"
    "ostype: unmanaged\n"
    "hostname: box\n"
    "rootfs: /x/rootfs\n"
    "net0: name=eth0,bridge=vmbr0,type=veth\n"
    "lxc.hook.pre-mount: /x/kento-hook\n"
    "lxc.mount.auto: cgroup:rw:force proc:mixed sys:mixed\n"
    "lxc.tty.max: 1\n"
    "memory: 512\n"
    "lxc.cgroup2.memory.max: 536870912\n"
    "cores: 1\n"
    "cpulimit: 1\n"
    "lxc.cgroup2.cpu.max: 100000 100000\n"
)


def test_pve_lxc_memory_rewrite(tmp_path):
    pve_dir, d, conf = _make_pve(tmp_path, 100, "lxc", _PVE_LXC_CONF)
    with _env(d, "pve"), _pve_patch(pve_dir):
        rc = set_cmd("box", memory=2048)
    assert rc == 0
    assert (d / "kento-memory").read_text() == "2048\n"
    text = conf.read_text()
    assert "memory: 2048" in text
    assert f"lxc.cgroup2.memory.max: {2048 * 1048576}" in text
    # Network/structural lines preserved.
    assert "net0: name=eth0,bridge=vmbr0,type=veth" in text
    assert "rootfs: /x/rootfs" in text
    # cores untouched.
    assert "cores: 1" in text


def test_pve_lxc_cores_rewrite(tmp_path):
    pve_dir, d, conf = _make_pve(tmp_path, 100, "lxc", _PVE_LXC_CONF)
    with _env(d, "pve"), _pve_patch(pve_dir):
        assert set_cmd("box", cores=4) == 0
    text = conf.read_text()
    assert "cores: 4" in text
    assert "cpulimit: 4" in text
    assert f"lxc.cgroup2.cpu.max: {4 * 100000} 100000" in text
    assert "memory: 512" in text


def test_pve_lxc_pve_arg_replace(tmp_path):
    pve_dir, d, conf = _make_pve(tmp_path, 100, "lxc", _PVE_LXC_CONF)
    (d / "kento-pve-args").write_text("oldkey: 1\n")
    # Reflect the existing block in the conf, as create would have appended it.
    conf.write_text(_PVE_LXC_CONF + "oldkey: 1\n")
    with _env(d, "pve"), _pve_patch(pve_dir):
        assert set_cmd("box", pve_args=["newkey: 9"]) == 0
    text = conf.read_text()
    assert "oldkey: 1" not in text
    assert "newkey: 9" in text
    assert (d / "kento-pve-args").read_text() == "newkey: 9\n"


def test_pve_lxc_pve_arg_clear(tmp_path):
    pve_dir, d, conf = _make_pve(tmp_path, 100, "lxc", _PVE_LXC_CONF + "oldkey: 1\n")
    (d / "kento-pve-args").write_text("oldkey: 1\n")
    with _env(d, "pve"), _pve_patch(pve_dir):
        assert set_cmd("box", pve_args=[""]) == 0
    assert not (d / "kento-pve-args").exists()
    assert "oldkey: 1" not in conf.read_text()


def test_pve_lxc_mac_rejected(tmp_path):
    pve_dir, d, conf = _make_pve(tmp_path, 100, "lxc", _PVE_LXC_CONF)
    with _env(d, "pve"), _pve_patch(pve_dir):
        with pytest.raises(ModeError):
            set_cmd("box", mac="de:ad:be:ef:00:01")


def test_pve_lxc_qemu_arg_rejected(tmp_path):
    pve_dir, d, conf = _make_pve(tmp_path, 100, "lxc", _PVE_LXC_CONF)
    with _env(d, "pve"), _pve_patch(pve_dir):
        with pytest.raises(ModeError):
            set_cmd("box", qemu_args=["-foo"])


# ---------------------------------------------------------------------------
# PVE-VM: qm .conf rewrite
# ---------------------------------------------------------------------------

_QM_CONF = (
    "name: box\n"
    "ostype: l26\n"
    "machine: q35\n"
    "memory: 512\n"
    "cores: 1\n"
    "hookscript: local:snippets/kento-100.sh\n"
    "serial0: socket\n"
    "args: -enable-kvm -cpu host -object memory-backend-memfd,id=mem,size=512M,share=on\n"
    "net0: virtio=DE:AD:BE:EF:00:01,bridge=vmbr0\n"
)


def test_pve_vm_memory_patches_and_resyncs_args(tmp_path):
    pve_dir, d, conf = _make_pve(tmp_path, 100, "qemu-server", _QM_CONF)
    (d / "rootfs").mkdir()
    with _env(d, "pve-vm"), _pve_patch(pve_dir):
        assert set_cmd("box", memory=4096) == 0
    text = conf.read_text()
    assert "memory: 4096" in text
    # sync_qm_args_to_memory rewrites memfd size= to match.
    assert "size=4096M" in text
    assert (d / "kento-memory").read_text() == "4096\n"


def test_pve_vm_cores_patch(tmp_path):
    pve_dir, d, conf = _make_pve(tmp_path, 100, "qemu-server", _QM_CONF)
    # Mock a large node so cores=8 isn't clamped — this test exercises the
    # conf-patch mechanism, not the capacity clamp (covered separately).
    with _env(d, "pve-vm"), _pve_patch(pve_dir), \
         patch("kento.create._host_cpu_count", return_value=64):
        assert set_cmd("box", cores=8) == 0
    assert "cores: 8" in conf.read_text()
    assert (d / "kento-cores").read_text() == "8\n"


def test_pve_vm_mac_swaps_preserving_bridge(tmp_path):
    pve_dir, d, conf = _make_pve(tmp_path, 100, "qemu-server", _QM_CONF)
    with _env(d, "pve-vm"), _pve_patch(pve_dir):
        assert set_cmd("box", mac="00:11:22:33:44:55") == 0
    text = conf.read_text()
    assert "net0: virtio=00:11:22:33:44:55,bridge=vmbr0" in text
    assert (d / "kento-mac").read_text() == "00:11:22:33:44:55\n"


def test_pve_vm_mac_no_net0_best_effort(tmp_path):
    qm = _QM_CONF.replace(
        "net0: virtio=DE:AD:BE:EF:00:01,bridge=vmbr0\n", "")
    pve_dir, d, conf = _make_pve(tmp_path, 100, "qemu-server", qm)
    with _env(d, "pve-vm"), _pve_patch(pve_dir):
        assert set_cmd("box", mac="00:11:22:33:44:55") == 0
    # Metadata still written; conf has no net0 to edit.
    assert (d / "kento-mac").read_text() == "00:11:22:33:44:55\n"
    assert "net0:" not in conf.read_text()


def test_pve_vm_qemu_arg_regenerates_args(tmp_path):
    pve_dir, d, conf = _make_pve(tmp_path, 100, "qemu-server", _QM_CONF)
    (d / "rootfs").mkdir()
    with _env(d, "pve-vm"), _pve_patch(pve_dir):
        assert set_cmd("box", qemu_args=["-device", "usb-host"]) == 0
    text = conf.read_text()
    assert (d / "kento-qemu-args").read_text() == "-device\nusb-host\n"
    # The pass-through entries land in the regenerated args: line.
    assert "-device" in text
    assert "usb-host" in text
    # Memfd size matches the existing memory.
    assert "size=512M" in text


def test_pve_vm_pve_arg_replace(tmp_path):
    pve_dir, d, conf = _make_pve(tmp_path, 100, "qemu-server",
                                 _QM_CONF + "oldkey: 1\n")
    (d / "kento-pve-args").write_text("oldkey: 1\n")
    with _env(d, "pve-vm"), _pve_patch(pve_dir):
        assert set_cmd("box", pve_args=["balloon: 0"]) == 0
    text = conf.read_text()
    assert "oldkey: 1" not in text
    assert "balloon: 0" in text


def test_pve_vm_mac_non_virtio_net0_warns(tmp_path, caplog):
    """F: a net0 that exists but has no 'virtio' token (e.g. a user-edited
    non-virtio model) can't take the MAC. set_cmd must surface a WARNING log
    so "Updated" isn't misleading, while STILL writing kento-mac
    metadata and returning success."""
    import logging
    qm = _QM_CONF.replace(
        "net0: virtio=DE:AD:BE:EF:00:01,bridge=vmbr0\n",
        "net0: e1000=DE:AD:BE:EF:00:01,bridge=vmbr0\n")
    pve_dir, d, conf = _make_pve(tmp_path, 100, "qemu-server", qm)
    with _env(d, "pve-vm"), _pve_patch(pve_dir):
        with caplog.at_level(logging.WARNING, logger="kento"):
            rc = set_cmd("box", mac="00:11:22:33:44:55")
    assert rc == 0
    warning_text = " ".join(caplog.messages)
    assert "virtio" in warning_text.lower()
    assert "could not be applied" in warning_text.lower()
    # Metadata IS written even though the conf NIC was left alone.
    assert (d / "kento-mac").read_text() == "00:11:22:33:44:55\n"
    # The non-virtio net0 line is preserved unchanged (no MAC swapped in).
    assert "net0: e1000=DE:AD:BE:EF:00:01,bridge=vmbr0" in conf.read_text()


# ---------------------------------------------------------------------------
# Denylist parity with create.py: --qemu-arg / --pve-arg denylisted tokens
# are rejected (exit 1, no metadata written), even in their valid mode.
# ---------------------------------------------------------------------------

def test_vm_qemu_arg_denylisted_token_rejected(tmp_path):
    """A denylisted --qemu-arg token (-kernel) in vm mode is rejected with
    ValidationError and no metadata file is written (create/set parity)."""
    d = tmp_path / "box"
    d.mkdir()
    with _env(d, "vm"):
        with pytest.raises(ValidationError, match="kento manages"):
            set_cmd("box", qemu_args=["-kernel", "/x"])
    assert not (d / "kento-qemu-args").exists()


def test_vm_qemu_arg_denylisted_memfd_rejected(tmp_path):
    """A memfd-size token in a --qemu-arg is denylisted (would collide with
    kento's memory-backend-memfd size=)."""
    d = tmp_path / "box"
    d.mkdir()
    with _env(d, "vm"):
        with pytest.raises(ValidationError, match="kento manages"):
            set_cmd("box", qemu_args=["memfd-size=2048"])
    assert not (d / "kento-qemu-args").exists()


def test_vm_qemu_arg_benign_succeeds(tmp_path):
    """Counter-test: a benign --qemu-arg in vm mode still succeeds."""
    d = tmp_path / "box"
    d.mkdir()
    with _env(d, "vm"):
        assert set_cmd("box", qemu_args=["-device", "virtio-rng-pci"]) == 0
    assert (d / "kento-qemu-args").read_text() == "-device\nvirtio-rng-pci\n"


def test_pve_vm_qemu_arg_denylisted_rejected(tmp_path):
    """Denylist also fires in pve-vm mode for --qemu-arg, leaving no
    metadata behind."""
    pve_dir, d, conf = _make_pve(tmp_path, 100, "qemu-server", _QM_CONF)
    with _env(d, "pve-vm"), _pve_patch(pve_dir):
        with pytest.raises(ValidationError, match="kento manages"):
            set_cmd("box", qemu_args=["-kernel", "/x"])
    assert not (d / "kento-qemu-args").exists()


def test_pve_lxc_pve_arg_denylisted_rootfs_rejected(tmp_path):
    """A denylisted --pve-arg token (rootfs:) in pve (pve-lxc) mode is
    rejected with ValidationError and no metadata file written."""
    pve_dir, d, conf = _make_pve(tmp_path, 100, "lxc", _PVE_LXC_CONF)
    with _env(d, "pve"), _pve_patch(pve_dir):
        with pytest.raises(ValidationError, match="kento manages"):
            set_cmd("box", pve_args=["rootfs: /evil"])
    assert not (d / "kento-pve-args").exists()


def test_pve_lxc_pve_arg_denylisted_arch_rejected(tmp_path):
    pve_dir, d, conf = _make_pve(tmp_path, 100, "lxc", _PVE_LXC_CONF)
    with _env(d, "pve"), _pve_patch(pve_dir):
        with pytest.raises(ValidationError, match="kento manages"):
            set_cmd("box", pve_args=["arch: x"])
    assert not (d / "kento-pve-args").exists()


def test_pve_vm_pve_arg_denylisted_hostname_rejected(tmp_path):
    pve_dir, d, conf = _make_pve(tmp_path, 100, "qemu-server", _QM_CONF)
    with _env(d, "pve-vm"), _pve_patch(pve_dir):
        with pytest.raises(ValidationError, match="kento manages"):
            set_cmd("box", pve_args=["hostname: y"])
    assert not (d / "kento-pve-args").exists()


def test_pve_lxc_pve_arg_benign_succeeds(tmp_path):
    """Counter-test: a benign --pve-arg in pve mode still succeeds."""
    pve_dir, d, conf = _make_pve(tmp_path, 100, "lxc", _PVE_LXC_CONF)
    with _env(d, "pve"), _pve_patch(pve_dir):
        assert set_cmd("box", pve_args=["tags: kento-test"]) == 0
    assert (d / "kento-pve-args").read_text() == "tags: kento-test\n"


# ---------------------------------------------------------------------------
# List semantics (qemu_args / pve_args)
# ---------------------------------------------------------------------------

def test_qemu_arg_none_leaves_untouched(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    (d / "kento-qemu-args").write_text("-keep\n")
    with _env(d, "vm"):
        assert set_cmd("box", memory=512) == 0
    assert (d / "kento-qemu-args").read_text() == "-keep\n"


def test_qemu_arg_empty_clears(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    (d / "kento-qemu-args").write_text("-gone\n")
    with _env(d, "vm"):
        assert set_cmd("box", qemu_args=[""]) == 0
    assert not (d / "kento-qemu-args").exists()


def test_qemu_arg_replace(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    (d / "kento-qemu-args").write_text("-old\n")
    with _env(d, "vm"):
        assert set_cmd("box", qemu_args=["-a", "-b"]) == 0
    assert (d / "kento-qemu-args").read_text() == "-a\n-b\n"


# ===========================================================================
# Network rewrite matrix (v1.6.0 `kento set` net fields)
# ===========================================================================

from kento.set_cmd import _resolve_net_identity


def _state(container_dir):
    """Create a kento-state dir with upper/ and return the state dir path."""
    state = container_dir / "state"
    (state / "upper").mkdir(parents=True)
    (container_dir / "kento-state").write_text(str(state) + "\n")
    return state


def _net_static_file(state):
    return (state / "upper" / "etc" / "systemd" / "network"
            / "05-kento-static.network")


def _resolved_file(state):
    return (state / "upper" / "etc" / "systemd" / "resolved.conf.d"
            / "90-kento.conf")


# ---------------------------------------------------------------------------
# _resolve_net_identity
# ---------------------------------------------------------------------------

def test_resolve_identity_from_metadata(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    (d / "kento-name").write_text("box\n")
    (d / "kento-net-type").write_text("bridge\n")
    (d / "kento-bridge").write_text("vmbr0\n")
    (d / "kento-net").write_text(
        "ip=192.168.0.10/24\ngateway=192.168.0.1\ndns=1.1.1.1\n"
        "searchdomain=lan\n")
    (d / "kento-port").write_text("10022:22\n")
    (d / "kento-hostname").write_text("myhost\n")
    ident = _resolve_net_identity(d, "lxc")
    assert ident["type"] == "bridge"
    assert ident["bridge"] == "vmbr0"
    assert ident["ip"] == "192.168.0.10/24"
    assert ident["gateway"] == "192.168.0.1"
    assert ident["dns"] == "1.1.1.1"
    assert ident["searchdomain"] == "lan"
    assert ident["port"] == "10022:22"
    assert ident["hostname"] == "myhost"


def test_resolve_identity_hostname_falls_back_to_name(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    (d / "kento-name").write_text("box\n")
    (d / "kento-net-type").write_text("none\n")
    ident = _resolve_net_identity(d, "lxc")
    assert ident["hostname"] == "box"
    assert ident["type"] == "none"


def test_resolve_identity_pre_n1_lxc_parseback(tmp_path):
    """No kento-net-type: derive from the native config."""
    d = tmp_path / "box"
    d.mkdir()
    (d / "kento-name").write_text("box\n")
    (d / "config").write_text(
        "lxc.uts.name = box\n"
        "lxc.net.0.type = veth\n"
        "lxc.net.0.link = lxcbr0\n"
        "lxc.net.0.flags = up\n"
        "lxc.net.0.ipv4.address = 10.0.0.5/24\n"
        "lxc.net.0.ipv4.gateway = 10.0.0.1\n"
    )
    ident = _resolve_net_identity(d, "lxc")
    assert ident["type"] == "bridge"
    assert ident["bridge"] == "lxcbr0"
    assert ident["ip"] == "10.0.0.5/24"
    assert ident["gateway"] == "10.0.0.1"


def test_resolve_identity_pre_n1_lxc_host(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    (d / "kento-name").write_text("box\n")
    (d / "config").write_text(
        "lxc.uts.name = box\nlxc.net.0.type = none\n")
    ident = _resolve_net_identity(d, "lxc")
    assert ident["type"] == "host"


def test_resolve_identity_pre_n1_pve_lxc_parseback(tmp_path):
    pve_dir, d, conf = _make_pve(tmp_path, 100, "lxc", _PVE_LXC_CONF)
    (d / "kento-name").write_text("box\n")
    with _pve_patch(pve_dir):
        ident = _resolve_net_identity(d, "pve")
    assert ident["type"] == "bridge"
    assert ident["bridge"] == "vmbr0"


# ---------------------------------------------------------------------------
# Plain LXC net rewrites
# ---------------------------------------------------------------------------

_LXC_BRIDGE_CONFIG = (
    "lxc.uts.name = box\n"
    "lxc.rootfs.path = dir:/x/rootfs\n"
    "\n"
    "lxc.net.0.type = veth\n"
    "lxc.net.0.link = vmbr0\n"
    "lxc.net.0.flags = up\n"
    "\n"
    "lxc.mount.auto = proc:mixed sys:ro cgroup:mixed\n"
    "lxc.tty.max = 1\n"
)


def _lxc_box(tmp_path, config=_LXC_BRIDGE_CONFIG, **meta):
    d = tmp_path / "box"
    d.mkdir()
    (d / "config").write_text(config)
    (d / "kento-name").write_text("box\n")
    (d / "kento-net-type").write_text(meta.get("net_type", "bridge") + "\n")
    if meta.get("bridge"):
        (d / "kento-bridge").write_text(meta["bridge"] + "\n")
    _state(d)
    return d


def test_lxc_set_static_ip_rewrites_config_and_injects(tmp_path):
    d = _lxc_box(tmp_path, bridge="vmbr0")
    with _env(d, "lxc"):
        assert set_cmd("box", ip="192.168.0.50/24", gateway="192.168.0.1") == 0
    cfg = (d / "config").read_text()
    assert "lxc.net.0.ipv4.address = 192.168.0.50/24" in cfg
    assert "lxc.net.0.ipv4.gateway = 192.168.0.1" in cfg
    state = d / "state"
    net = _net_static_file(state).read_text()
    assert "Address=192.168.0.50/24" in net
    assert "Gateway=192.168.0.1" in net
    netmeta = (d / "kento-net").read_text()
    assert "ip=192.168.0.50/24" in netmeta
    assert "gateway=192.168.0.1" in netmeta


def test_lxc_set_ip_dhcp_clears_static(tmp_path):
    d = _lxc_box(tmp_path, bridge="vmbr0")
    # Seed a prior static config.
    (d / "kento-net").write_text("ip=1.2.3.4/24\ngateway=1.2.3.1\n")
    _net_static_file(d / "state").parent.mkdir(parents=True, exist_ok=True)
    _net_static_file(d / "state").write_text("[Match]\nName=eth0\n")
    with _env(d, "lxc"):
        assert set_cmd("box", ip="dhcp") == 0
    cfg = (d / "config").read_text()
    assert "ipv4.address" not in cfg
    assert not _net_static_file(d / "state").exists()
    # kento-net no longer carries an ip= line.
    if (d / "kento-net").is_file():
        assert "ip=" not in (d / "kento-net").read_text()


def test_lxc_set_ip_without_bridge_rejected(tmp_path):
    d = _lxc_box(tmp_path, net_type="host")
    with _env(d, "lxc"):
        with pytest.raises(ValidationError, match="requires bridge"):
            set_cmd("box", ip="192.168.0.50/24")


def test_lxc_set_network_host_removes_net_lines(tmp_path):
    d = _lxc_box(tmp_path, bridge="vmbr0")
    with _env(d, "lxc"):
        assert set_cmd("box", network="host") == 0
    cfg = (d / "config").read_text()
    assert "lxc.net.0.type = none" in cfg
    assert "lxc.net.0.link" not in cfg
    assert "lxc.net.0.flags" not in cfg
    assert (d / "kento-net-type").read_text().strip() == "host"


def test_lxc_set_network_none_removes_all_net_lines(tmp_path):
    d = _lxc_box(tmp_path, bridge="vmbr0")
    with _env(d, "lxc"):
        assert set_cmd("box", network="none") == 0
    cfg = (d / "config").read_text()
    assert "lxc.net.0" not in cfg
    assert (d / "kento-net-type").read_text().strip() == "none"


def test_lxc_set_bridge_change_rewrites_link(tmp_path):
    d = _lxc_box(tmp_path, bridge="vmbr0")
    with _env(d, "lxc"):
        assert set_cmd("box", network="bridge=br1") == 0
    cfg = (d / "config").read_text()
    assert "lxc.net.0.link = br1" in cfg
    assert (d / "kento-bridge").read_text().strip() == "br1"


def test_lxc_set_usermode_rejected(tmp_path):
    d = _lxc_box(tmp_path, bridge="vmbr0")
    with _env(d, "lxc"):
        with pytest.raises(ModeError, match="usermode"):
            set_cmd("box", network="usermode")


def test_lxc_set_dns_only_injects_resolved(tmp_path):
    d = _lxc_box(tmp_path, bridge="vmbr0")
    with _env(d, "lxc"):
        assert set_cmd("box", dns="8.8.8.8") == 0
    resolved = _resolved_file(d / "state").read_text()
    assert "DNS=8.8.8.8" in resolved
    assert "dns=8.8.8.8" in (d / "kento-net").read_text()


def test_lxc_set_hostname_injects_and_persists(tmp_path):
    d = _lxc_box(tmp_path, bridge="vmbr0")
    with _env(d, "lxc"):
        assert set_cmd("box", hostname="newname") == 0
    assert (d / "state" / "upper" / "etc" / "hostname").read_text() == "newname\n"
    assert (d / "kento-hostname").read_text().strip() == "newname"


def test_lxc_set_port_rewrites_kento_port(tmp_path):
    d = _lxc_box(tmp_path, bridge="vmbr0")
    with _env(d, "lxc"):
        assert set_cmd("box", port=["10022:22"]) == 0
    assert (d / "kento-port").read_text().strip() == "10022:22"


def test_lxc_set_port_clear(tmp_path):
    d = _lxc_box(tmp_path, bridge="vmbr0")
    (d / "kento-port").write_text("10022:22\n")
    with _env(d, "lxc"):
        assert set_cmd("box", port=[""]) == 0
    assert not (d / "kento-port").exists()


def test_lxc_set_port_bad_form_rejected(tmp_path):
    d = _lxc_box(tmp_path, bridge="vmbr0")
    with _env(d, "lxc"):
        # Validation now runs through the §5.7A boundary parser (parse_forwards):
        # a bare token has too few colon-separated elements.
        with pytest.raises(ValidationError, match="port-forward spec"):
            set_cmd("box", port=["notaport"])


def test_lxc_set_port_replaces_with_n(tmp_path):
    """set --port a --port b --port c/udp is a declarative full-set replace."""
    d = _lxc_box(tmp_path, bridge="vmbr0")
    (d / "kento-port").write_text("9999:99\n")  # prior single forward
    with _env(d, "lxc"):
        assert set_cmd("box", port=["8080:80", "8443:443", "5353:53/udp"]) == 0
    assert (d / "kento-port").read_text().splitlines() == [
        "8080:80", "8443:443", "5353:53/udp"]


def test_lxc_set_port_dedup_rejected(tmp_path):
    """A duplicate (proto, host_port) in the replace set is a clear error."""
    d = _lxc_box(tmp_path, bridge="vmbr0")
    with _env(d, "lxc"):
        with pytest.raises(ValidationError, match="duplicate"):
            set_cmd("box", port=["8080:80", "8080:90"])


def test_lxc_set_port_clears_n(tmp_path):
    """--port '' clears the whole set even when N forwards were present."""
    d = _lxc_box(tmp_path, bridge="vmbr0")
    (d / "kento-port").write_text("8080:80\n8443:443\n5353:53/udp\n")
    with _env(d, "lxc"):
        assert set_cmd("box", port=[""]) == 0
    assert not (d / "kento-port").exists()


def test_lxc_set_port_address_form_raises(tmp_path):
    """A 3/4-element address form raises at the boundary (1.0 never writes)."""
    from kento._network import ForwardAddressNotImplemented
    d = _lxc_box(tmp_path, bridge="vmbr0")
    with _env(d, "lxc"):
        with pytest.raises(ForwardAddressNotImplemented):
            set_cmd("box", port=["127.0.0.1:8080:80"])


def test_lxc_set_port_on_host_type_rejected(tmp_path):
    d = _lxc_box(tmp_path, net_type="host")
    with _env(d, "lxc"):
        with pytest.raises(ModeError, match="port"):
            set_cmd("box", port=["10022:22"])


def test_lxc_set_partial_preserves_untouched(tmp_path):
    """Setting only dns must not disturb the existing bridge/ip identity."""
    d = _lxc_box(tmp_path, bridge="vmbr0")
    (d / "kento-net").write_text("ip=10.0.0.9/24\ngateway=10.0.0.1\n")
    with _env(d, "lxc"):
        assert set_cmd("box", dns="9.9.9.9") == 0
    netmeta = (d / "kento-net").read_text()
    assert "ip=10.0.0.9/24" in netmeta
    assert "gateway=10.0.0.1" in netmeta
    assert "dns=9.9.9.9" in netmeta
    # The native LXC `config` (the actual boot config) must STILL carry the
    # preserved static IP — a dns-only set must not blank the NIC address.
    cfg = (d / "config").read_text()
    assert "lxc.net.0.ipv4.address = 10.0.0.9/24" in cfg
    assert "lxc.net.0.link = vmbr0" in cfg
    # Static network file still has the IP + new DNS.
    net = _net_static_file(d / "state").read_text()
    assert "Address=10.0.0.9/24" in net
    assert "DNS=9.9.9.9" in net


# ---------------------------------------------------------------------------
# PVE-LXC net rewrites
# ---------------------------------------------------------------------------

def _pve_lxc_box(tmp_path, conf=_PVE_LXC_CONF, **meta):
    pve_dir, d, conf_path = _make_pve(tmp_path, 100, "lxc", conf)
    (d / "kento-name").write_text("box\n")
    (d / "kento-net-type").write_text(meta.get("net_type", "bridge") + "\n")
    if meta.get("bridge", "vmbr0"):
        (d / "kento-bridge").write_text(meta.get("bridge", "vmbr0") + "\n")
    _state(d)
    return pve_dir, d, conf_path


def test_pve_lxc_set_static_ip_rewrites_net0(tmp_path):
    pve_dir, d, conf = _pve_lxc_box(tmp_path)
    with _env(d, "pve"), _pve_patch(pve_dir):
        assert set_cmd("box", ip="192.168.0.60/24", gateway="192.168.0.1") == 0
    text = conf.read_text()
    # Assert the FULL create-compatible net0 line — name=eth0 and type=veth are
    # load-bearing (without name=eth0 PVE won't assign eth0 in the guest).
    assert ("net0: name=eth0,bridge=vmbr0,ip=192.168.0.60/24,"
            "gw=192.168.0.1,type=veth" in text)
    # Guest drop-in injected too.
    net = _net_static_file(d / "state").read_text()
    assert "Address=192.168.0.60/24" in net


def test_pve_lxc_set_network_host(tmp_path):
    pve_dir, d, conf = _pve_lxc_box(tmp_path)
    with _env(d, "pve"), _pve_patch(pve_dir):
        assert set_cmd("box", network="host") == 0
    text = conf.read_text()
    assert "lxc.net.0.type: none" in text
    assert "net0:" not in text
    assert (d / "kento-net-type").read_text().strip() == "host"


def test_pve_lxc_set_dns(tmp_path):
    pve_dir, d, conf = _pve_lxc_box(tmp_path)
    with _env(d, "pve"), _pve_patch(pve_dir):
        assert set_cmd("box", dns="8.8.4.4") == 0
    assert "nameserver: 8.8.4.4" in conf.read_text()


def test_pve_lxc_set_hostname(tmp_path):
    pve_dir, d, conf = _pve_lxc_box(tmp_path)
    with _env(d, "pve"), _pve_patch(pve_dir):
        assert set_cmd("box", hostname="newhost") == 0
    assert "hostname: newhost" in conf.read_text()
    assert (d / "kento-hostname").read_text().strip() == "newhost"


def test_pve_lxc_set_port(tmp_path):
    pve_dir, d, conf = _pve_lxc_box(tmp_path)
    with _env(d, "pve"), _pve_patch(pve_dir):
        assert set_cmd("box", port=["10080:80"]) == 0
    assert (d / "kento-port").read_text().strip() == "10080:80"


# ---------------------------------------------------------------------------
# PVE-VM net rewrites
# ---------------------------------------------------------------------------

_QM_USERMODE_CONF = (
    "name: box\n"
    "ostype: l26\n"
    "machine: q35\n"
    "memory: 512\n"
    "cores: 1\n"
    "hookscript: local:snippets/kento-100.sh\n"
    "serial0: socket\n"
    "args: -enable-kvm -cpu host -object memory-backend-memfd,id=mem,size=512M,share=on\n"
)


def _pve_vm_box(tmp_path, conf=_QM_CONF, **meta):
    pve_dir, d, conf_path = _make_pve(tmp_path, 100, "qemu-server", conf)
    (d / "kento-name").write_text("box\n")
    (d / "rootfs").mkdir()
    (d / "kento-mac").write_text("DE:AD:BE:EF:00:01\n")
    (d / "kento-net-type").write_text(meta.get("net_type", "bridge") + "\n")
    if meta.get("bridge"):
        (d / "kento-bridge").write_text(meta["bridge"] + "\n")
    _state(d)
    return pve_dir, d, conf_path


def test_pve_vm_set_port_usermode(tmp_path):
    pve_dir, d, conf = _pve_vm_box(tmp_path, conf=_QM_USERMODE_CONF,
                                   net_type="usermode")
    with _env(d, "pve-vm"), _pve_patch(pve_dir):
        assert set_cmd("box", port=["10022:22"]) == 0
    assert (d / "kento-port").read_text().strip() == "10022:22"
    text = conf.read_text()
    assert "hostfwd=tcp:127.0.0.1:10022-:22" in text


def test_pve_vm_set_bridge_rewrites_net0_preserving_mac(tmp_path):
    pve_dir, d, conf = _pve_vm_box(tmp_path, conf=_QM_USERMODE_CONF,
                                   net_type="usermode")
    with _env(d, "pve-vm"), _pve_patch(pve_dir):
        assert set_cmd("box", network="bridge=vmbr0") == 0
    text = conf.read_text()
    assert "net0: virtio=DE:AD:BE:EF:00:01,bridge=vmbr0" in text
    assert (d / "kento-net-type").read_text().strip() == "bridge"


def test_pve_vm_switch_usermode_to_bridge_drops_slirp(tmp_path):
    """Regression: usermode->bridge must regenerate args: WITHOUT the slirp NIC
    and drop kento-port, else qm gets two NICs fighting over id=net0 (broken
    boot)."""
    # Seed a usermode instance whose args: already carries the slirp netdev
    # (as generate_qm_args would have produced) plus a kento-port.
    slirp_conf = (
        "name: box\nostype: l26\nmachine: q35\nmemory: 512\ncores: 1\n"
        "hookscript: local:snippets/kento-100.sh\nserial0: socket\n"
        "args: -enable-kvm -cpu host "
        "-object memory-backend-memfd,id=mem,size=512M,share=on "
        "-netdev user,id=net0,hostfwd=tcp:127.0.0.1:10022-:22 "
        "-device virtio-net-pci,netdev=net0,mac=DE:AD:BE:EF:00:01\n"
    )
    pve_dir, d, conf = _pve_vm_box(tmp_path, conf=slirp_conf,
                                   net_type="usermode")
    (d / "kento-port").write_text("10022:22\n")
    with _env(d, "pve-vm"), _pve_patch(pve_dir):
        assert set_cmd("box", network="bridge=vmbr0") == 0
    text = conf.read_text()
    assert "net0: virtio=DE:AD:BE:EF:00:01,bridge=vmbr0" in text
    # The slirp NIC must be gone from args: and kento-port removed.
    assert "hostfwd" not in text
    assert "-netdev user" not in text
    assert not (d / "kento-port").exists()
    assert (d / "kento-net-type").read_text().strip() == "bridge"


def test_pve_vm_switch_bridge_to_usermode_adds_slirp(tmp_path):
    """Regression (inverse): bridge->usermode with --port must remove net0 and
    emit the slirp hostfwd in args:."""
    pve_dir, d, conf = _pve_vm_box(tmp_path, net_type="bridge", bridge="vmbr0")
    with _env(d, "pve-vm"), _pve_patch(pve_dir):
        assert set_cmd("box", network="usermode", port=["10022:22"]) == 0
    text = conf.read_text()
    assert "net0:" not in text
    assert "hostfwd=tcp:127.0.0.1:10022-:22" in text
    assert (d / "kento-port").read_text().strip() == "10022:22"
    assert (d / "kento-net-type").read_text().strip() == "usermode"


def test_pve_vm_set_static_ip_bridge_injects_guest_dropin(tmp_path):
    pve_dir, d, conf = _pve_vm_box(tmp_path, net_type="bridge", bridge="vmbr0")
    with _env(d, "pve-vm"), _pve_patch(pve_dir):
        assert set_cmd("box", ip="192.168.0.70/24", gateway="192.168.0.1") == 0
    net = _net_static_file(d / "state").read_text()
    assert "Type=ether" in net
    assert "Address=192.168.0.70/24" in net
    # No qm ip field — bridge ip is guest-configured.
    assert "ip=192.168.0.70" not in conf.read_text()


def test_pve_vm_set_dns_hostname_guest_dropins(tmp_path):
    pve_dir, d, conf = _pve_vm_box(tmp_path, conf=_QM_USERMODE_CONF,
                                   net_type="usermode")
    with _env(d, "pve-vm"), _pve_patch(pve_dir):
        assert set_cmd("box", dns="8.8.8.8", hostname="vmhost") == 0
    assert "DNS=8.8.8.8" in _resolved_file(d / "state").read_text()
    assert (d / "state" / "upper" / "etc" / "hostname").read_text() == "vmhost\n"


def test_pve_vm_set_host_rejected(tmp_path):
    pve_dir, d, conf = _pve_vm_box(tmp_path, conf=_QM_USERMODE_CONF,
                                   net_type="usermode")
    with _env(d, "pve-vm"), _pve_patch(pve_dir):
        with pytest.raises(ModeError, match="host"):
            set_cmd("box", network="host")


# ---------------------------------------------------------------------------
# Plain VM net rewrites
# ---------------------------------------------------------------------------

def _plain_vm_box(tmp_path, **meta):
    d = tmp_path / "box"
    d.mkdir()
    (d / "kento-name").write_text("box\n")
    (d / "kento-net-type").write_text(meta.get("net_type", "usermode") + "\n")
    _state(d)
    return d


def test_plain_vm_set_port(tmp_path):
    d = _plain_vm_box(tmp_path)
    with _env(d, "vm"):
        assert set_cmd("box", port=["10022:22"]) == 0
    assert (d / "kento-port").read_text().strip() == "10022:22"


def test_plain_vm_set_bridge_rejected(tmp_path):
    d = _plain_vm_box(tmp_path)
    with _env(d, "vm"):
        with pytest.raises(ModeError, match="bridge"):
            set_cmd("box", network="bridge=vmbr0")


def test_plain_vm_set_host_rejected(tmp_path):
    d = _plain_vm_box(tmp_path)
    with _env(d, "vm"):
        with pytest.raises(ModeError, match="host"):
            set_cmd("box", network="host")


def test_plain_vm_set_ip_rejected(tmp_path):
    d = _plain_vm_box(tmp_path)
    with _env(d, "vm"):
        with pytest.raises(ValidationError, match="requires bridge"):
            set_cmd("box", ip="10.0.0.5/24")


def test_plain_vm_set_dns_hostname_guest_dropins(tmp_path):
    d = _plain_vm_box(tmp_path)
    with _env(d, "vm"):
        assert set_cmd("box", dns="1.1.1.1", hostname="vm1") == 0
    assert "DNS=1.1.1.1" in _resolved_file(d / "state").read_text()
    assert (d / "state" / "upper" / "etc" / "hostname").read_text() == "vm1\n"


def test_plain_vm_set_network_none(tmp_path):
    d = _plain_vm_box(tmp_path)
    with _env(d, "vm"):
        assert set_cmd("box", network="none") == 0
    assert (d / "kento-net-type").read_text().strip() == "none"


# ---------------------------------------------------------------------------
# Cross-cutting: gateway without ip, running, and net signature reachable
# ---------------------------------------------------------------------------

def test_set_gateway_without_static_ip_rejected(tmp_path):
    d = _lxc_box(tmp_path, bridge="vmbr0")
    with _env(d, "lxc"):
        with pytest.raises(ValidationError, match="gateway"):
            set_cmd("box", gateway="192.168.0.1", ip="dhcp")


def test_set_net_field_running_errors(tmp_path):
    d = _lxc_box(tmp_path, bridge="vmbr0")
    with _env(d, "lxc", running=True):
        with pytest.raises(StateError, match="instance is running"):
            set_cmd("box", dns="8.8.8.8")
