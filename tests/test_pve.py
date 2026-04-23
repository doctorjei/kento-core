"""Tests for PVE integration."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kento.pve import (
    is_pve, _used_vmids, next_vmid, validate_vmid,
    generate_pve_config, write_pve_config, delete_pve_config,
    generate_qm_config, write_qm_config, delete_qm_config,
    generate_qm_args, sync_qm_args_to_memory, _parse_qm_conf_field,
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
        """Without port/memory/cores, only lxc.hook.pre-mount is present."""
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"))
        assert "lxc.hook.pre-mount" in cfg
        assert "lxc.hook.start-host" not in cfg
        assert "lxc.hook.post-stop" not in cfg

    def test_pve_config_memory_wires_start_host(self):
        """Memory alone must wire start-host so the hook can propagate the
        limit into the inner `ns` cgroup at container-start time."""
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  memory=256)
        assert "lxc.hook.start-host: /var/lib/lxc/100/kento-hook" in cfg
        assert "lxc.hook.post-stop: /var/lib/lxc/100/kento-hook" in cfg

    def test_pve_config_cores_wires_start_host(self):
        """Cores alone must wire start-host for the same reason as memory."""
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  cores=2)
        assert "lxc.hook.start-host: /var/lib/lxc/100/kento-hook" in cfg
        assert "lxc.hook.post-stop: /var/lib/lxc/100/kento-hook" in cfg

    def test_pve_config_pre_mount_always_present(self):
        """pre-mount hook is always present regardless of port setting."""
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  bridge="vmbr0", net_type="bridge",
                                  port="10022:22")
        assert "lxc.hook.pre-mount: /var/lib/lxc/100/kento-hook" in cfg


class TestPveConfigHookscriptRef:
    """Tests for hookscript_ref kwarg — PVE-native snippets replace start-host."""

    def test_hookscript_ref_emits_hookscript_line(self):
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  port="10205:22",
                                  hookscript_ref="local:snippets/kento-lxc-100.sh")
        assert "hookscript: local:snippets/kento-lxc-100.sh" in cfg

    def test_hookscript_ref_skips_start_host_and_post_stop(self):
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  port="10205:22",
                                  hookscript_ref="local:snippets/kento-lxc-100.sh")
        assert "lxc.hook.start-host" not in cfg
        assert "lxc.hook.post-stop" not in cfg

    def test_hookscript_ref_wins_without_port_memory_cores(self):
        """When hookscript_ref is set, it's emitted even if no resource flags."""
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  hookscript_ref="local:snippets/kento-lxc-100.sh")
        assert "hookscript: local:snippets/kento-lxc-100.sh" in cfg
        assert "lxc.hook.start-host" not in cfg
        assert "lxc.hook.post-stop" not in cfg

    def test_legacy_behavior_without_hookscript_ref(self):
        """Without hookscript_ref, port set still emits start-host/post-stop."""
        cfg = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                  port="10205:22")
        assert "hookscript:" not in cfg
        assert "lxc.hook.start-host: /var/lib/lxc/100/kento-hook" in cfg
        assert "lxc.hook.post-stop: /var/lib/lxc/100/kento-hook" in cfg

    def test_pre_mount_always_present(self):
        """pre-mount is emitted regardless of hookscript_ref — PVE accepts it."""
        cfg_with = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                        port="10205:22",
                                        hookscript_ref="local:snippets/kento-lxc-100.sh")
        cfg_without = generate_pve_config("test", 100, Path("/var/lib/lxc/100"),
                                           port="10205:22")
        assert "lxc.hook.pre-mount: /var/lib/lxc/100/kento-hook" in cfg_with
        assert "lxc.hook.pre-mount: /var/lib/lxc/100/kento-hook" in cfg_without


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


class TestGenerateQmArgs:
    """Tests for the kento-managed ``args:`` payload generator."""

    def test_memfd_size_matches_memory(self):
        args = generate_qm_args(Path("/d"), memory=2048)
        assert "memory-backend-memfd,id=mem,size=2048M,share=on" in args

    def test_kvm_enabled(self):
        args = generate_qm_args(Path("/d"), kvm=True)
        assert args.startswith("-enable-kvm ")

    def test_kvm_disabled(self):
        args = generate_qm_args(Path("/d"), kvm=False)
        assert "-enable-kvm" not in args

    def test_contains_kernel_and_initrd(self):
        args = generate_qm_args(Path("/var/lib/kento/vm/t"), memory=512)
        assert "-kernel /var/lib/kento/vm/t/rootfs/boot/vmlinuz" in args
        assert "-initrd /var/lib/kento/vm/t/rootfs/boot/initramfs.img" in args

    def test_used_by_generate_qm_config(self):
        """``generate_qm_config`` must emit the same args payload as
        ``generate_qm_args`` so create/scrub can't drift."""
        container = Path("/var/lib/kento/vm/t")
        expected = generate_qm_args(container, memory=1024, kvm=True)
        cfg = generate_qm_config(
            "t", 100, container,
            hookscript_ref="local:snippets/kento-vm-100.sh",
            memory=1024, kvm=True,
        )
        assert f"args: {expected}" in cfg

    def test_no_passthrough_file(self, tmp_path):
        """B2: absence of kento-qemu-args leaves args payload unchanged."""
        args = generate_qm_args(tmp_path, memory=512, kvm=True)
        # Kento's own last element is the -numa clause.
        assert args.endswith("-numa node,memdev=mem")

    def test_passthrough_single_entry_appended(self, tmp_path):
        """B2: a single-entry kento-qemu-args line is appended space-separated
        after kento's own args."""
        (tmp_path / "kento-qemu-args").write_text("-device=virtio-rng-pci\n")
        args = generate_qm_args(tmp_path, memory=512, kvm=True)
        assert args.endswith(" -device=virtio-rng-pci")
        # Precedes by kento's final -numa element.
        assert "-numa node,memdev=mem -device=virtio-rng-pci" in args

    def test_passthrough_multi_entry_ordered(self, tmp_path):
        """B2: multi-line kento-qemu-args preserves order after kento's own."""
        (tmp_path / "kento-qemu-args").write_text(
            "-device=virtio-rng-pci\n-cpu=max\n"
        )
        args = generate_qm_args(tmp_path, memory=512, kvm=True)
        # Both pass-through entries appear at the tail in order.
        assert args.endswith(" -device=virtio-rng-pci -cpu=max")

    def test_passthrough_whitespace_errors(self, tmp_path, capsys):
        """B2: qm args: is whitespace-tokenized, so a line that itself
        contains whitespace would mis-split at boot. Reject explicitly."""
        (tmp_path / "kento-qemu-args").write_text("-device virtio-rng-pci\n")
        with pytest.raises(SystemExit) as exc:
            generate_qm_args(tmp_path, memory=512, kvm=True)
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "kento-qemu-args line contains whitespace" in captured.err
        assert "--qemu-arg" in captured.err

    def test_passthrough_blank_lines_ignored(self, tmp_path):
        """Blank lines must not turn into empty tokens in the args: payload."""
        (tmp_path / "kento-qemu-args").write_text("\n-device=virtio-rng-pci\n\n")
        args = generate_qm_args(tmp_path, memory=512, kvm=True)
        assert args.endswith(" -device=virtio-rng-pci")
        # No double-space from an empty token.
        assert "  " not in args


class TestGeneratePveConfigPassthrough:
    """B3: kento-pve-args lines appended verbatim to pve-lxc config."""

    def test_no_passthrough_file(self, tmp_path):
        """Baseline: absent kento-pve-args leaves config byte-identical."""
        # Use a container_dir that does NOT have the file.
        before = generate_pve_config("test", 100, tmp_path)
        after = generate_pve_config("test", 100, tmp_path)
        assert before == after
        # And the last non-empty line is kento-controlled (no stray appends).
        assert before.rstrip().splitlines()[-1].startswith("lxc.tty.max:")

    def test_single_line_appended_at_end(self, tmp_path):
        (tmp_path / "kento-pve-args").write_text("tags: kento-test\n")
        cfg = generate_pve_config("test", 100, tmp_path)
        lines = cfg.rstrip().splitlines()
        assert lines[-1] == "tags: kento-test"
        # Kento's own lines come first — confirm arch: still leads.
        assert lines[0].startswith("arch:")

    def test_multiple_lines_appended_in_order(self, tmp_path):
        (tmp_path / "kento-pve-args").write_text(
            "tags: kento-test\nonboot: 1\nunprivileged: 0\n"
        )
        cfg = generate_pve_config("test", 100, tmp_path)
        lines = cfg.rstrip().splitlines()
        # All three appear at the tail in order.
        assert lines[-3:] == ["tags: kento-test", "onboot: 1", "unprivileged: 0"]

    def test_blank_lines_skipped(self, tmp_path):
        """Empty lines in the metadata file must not produce blank config
        lines that could confuse qm's parser."""
        (tmp_path / "kento-pve-args").write_text(
            "\ntags: kento-test\n\nonboot: 1\n\n"
        )
        cfg = generate_pve_config("test", 100, tmp_path)
        assert "tags: kento-test\nonboot: 1\n" in cfg
        # No double-newline in pass-through region.
        assert "\n\n" not in cfg

    def test_appended_after_kento_lines(self, tmp_path):
        """Last-value-wins semantics: a user `memory: 4096` comes AFTER
        kento's `memory: 512` so PVE honours the user's value."""
        (tmp_path / "kento-pve-args").write_text("memory: 4096\n")
        cfg = generate_pve_config("test", 100, tmp_path, memory=512)
        memory_lines = [l for l in cfg.splitlines() if l.startswith("memory:")]
        # Kento's own first, user's last.
        assert memory_lines == ["memory: 512", "memory: 4096"]


class TestGenerateQmConfigPassthrough:
    """B3: kento-pve-args lines appended verbatim to pve-vm qm config."""

    def test_no_passthrough_file(self, tmp_path):
        """Baseline: absent kento-pve-args leaves qm config byte-identical."""
        before = generate_qm_config("test", 100, tmp_path, hookscript_ref="ref")
        after = generate_qm_config("test", 100, tmp_path, hookscript_ref="ref")
        assert before == after

    def test_single_line_appended_at_end(self, tmp_path):
        (tmp_path / "kento-pve-args").write_text("protection: 1\n")
        cfg = generate_qm_config("test", 100, tmp_path, hookscript_ref="ref")
        assert cfg.rstrip().splitlines()[-1] == "protection: 1"

    def test_multiple_lines_appended_in_order(self, tmp_path):
        (tmp_path / "kento-pve-args").write_text(
            "protection: 1\ntags: kento-test\nonboot: 0\n"
        )
        cfg = generate_qm_config("test", 100, tmp_path, hookscript_ref="ref")
        lines = cfg.rstrip().splitlines()
        assert lines[-3:] == ["protection: 1", "tags: kento-test", "onboot: 0"]

    def test_blank_lines_skipped(self, tmp_path):
        (tmp_path / "kento-pve-args").write_text("\nprotection: 1\n\n")
        cfg = generate_qm_config("test", 100, tmp_path, hookscript_ref="ref")
        assert "protection: 1" in cfg
        assert "\n\n" not in cfg

    def test_pve_args_do_not_bleed_into_args_payload(self, tmp_path):
        """kento-pve-args is for qm config lines (key: value), NOT for QEMU
        flags inside args:. Confirm the pass-through entry lands as its own
        config line, not concatenated into the args: line."""
        (tmp_path / "kento-pve-args").write_text("tags: kento-test\n")
        cfg = generate_qm_config("test", 100, tmp_path, hookscript_ref="ref")
        args_line = next(l for l in cfg.splitlines() if l.startswith("args:"))
        assert "tags:" not in args_line


class TestParseQmConfField:
    def test_reads_memory(self):
        c = "name: x\nmemory: 2048\ncores: 4\n"
        assert _parse_qm_conf_field(c, "memory") == "2048"

    def test_reads_cores(self):
        c = "name: x\nmemory: 2048\ncores: 4\n"
        assert _parse_qm_conf_field(c, "cores") == "4"

    def test_missing_returns_none(self):
        assert _parse_qm_conf_field("name: x\n", "memory") is None

    def test_stops_at_snapshot_section(self):
        """Values inside [snapshot-name] sections must not shadow the
        global section. qm stores per-snapshot config after a section
        header; we only care about live config."""
        c = "memory: 2048\n[snap1]\nmemory: 99\n"
        assert _parse_qm_conf_field(c, "memory") == "2048"

    def test_last_occurrence_in_global_wins(self):
        c = "memory: 512\nmemory: 2048\n"
        assert _parse_qm_conf_field(c, "memory") == "2048"


class TestSyncQmArgsToMemory:
    def _setup(self, tmp_path, qm_content):
        pve = tmp_path / "pve"
        conf_dir = pve / "nodes" / "mynode" / "qemu-server"
        conf_dir.mkdir(parents=True)
        conf = conf_dir / "100.conf"
        conf.write_text(qm_content)
        container = tmp_path / "100"
        container.mkdir()
        return pve, conf, container

    def test_rewrites_memfd_to_match_memory(self, tmp_path):
        """Repro: user ran `qm set --memory 2048` after create-time 512M.
        Scrub should rewrite the embedded size= to 2048M."""
        container = tmp_path / "100"
        qm = (
            "name: test\n"
            "ostype: l26\n"
            "memory: 2048\n"
            "cores: 1\n"
            f"args: -kernel {container}/rootfs/boot/vmlinuz "
            f"-object memory-backend-memfd,id=mem,size=512M,share=on\n"
            "serial0: socket\n"
        )
        pve, conf, container = self._setup(tmp_path, qm)
        with patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve._pve_node_name", return_value="mynode"):
            memory, cores = sync_qm_args_to_memory(100, container)

        assert memory == 2048
        assert cores == 1
        new = conf.read_text()
        assert "size=2048M" in new
        assert "size=512M" not in new
        # memory: and other non-args lines preserved
        assert "memory: 2048" in new
        assert "name: test" in new
        assert "ostype: l26" in new
        assert "serial0: socket" in new

    def test_missing_config_is_noop(self, tmp_path):
        pve = tmp_path / "pve"
        pve.mkdir()
        container = tmp_path / "100"
        container.mkdir()
        with patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve._pve_node_name", return_value="mynode"):
            result = sync_qm_args_to_memory(100, container)
        assert result == (None, None)

    def test_missing_memory_field(self, tmp_path):
        qm = "name: test\ncores: 1\nargs: -nographic\n"
        pve, conf, container = self._setup(tmp_path, qm)
        with patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve._pve_node_name", return_value="mynode"):
            memory, cores = sync_qm_args_to_memory(100, container)
        # No memory: to sync against — cores still parsed but file untouched.
        assert memory is None
        assert cores == 1
        assert conf.read_text() == qm  # unchanged

    def test_no_args_line_appends_one(self, tmp_path):
        qm = "name: test\nmemory: 1024\ncores: 2\n"
        pve, conf, container = self._setup(tmp_path, qm)
        with patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve._pve_node_name", return_value="mynode"):
            sync_qm_args_to_memory(100, container)
        new = conf.read_text()
        assert "args: " in new
        assert "size=1024M" in new

    def test_duplicate_args_lines_collapsed(self, tmp_path):
        qm = (
            "name: test\n"
            "memory: 1024\n"
            "args: -enable-kvm -object memory-backend-memfd,id=mem,size=512M,share=on\n"
            "args: -bogus -object memory-backend-memfd,id=mem,size=256M,share=on\n"
        )
        pve, conf, container = self._setup(tmp_path, qm)
        with patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve._pve_node_name", return_value="mynode"):
            sync_qm_args_to_memory(100, container)
        new = conf.read_text()
        args_lines = [l for l in new.splitlines() if l.startswith("args:")]
        assert len(args_lines) == 1
        assert "size=1024M" in args_lines[0]

    def test_updates_kento_memory_metadata(self, tmp_path):
        """PVE config wins: kento-memory / kento-cores metadata files
        get rewritten to match qm config (the user edited qm, not us)."""
        qm = (
            "memory: 4096\n"
            "cores: 8\n"
            f"args: -object memory-backend-memfd,id=mem,size=512M,share=on\n"
        )
        pve, conf, container = self._setup(tmp_path, qm)
        (container / "kento-memory").write_text("512\n")
        (container / "kento-cores").write_text("1\n")
        with patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve._pve_node_name", return_value="mynode"):
            sync_qm_args_to_memory(100, container)
        assert (container / "kento-memory").read_text().strip() == "4096"
        assert (container / "kento-cores").read_text().strip() == "8"

    def test_non_integer_memory_noop(self, tmp_path):
        qm = "memory: oops\nargs: -object memory-backend-memfd,id=mem,size=512M,share=on\n"
        pve, conf, container = self._setup(tmp_path, qm)
        with patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve._pve_node_name", return_value="mynode"):
            memory, _ = sync_qm_args_to_memory(100, container)
        assert memory is None
        # args: line untouched when we can't parse memory.
        assert conf.read_text() == qm
