"""Tests for the `kento set` command (scalar config mutation, stopped only)."""

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

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
# Guard rails: empty set, running, mac format
# ---------------------------------------------------------------------------

def test_empty_set_is_usage_error(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    with _env(d, "vm"):
        assert set_cmd("box") == 1


def test_running_instance_errors(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    with _env(d, "vm", running=True):
        assert set_cmd("box", memory=512) == 1
    assert not (d / "kento-memory").exists()


def test_bad_mac_rejected(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    with _env(d, "vm"):
        assert set_cmd("box", mac="zz:zz:zz:zz:zz:zz") == 1
    assert not (d / "kento-mac").exists()


def test_nonpositive_memory_rejected(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    with _env(d, "vm"):
        assert set_cmd("box", memory=0) == 1


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


def test_vm_pve_arg_rejected(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    with _env(d, "vm"):
        assert set_cmd("box", pve_args=["balloon: 0"]) == 1
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
        assert set_cmd("box", mac="de:ad:be:ef:00:01") == 1
    assert not (d / "kento-mac").exists()


def test_lxc_qemu_arg_rejected(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    with _env(d, "lxc"):
        assert set_cmd("box", qemu_args=["-foo"]) == 1


def test_lxc_pve_arg_rejected(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    with _env(d, "lxc"):
        assert set_cmd("box", pve_args=["x: y"]) == 1


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
        assert set_cmd("box", lxc_args=["lxc.rootfs.path = /evil"]) == 1
    assert not (d / "kento-lxc-args").exists()


def test_lxc_arg_rejected_on_vm(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    with _env(d, "vm"):
        assert set_cmd("box", lxc_args=["lxc.cap.drop = x"]) == 1
    assert not (d / "kento-lxc-args").exists()


def test_lxc_arg_rejected_on_pve_lxc(tmp_path):
    pve_dir, d, conf = _make_pve(tmp_path, 100, "lxc", _PVE_LXC_CONF)
    with _env(d, "pve"), _pve_patch(pve_dir):
        assert set_cmd("box", lxc_args=["lxc.cap.drop = x"]) == 1
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
        assert set_cmd("box", mac="de:ad:be:ef:00:01") == 1


def test_pve_lxc_qemu_arg_rejected(tmp_path):
    pve_dir, d, conf = _make_pve(tmp_path, 100, "lxc", _PVE_LXC_CONF)
    with _env(d, "pve"), _pve_patch(pve_dir):
        assert set_cmd("box", qemu_args=["-foo"]) == 1


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
    with _env(d, "pve-vm"), _pve_patch(pve_dir):
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
