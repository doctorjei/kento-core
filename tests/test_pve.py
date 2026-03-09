"""Tests for PVE integration."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kento.pve import (
    is_pve, _used_vmids, next_vmid, validate_vmid,
    generate_pve_config, write_pve_config, delete_pve_config,
)
from kento import detect_mode


class TestIsPve:
    def test_true_when_pve_dir_exists(self, tmp_path):
        pve_dir = tmp_path / "pve"
        pve_dir.mkdir()
        with patch("kento.pve.PVE_DIR", pve_dir):
            assert is_pve() is True

    def test_false_when_no_pve_dir(self, tmp_path):
        with patch("kento.pve.PVE_DIR", tmp_path / "nope"):
            assert is_pve() is False


class TestUsedVmids:
    def test_from_vmlist(self, tmp_path):
        pve = tmp_path / "pve"
        pve.mkdir()
        vmlist = pve / ".vmlist"
        vmlist.write_text(json.dumps({"ids": {"100": {}, "105": {}, "200": {}}}))
        with patch("kento.pve.PVE_DIR", pve):
            assert _used_vmids() == {100, 105, 200}

    def test_fallback_scan(self, tmp_path):
        pve = tmp_path / "pve"
        pve.mkdir()
        lxc = pve / "lxc"
        lxc.mkdir()
        qemu = pve / "qemu-server"
        qemu.mkdir()
        (lxc / "100.conf").write_text("")
        (lxc / "101.conf").write_text("")
        (qemu / "200.conf").write_text("")
        with patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve.PVE_LXC_DIR", lxc), \
             patch("kento.pve.PVE_QEMU_DIR", qemu):
            assert _used_vmids() == {100, 101, 200}

    def test_empty(self, tmp_path):
        pve = tmp_path / "pve"
        pve.mkdir()
        with patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve.PVE_LXC_DIR", pve / "lxc"), \
             patch("kento.pve.PVE_QEMU_DIR", pve / "qemu-server"):
            assert _used_vmids() == set()

    def test_vmlist_with_gaps(self, tmp_path):
        pve = tmp_path / "pve"
        pve.mkdir()
        vmlist = pve / ".vmlist"
        vmlist.write_text(json.dumps({"ids": {"100": {}, "102": {}}}))
        with patch("kento.pve.PVE_DIR", pve):
            assert _used_vmids() == {100, 102}

    def test_corrupt_vmlist_falls_back(self, tmp_path):
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text("not json")
        lxc = pve / "lxc"
        lxc.mkdir()
        (lxc / "100.conf").write_text("")
        with patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve.PVE_LXC_DIR", lxc), \
             patch("kento.pve.PVE_QEMU_DIR", pve / "qemu-server"):
            assert _used_vmids() == {100}


class TestNextVmid:
    def test_empty_returns_100(self, tmp_path):
        pve = tmp_path / "pve"
        pve.mkdir()
        with patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve.PVE_LXC_DIR", pve / "lxc"), \
             patch("kento.pve.PVE_QEMU_DIR", pve / "qemu-server"):
            assert next_vmid() == 100

    def test_fills_gaps(self, tmp_path):
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text(json.dumps({"ids": {"100": {}, "102": {}}}))
        with patch("kento.pve.PVE_DIR", pve):
            assert next_vmid() == 101

    def test_sequential(self, tmp_path):
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text(json.dumps({"ids": {"100": {}, "101": {}, "102": {}}}))
        with patch("kento.pve.PVE_DIR", pve):
            assert next_vmid() == 103


class TestValidateVmid:
    def test_rejects_low_vmid(self):
        with pytest.raises(SystemExit):
            validate_vmid(50)

    def test_rejects_taken_vmid(self, tmp_path):
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text(json.dumps({"ids": {"100": {}}}))
        with patch("kento.pve.PVE_DIR", pve):
            with pytest.raises(SystemExit):
                validate_vmid(100)

    def test_accepts_free_vmid(self, tmp_path):
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text(json.dumps({"ids": {"100": {}}}))
        with patch("kento.pve.PVE_DIR", pve):
            validate_vmid(101)  # should not raise


class TestDetectMode:
    def test_auto_pve(self):
        with patch("kento.pve.is_pve", return_value=True):
            assert detect_mode() == "pve"

    def test_auto_lxc(self):
        with patch("kento.pve.is_pve", return_value=False):
            assert detect_mode() == "lxc"

    def test_force_lxc(self):
        assert detect_mode("lxc") == "lxc"

    def test_force_pve(self):
        assert detect_mode("pve") == "pve"


class TestGeneratePveConfig:
    def test_basic_config(self):
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"))
        assert "arch: amd64" in cfg
        assert "ostype: unmanaged" in cfg
        assert "hostname: test" in cfg
        assert "rootfs: /var/lib/lxc/100/rootfs" in cfg
        assert "memory: 512" in cfg
        assert "swap: 0" in cfg
        assert "cores: 1" in cfg
        assert "net0: name=eth0,bridge=vmbr0,type=veth" in cfg
        assert "onboot: 0" in cfg
        assert "features: nesting=1" in cfg
        assert "lxc.mount.entry: proc dev/.lxc/proc proc create=dir,optional" in cfg
        assert "lxc.mount.entry: sys dev/.lxc/sys sysfs create=dir,optional" in cfg
        assert "/dev/fuse dev/fuse none bind,create=file,optional" in cfg
        assert "/dev/net/tun dev/net/tun none bind,create=file,optional" in cfg
        assert "lxc.hook.pre-mount: /var/lib/lxc/100/kento-hook" in cfg
        assert "lxc.mount.auto: proc:rw sys:rw cgroup:rw" in cfg
        assert "lxc.apparmor.profile: unconfined" in cfg
        assert "lxc.init.cmd: /sbin/init" in cfg

    def test_custom_bridge(self):
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  bridge="vmbr1")
        assert "bridge=vmbr1" in cfg

    def test_custom_memory_cores(self):
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  memory=1024, cores=4)
        assert "memory: 1024" in cfg
        assert "cores: 4" in cfg

    def test_nesting_disabled(self):
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  nesting=False)
        assert "nesting" not in cfg
        assert "dev/.lxc/proc" not in cfg
        assert "/dev/fuse" not in cfg

    def test_static_ip(self):
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  ip="192.168.0.160/22", gateway="192.168.0.1")
        assert "ip=192.168.0.160/22" in cfg
        assert "gw=192.168.0.1" in cfg

    def test_static_ip_no_gateway(self):
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  ip="10.0.0.5/24")
        assert "ip=10.0.0.5/24" in cfg
        assert "gw=" not in cfg

    def test_no_static_ip(self):
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"))
        assert "ip=" not in cfg
        assert "gw=" not in cfg

    def test_no_lxc_rootfs_path(self):
        """PVE hardcodes lxc.rootfs.path — we must NOT set it."""
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"))
        assert "lxc.rootfs.path" not in cfg

    def test_no_pre_start_hook(self):
        """PVE mode uses pre-mount, not pre-start."""
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"))
        assert "lxc.hook.pre-start" not in cfg
        assert "lxc.hook.pre-mount" in cfg


class TestWritePveConfig:
    def test_writes_to_node_path(self, tmp_path):
        pve = tmp_path / "pve"
        pve.mkdir()
        with patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve.socket.gethostname", return_value="mynode"):
            result = write_pve_config(100, "arch: amd64\n")

        expected = pve / "nodes" / "mynode" / "lxc" / "100.conf"
        assert result == expected
        assert expected.read_text() == "arch: amd64\n"

    def test_creates_intermediate_dirs(self, tmp_path):
        pve = tmp_path / "pve"
        pve.mkdir()
        with patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve.socket.gethostname", return_value="node1"):
            write_pve_config(200, "test\n")

        assert (pve / "nodes" / "node1" / "lxc" / "200.conf").is_file()


class TestDeletePveConfig:
    def test_deletes_config(self, tmp_path):
        pve = tmp_path / "pve"
        conf_dir = pve / "nodes" / "mynode" / "lxc"
        conf_dir.mkdir(parents=True)
        conf = conf_dir / "100.conf"
        conf.write_text("arch: amd64\n")

        with patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve.socket.gethostname", return_value="mynode"):
            delete_pve_config(100)

        assert not conf.exists()

    def test_missing_config_is_noop(self, tmp_path):
        pve = tmp_path / "pve"
        pve.mkdir()
        with patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve.socket.gethostname", return_value="mynode"):
            delete_pve_config(999)  # should not raise
