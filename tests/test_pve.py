"""Tests for PVE integration."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kento.pve import (
    is_pve, _used_vmids, next_vmid, validate_vmid,
    generate_pve_config, write_pve_config, delete_pve_config,
    generate_qm_config, write_qm_config, delete_qm_config,
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
        assert "ostype: unmanaged" in cfg
        assert "hostname: test" in cfg
        assert "rootfs: /var/lib/lxc/100/rootfs" in cfg
        assert "net0:" not in cfg  # no networking by default
        assert "features: nesting=1" in cfg
        assert "lxc.mount.entry: proc dev/.lxc/proc proc create=dir,optional" in cfg
        assert "lxc.mount.entry: sys dev/.lxc/sys sysfs create=dir,optional" in cfg
        assert "/dev/fuse dev/fuse none bind,create=file,optional" in cfg
        assert "/dev/net/tun dev/net/tun none bind,create=file,optional" in cfg
        assert "lxc.hook.pre-mount: /var/lib/lxc/100/kento-hook" in cfg
        assert "lxc.mount.auto: proc:rw sys:rw cgroup:rw" in cfg
        assert "lxc.tty.max: 2" in cfg
        assert "arch: amd64" in cfg
        assert "memory:" not in cfg
        assert "swap:" not in cfg
        assert "cores:" not in cfg
        assert "onboot:" not in cfg
        assert "lxc.apparmor.profile:" not in cfg
        assert "lxc.init.cmd:" not in cfg
        assert "lxc.pty.max:" not in cfg

    def test_custom_bridge(self):
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  bridge="vmbr1", net_type="bridge")
        assert "bridge=vmbr1" in cfg

    def test_nesting_disabled(self):
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  nesting=False)
        assert "nesting" not in cfg
        assert "dev/.lxc/proc" not in cfg
        assert "/dev/fuse" not in cfg
        assert "lxc.mount.auto: proc:mixed sys:mixed cgroup:mixed" in cfg

    def test_no_bridge(self):
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  bridge=None)
        assert "net0:" not in cfg

    def test_static_ip(self):
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  bridge="vmbr0", net_type="bridge",
                                  ip="192.168.0.160/22", gateway="192.168.0.1")
        assert "ip=192.168.0.160/22" in cfg
        assert "gw=192.168.0.1" in cfg

    def test_static_ip_no_gateway(self):
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  bridge="vmbr0", net_type="bridge",
                                  ip="10.0.0.5/24")
        assert "ip=10.0.0.5/24" in cfg
        assert "gw=" not in cfg

    def test_no_static_ip(self):
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"))
        assert "ip=" not in cfg
        assert "gw=" not in cfg

    def test_nameserver(self):
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  nameserver="8.8.8.8")
        assert "nameserver: 8.8.8.8" in cfg

    def test_searchdomain(self):
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  searchdomain="example.com")
        assert "searchdomain: example.com" in cfg

    def test_timezone(self):
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  timezone="Europe/Berlin")
        assert "timezone: Europe/Berlin" in cfg

    def test_env(self):
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  env=["FOO=bar", "BAZ=qux"])
        assert "lxc.environment: FOO=bar" in cfg
        assert "lxc.environment: BAZ=qux" in cfg

    def test_no_lxc_rootfs_path(self):
        """PVE hardcodes lxc.rootfs.path — we must NOT set it."""
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"))
        assert "lxc.rootfs.path" not in cfg

    def test_no_pre_start_hook(self):
        """PVE mode uses pre-mount, not pre-start."""
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"))
        assert "lxc.hook.pre-start" not in cfg
        assert "lxc.hook.pre-mount" in cfg

    def test_arch_arm64(self):
        with patch("kento.pve.platform.machine", return_value="aarch64"):
            cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"))
        assert "arch: arm64" in cfg

    def test_arch_unknown_passthrough(self):
        with patch("kento.pve.platform.machine", return_value="riscv64"):
            cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"))
        assert "arch: riscv64" in cfg


class TestWritePveConfig:
    def test_writes_to_node_path(self, tmp_path):
        pve = tmp_path / "pve"
        pve.mkdir()
        with patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve._pve_node_name", return_value="mynode"):
            result = write_pve_config(100, "arch: amd64\n")

        expected = pve / "nodes" / "mynode" / "lxc" / "100.conf"
        assert result == expected
        assert expected.read_text() == "arch: amd64\n"

    def test_creates_intermediate_dirs(self, tmp_path):
        pve = tmp_path / "pve"
        pve.mkdir()
        with patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve._pve_node_name", return_value="node1"):
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
             patch("kento.pve._pve_node_name", return_value="mynode"):
            delete_pve_config(100)

        assert not conf.exists()

    def test_missing_config_is_noop(self, tmp_path):
        pve = tmp_path / "pve"
        pve.mkdir()
        with patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve._pve_node_name", return_value="mynode"):
            delete_pve_config(999)  # should not raise


class TestGenerateQmConfig:
    def test_basic_config(self):
        cfg = generate_qm_config("test", 100, Path("/var/lib/kento/vm/test"),
                                  hookscript_ref="local:snippets/kento-vm-100.sh")
        assert "name: test" in cfg
        assert "ostype: l26" in cfg
        assert "machine: q35" in cfg
        assert "memory: 512" in cfg
        assert "cores: 1" in cfg
        assert "hookscript: local:snippets/kento-vm-100.sh" in cfg
        assert "serial0: socket" in cfg

    def test_args_contains_kernel(self):
        cfg = generate_qm_config("test", 100, Path("/var/lib/kento/vm/test"),
                                  hookscript_ref="local:snippets/kento-vm-100.sh")
        assert "-kernel /var/lib/kento/vm/test/rootfs/boot/vmlinuz" in cfg

    def test_args_contains_initrd(self):
        cfg = generate_qm_config("test", 100, Path("/var/lib/kento/vm/test"),
                                  hookscript_ref="local:snippets/kento-vm-100.sh")
        assert "-initrd /var/lib/kento/vm/test/rootfs/boot/initramfs.img" in cfg

    def test_args_contains_console(self):
        cfg = generate_qm_config("test", 100, Path("/var/lib/kento/vm/test"),
                                  hookscript_ref="local:snippets/kento-vm-100.sh")
        assert "console=ttyS0 rootfstype=virtiofs root=rootfs" in cfg

    def test_args_contains_virtiofs(self):
        cfg = generate_qm_config("test", 100, Path("/var/lib/kento/vm/test"),
                                  hookscript_ref="local:snippets/kento-vm-100.sh")
        assert "chardev socket,id=vfs,path=/var/lib/kento/vm/test/virtiofsd.sock" in cfg
        assert "vhost-user-fs-pci,chardev=vfs,tag=rootfs" in cfg

    def test_args_contains_memfd(self):
        cfg = generate_qm_config("test", 100, Path("/var/lib/kento/vm/test"),
                                  hookscript_ref="local:snippets/kento-vm-100.sh")
        assert "memory-backend-memfd,id=mem,size=512M,share=on" in cfg
        assert "numa node,memdev=mem" in cfg

    def test_memfd_matches_memory(self):
        cfg = generate_qm_config("test", 100, Path("/d"),
                                  hookscript_ref="ref", memory=1024)
        assert "memory: 1024" in cfg
        assert "size=1024M" in cfg

    def test_kvm_enabled(self):
        cfg = generate_qm_config("test", 100, Path("/d"),
                                  hookscript_ref="ref", kvm=True)
        assert "-enable-kvm" in cfg

    def test_kvm_disabled(self):
        cfg = generate_qm_config("test", 100, Path("/d"),
                                  hookscript_ref="ref", kvm=False)
        assert "-enable-kvm" not in cfg

    def test_bridge_network(self):
        cfg = generate_qm_config("test", 100, Path("/d"),
                                  hookscript_ref="ref",
                                  bridge="vmbr0", net_type="bridge")
        assert "net0: virtio,bridge=vmbr0" in cfg

    def test_no_bridge(self):
        cfg = generate_qm_config("test", 100, Path("/d"),
                                  hookscript_ref="ref")
        assert "net0:" not in cfg

    def test_bridge_with_mac(self):
        """MAC is included in the net0 line via virtio=<MAC> syntax."""
        cfg = generate_qm_config("test", 100, Path("/d"),
                                  hookscript_ref="ref",
                                  bridge="vmbr0", net_type="bridge",
                                  mac="52:54:00:de:ad:be")
        assert "net0: virtio=52:54:00:de:ad:be,bridge=vmbr0" in cfg

    def test_bridge_no_mac_fallback(self):
        """Without mac, net0 uses plain virtio (PVE assigns its own MAC)."""
        cfg = generate_qm_config("test", 100, Path("/d"),
                                  hookscript_ref="ref",
                                  bridge="vmbr0", net_type="bridge")
        assert "net0: virtio,bridge=vmbr0" in cfg
        assert "virtio=" not in cfg

    def test_custom_machine(self):
        cfg = generate_qm_config("test", 100, Path("/d"),
                                  hookscript_ref="ref", machine="pc")
        assert "machine: pc" in cfg

    def test_custom_cores(self):
        cfg = generate_qm_config("test", 100, Path("/d"),
                                  hookscript_ref="ref", cores=4)
        assert "cores: 4" in cfg


class TestWriteQmConfig:
    def test_writes_to_node_path(self, tmp_path):
        pve = tmp_path / "pve"
        pve.mkdir()
        with patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve._pve_node_name", return_value="mynode"):
            result = write_qm_config(100, "name: test\n")

        expected = pve / "nodes" / "mynode" / "qemu-server" / "100.conf"
        assert result == expected
        assert expected.read_text() == "name: test\n"

    def test_creates_intermediate_dirs(self, tmp_path):
        pve = tmp_path / "pve"
        pve.mkdir()
        with patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve._pve_node_name", return_value="node1"):
            write_qm_config(200, "test\n")

        assert (pve / "nodes" / "node1" / "qemu-server" / "200.conf").is_file()


class TestDeleteQmConfig:
    def test_deletes_config(self, tmp_path):
        pve = tmp_path / "pve"
        conf_dir = pve / "nodes" / "mynode" / "qemu-server"
        conf_dir.mkdir(parents=True)
        conf = conf_dir / "100.conf"
        conf.write_text("name: test\n")

        with patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve._pve_node_name", return_value="mynode"):
            delete_qm_config(100)

        assert not conf.exists()

    def test_missing_config_is_noop(self, tmp_path):
        pve = tmp_path / "pve"
        pve.mkdir()
        with patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve._pve_node_name", return_value="mynode"):
            delete_qm_config(999)  # should not raise


class TestPveConfigPortForwarding:
    """Tests for PVE config with port forwarding (Phase 3)."""

    def test_pve_config_has_hooks_when_port_set(self):
        """When port is set, PVE config has lxc.hook.start-host and post-stop."""
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  bridge="vmbr0", net_type="bridge",
                                  port="10022:22")
        assert "lxc.hook.start-host: /var/lib/lxc/100/kento-hook" in cfg
        assert "lxc.hook.post-stop: /var/lib/lxc/100/kento-hook" in cfg

    def test_pve_config_omits_hooks_when_no_port(self):
        """Without port, only lxc.hook.pre-mount is present."""
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"))
        assert "lxc.hook.pre-mount" in cfg
        assert "lxc.hook.start-host" not in cfg
        assert "lxc.hook.post-stop" not in cfg

    def test_pve_config_pre_mount_always_present(self):
        """pre-mount hook is always present regardless of port setting."""
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  bridge="vmbr0", net_type="bridge",
                                  port="10022:22")
        assert "lxc.hook.pre-mount: /var/lib/lxc/100/kento-hook" in cfg


class TestPveConfigMemoryCores:
    """Tests for memory/cores in generate_pve_config."""

    def test_memory_included(self):
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  memory=1024)
        assert "memory: 1024" in cfg

    def test_cores_included(self):
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  cores=4)
        assert "cores: 4" in cfg

    def test_cores_emits_cpulimit(self):
        # cores alone sets cpuset affinity; cpulimit sets the cpu.max quota
        # that matches plain-LXC behavior. Both must be emitted.
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  cores=4)
        assert "cpulimit: 4" in cfg

    def test_cores_emits_lxc_cgroup2_cpu_max(self):
        # The cgroup v2 raw key is what the guest's cgroup namespace reads
        # at /sys/fs/cgroup/cpu.max. PVE's `cpulimit` does not always
        # reach the guest namespace, so emit the raw key directly.
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  cores=4)
        assert "lxc.cgroup2.cpu.max: 400000 100000" in cfg

    def test_memory_emits_lxc_cgroup2_memory_max(self):
        # 256 MiB in bytes.
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  memory=256)
        assert "lxc.cgroup2.memory.max: 268435456" in cfg

    def test_cpulimit_omitted_without_cores(self):
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"))
        assert "cpulimit:" not in cfg
        assert "lxc.cgroup2.cpu.max" not in cfg

    def test_memory_and_cores(self):
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  memory=2048, cores=8)
        assert "memory: 2048" in cfg
        assert "cores: 8" in cfg
        assert "cpulimit: 8" in cfg

    def test_no_memory_no_cores_by_default(self):
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"))
        assert "memory:" not in cfg
        assert "cores:" not in cfg

    def test_memory_none_omitted(self):
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  memory=None)
        assert "memory:" not in cfg
        assert "lxc.cgroup2.memory.max" not in cfg

    def test_cores_none_omitted(self):
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  cores=None)
        assert "cores:" not in cfg
        assert "cpulimit:" not in cfg
