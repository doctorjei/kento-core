"""Tests for container creation."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from kento.create import create, generate_config
from kento.vm import VM_BASE


class TestGenerateConfig:
    def test_basic_config(self, tmp_path):
        cfg = generate_config("test", tmp_path)
        assert "lxc.uts.name = test" in cfg
        assert f"lxc.rootfs.path = dir:{tmp_path}/rootfs" in cfg
        assert "lxc.hook.pre-start" in cfg
        assert "lxc.hook.post-stop" in cfg
        assert "lxc.net.0.link = lxcbr0" in cfg
        assert "lxc.init.cmd = /sbin/init" in cfg
        assert "nesting.conf" in cfg
        assert "/dev/fuse dev/fuse none bind,create=file,optional" in cfg
        assert "/dev/net/tun dev/net/tun none bind,create=file,optional" in cfg

    def test_custom_bridge(self, tmp_path):
        cfg = generate_config("test", tmp_path, bridge="br0")
        assert "lxc.net.0.link = br0" in cfg

    def test_memory_limit(self, tmp_path):
        cfg = generate_config("test", tmp_path, memory=512)
        assert "lxc.cgroup2.memory.max = 512M" in cfg

    def test_no_memory_limit(self, tmp_path):
        cfg = generate_config("test", tmp_path, memory=0)
        assert "memory.max" not in cfg

    def test_cores_limit(self, tmp_path):
        cfg = generate_config("test", tmp_path, cores=4)
        assert "lxc.cgroup2.cpuset.cpus = 0-3" in cfg

    def test_no_cores_limit(self, tmp_path):
        cfg = generate_config("test", tmp_path, cores=0)
        assert "cpuset.cpus" not in cfg

    def test_static_ip(self, tmp_path):
        cfg = generate_config("test", tmp_path, ip="192.168.0.160/22",
                              gateway="192.168.0.1")
        assert "lxc.net.0.ipv4.address = 192.168.0.160/22" in cfg
        assert "lxc.net.0.ipv4.gateway = 192.168.0.1" in cfg

    def test_static_ip_no_gateway(self, tmp_path):
        cfg = generate_config("test", tmp_path, ip="10.0.0.5/24")
        assert "lxc.net.0.ipv4.address = 10.0.0.5/24" in cfg
        assert "ipv4.gateway" not in cfg

    def test_no_static_ip(self, tmp_path):
        cfg = generate_config("test", tmp_path)
        assert "ipv4.address" not in cfg
        assert "ipv4.gateway" not in cfg

    def test_nesting_disabled(self, tmp_path):
        cfg = generate_config("test", tmp_path, nesting=False)
        assert "nesting.conf" not in cfg
        assert "/dev/fuse" not in cfg
        assert "/dev/net/tun" not in cfg


_BRIDGE_PATCH = patch("kento.create._bridge_exists", return_value=True)


class TestCreate:
    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_creates_directory_structure(self, mock_root, mock_layers,
                                         mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"), \
             _BRIDGE_PATCH:
            create("myimage:latest", name="test")

        lxc_dir = tmp_path / "test"
        assert (lxc_dir / "rootfs").is_dir()
        assert (lxc_dir / "upper").is_dir()
        assert (lxc_dir / "work").is_dir()
        assert (lxc_dir / "config").is_file()
        assert (lxc_dir / "kento-hook").is_file()
        assert (lxc_dir / "kento-image").read_text().strip() == "myimage:latest"
        assert (lxc_dir / "kento-layers").read_text().strip() == "/a:/b"
        assert (lxc_dir / "kento-state").is_file()
        assert (lxc_dir / "kento-name").read_text().strip() == "test"

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_auto_name_from_image(self, mock_root, mock_layers,
                                    mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "myimage_latest-0"), \
             _BRIDGE_PATCH:
            create("myimage:latest")

        lxc_dir = tmp_path / "myimage_latest-0"
        assert (lxc_dir / "rootfs").is_dir()
        assert (lxc_dir / "kento-name").read_text().strip() == "myimage_latest-0"
        assert (lxc_dir / "kento-image").read_text().strip() == "myimage:latest"

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_sudo_upper_in_separate_dir(self, mock_root, mock_layers,
                                         mock_run, tmp_path):
        state = tmp_path / "user-state" / "test"
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=state), \
             _BRIDGE_PATCH:
            create("myimage:latest", name="test")

        lxc_dir = tmp_path / "test"
        assert (state / "upper").is_dir()
        assert (state / "work").is_dir()
        assert (lxc_dir / "kento-state").read_text().strip() == str(state)
        # Hook should reference the state dir for upper/work
        hook = (lxc_dir / "kento-hook").read_text()
        assert str(state) in hook

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_refuses_existing_container(self, mock_root, mock_layers,
                                         mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"), \
             _BRIDGE_PATCH:
            create("myimage:latest", name="test")
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test")

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_start_calls_lxc_start(self, mock_root, mock_layers,
                                    mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"), \
             _BRIDGE_PATCH:
            create("myimage:latest", name="test", start=True)

        mock_run.assert_called_once_with(
            ["lxc-start", "-n", "test"], check=True,
        )

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_kento_mode_file_lxc(self, mock_root, mock_layers,
                                  mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"), \
             _BRIDGE_PATCH:
            create("myimage:latest", name="test", mode="lxc")

        assert (tmp_path / "test" / "kento-mode").read_text().strip() == "lxc"

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_pve_create_uses_vmid_as_dir(self, mock_root, mock_layers,
                                          mock_run, tmp_path):
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text(json.dumps({"ids": {}}))
        pve_conf = tmp_path / "pve-conf" / "100.conf"
        pve_conf.parent.mkdir()

        def fake_write(vmid, content):
            pve_conf.write_text(content)
            return pve_conf

        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "100"), \
             patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve.write_pve_config", side_effect=fake_write), \
             _BRIDGE_PATCH:
            create("myimage:latest", name="test", mode="pve")

        # Container dir should be VMID-based
        lxc_dir = tmp_path / "100"
        assert (lxc_dir / "rootfs").is_dir()
        assert (lxc_dir / "kento-hook").is_file()
        assert (lxc_dir / "kento-mode").read_text().strip() == "pve"
        # PVE config written via write_pve_config
        assert pve_conf.is_file()
        pve_cfg = pve_conf.read_text()
        assert "hostname: test" in pve_cfg
        assert "lxc.hook.pre-mount" in pve_cfg

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_pve_bridge_default(self, mock_root, mock_layers,
                                 mock_run, tmp_path):
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text(json.dumps({"ids": {}}))
        pve_conf = tmp_path / "pve-conf" / "100.conf"
        pve_conf.parent.mkdir()

        def fake_write(vmid, content):
            pve_conf.write_text(content)
            return pve_conf

        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "100"), \
             patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve.write_pve_config", side_effect=fake_write), \
             _BRIDGE_PATCH:
            create("myimage:latest", name="test", mode="pve")

        pve_cfg = pve_conf.read_text()
        assert "bridge=vmbr0" in pve_cfg

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_lxc_bridge_default(self, mock_root, mock_layers,
                                 mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"), \
             _BRIDGE_PATCH:
            create("myimage:latest", name="test", mode="lxc")

        cfg = (tmp_path / "test" / "config").read_text()
        assert "lxc.net.0.link = lxcbr0" in cfg

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_vmid_with_lxc_mode_errors(self, mock_root, mock_layers,
                                        mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"), \
             _BRIDGE_PATCH:
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", mode="lxc", vmid=100)

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_pve_start_calls_pct(self, mock_root, mock_layers,
                                  mock_run, tmp_path):
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text(json.dumps({"ids": {}}))

        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "100"), \
             patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve.write_pve_config", return_value=Path("/etc/pve/lxc/100.conf")), \
             _BRIDGE_PATCH:
            create("myimage:latest", name="test", mode="pve", start=True)

        mock_run.assert_called_once_with(
            ["pct", "start", "100"], check=True,
        )

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_pve_explicit_vmid(self, mock_root, mock_layers,
                                mock_run, tmp_path):
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text(json.dumps({"ids": {}}))

        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "200"), \
             patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve.write_pve_config", return_value=Path("/etc/pve/lxc/200.conf")), \
             _BRIDGE_PATCH:
            create("myimage:latest", name="test", mode="pve", vmid=200)

        assert (tmp_path / "200" / "kento-mode").read_text().strip() == "pve"

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_bridge_not_found_errors(self, mock_root, mock_layers,
                                      mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"), \
             patch("kento.create._bridge_exists", return_value=False):
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", mode="lxc")

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_explicit_bridge_not_found_errors(self, mock_root, mock_layers,
                                               mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"), \
             patch("kento.create._bridge_exists", return_value=False):
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", mode="lxc", bridge="br99")

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_pve_fallback_to_lxcbr0(self, mock_root, mock_layers,
                                      mock_run, tmp_path, capsys):
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text(json.dumps({"ids": {}}))
        pve_conf = tmp_path / "pve-conf" / "100.conf"
        pve_conf.parent.mkdir()

        def fake_write(vmid, content):
            pve_conf.write_text(content)
            return pve_conf

        def bridge_check(name):
            return name == "lxcbr0"  # vmbr0 missing, lxcbr0 exists

        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "100"), \
             patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve.write_pve_config", side_effect=fake_write), \
             patch("kento.create._bridge_exists", side_effect=bridge_check):
            create("myimage:latest", name="test", mode="pve")

        pve_cfg = pve_conf.read_text()
        assert "bridge=lxcbr0" in pve_cfg
        stderr = capsys.readouterr().err
        assert "vmbr0 not found" in stderr


class TestStaticIp:
    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_ip_creates_net_file_and_network_unit(self, mock_root, mock_layers,
                                                   mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"), \
             _BRIDGE_PATCH:
            create("myimage:latest", name="test", ip="192.168.0.160/22",
                   gateway="192.168.0.1", dns="8.8.8.8")

        lxc_dir = tmp_path / "test"
        # kento-net metadata
        net = (lxc_dir / "kento-net").read_text()
        assert "ip=192.168.0.160/22" in net
        assert "gateway=192.168.0.1" in net
        assert "dns=8.8.8.8" in net

        # 90-static.network in upper layer
        unit = (lxc_dir / "upper" / "etc" / "systemd" / "network" /
                "90-static.network").read_text()
        assert "Address=192.168.0.160/22" in unit
        assert "Gateway=192.168.0.1" in unit
        assert "DNS=8.8.8.8" in unit
        assert "[Match]" in unit
        assert "Name=eth0" in unit

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_ip_only_no_gateway_dns(self, mock_root, mock_layers,
                                     mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"), \
             _BRIDGE_PATCH:
            create("myimage:latest", name="test", ip="10.0.0.5/24")

        unit = (tmp_path / "test" / "upper" / "etc" / "systemd" / "network" /
                "90-static.network").read_text()
        assert "Address=10.0.0.5/24" in unit
        assert "Gateway" not in unit
        assert "DNS" not in unit

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_ip_with_vm_mode_errors(self, mock_root, mock_layers, tmp_path):
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()

        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "test"):
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", mode="vm",
                       ip="192.168.0.160/22")

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_gateway_without_ip_errors(self, mock_root, mock_layers,
                                        mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"), \
             _BRIDGE_PATCH:
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", gateway="192.168.0.1")

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_dns_without_ip_errors(self, mock_root, mock_layers,
                                    mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"), \
             _BRIDGE_PATCH:
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", dns="8.8.8.8")


class TestVmCreate:
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_vm_creates_in_vm_base(self, mock_root, mock_layers, tmp_path):
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()

        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "test"):
            create("myimage:latest", name="test", mode="vm")

        lxc_dir = vm_dir / "test"
        assert (lxc_dir / "rootfs").is_dir()
        assert (lxc_dir / "upper").is_dir()
        assert (lxc_dir / "work").is_dir()
        assert (lxc_dir / "kento-image").read_text().strip() == "myimage:latest"
        assert (lxc_dir / "kento-layers").read_text().strip() == "/a:/b"
        assert (lxc_dir / "kento-mode").read_text().strip() == "vm"
        assert (lxc_dir / "kento-name").read_text().strip() == "test"
        assert (lxc_dir / "kento-port").is_file()

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_vm_no_hook(self, mock_root, mock_layers, tmp_path):
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()

        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "test"):
            create("myimage:latest", name="test", mode="vm")

        lxc_dir = vm_dir / "test"
        assert not (lxc_dir / "kento-hook").exists()
        assert not (lxc_dir / "config").exists()

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_vm_auto_port(self, mock_root, mock_layers, tmp_path):
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()

        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "test"):
            create("myimage:latest", name="test", mode="vm")

        port = (vm_dir / "test" / "kento-port").read_text().strip()
        assert port == "10022:22"

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_vm_explicit_port(self, mock_root, mock_layers, tmp_path):
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()

        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "test"):
            create("myimage:latest", name="test", mode="vm", port="12345:2222")

        port = (vm_dir / "test" / "kento-port").read_text().strip()
        assert port == "12345:2222"

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_vmid_with_vm_mode_errors(self, mock_root, mock_layers, tmp_path):
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()

        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "test"):
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", mode="vm", vmid=100)

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_vm_auto_name(self, mock_root, mock_layers, tmp_path):
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()

        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "myimage_latest-0"):
            create("myimage:latest", mode="vm")

        lxc_dir = vm_dir / "myimage_latest-0"
        assert (lxc_dir / "kento-name").read_text().strip() == "myimage_latest-0"
