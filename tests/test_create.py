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
        assert "lxc.net.0" not in cfg
        assert "lxc.tty.max = 2" in cfg
        assert "lxc.mount.auto = proc:rw sys:rw cgroup:rw" in cfg
        assert "lxc.init.cmd" not in cfg
        assert "lxc.apparmor.profile" not in cfg
        assert "lxc.pty.max" not in cfg
        assert "nesting.conf" in cfg
        assert "/dev/fuse dev/fuse none bind,create=file,optional" in cfg
        assert "/dev/net/tun dev/net/tun none bind,create=file,optional" in cfg

    def test_custom_bridge(self, tmp_path):
        cfg = generate_config("test", tmp_path, bridge="br0")
        assert "lxc.net.0.link = br0" in cfg

    def test_no_bridge(self, tmp_path):
        cfg = generate_config("test", tmp_path, bridge=None)
        assert "lxc.net.0" not in cfg

    def test_static_ip(self, tmp_path):
        cfg = generate_config("test", tmp_path, bridge="lxcbr0",
                              ip="192.168.0.160/22", gateway="192.168.0.1")
        assert "lxc.net.0.ipv4.address = 192.168.0.160/22" in cfg
        assert "lxc.net.0.ipv4.gateway = 192.168.0.1" in cfg

    def test_static_ip_no_gateway(self, tmp_path):
        cfg = generate_config("test", tmp_path, bridge="lxcbr0", ip="10.0.0.5/24")
        assert "lxc.net.0.ipv4.address = 10.0.0.5/24" in cfg
        assert "ipv4.gateway" not in cfg

    def test_no_static_ip(self, tmp_path):
        cfg = generate_config("test", tmp_path)
        assert "ipv4.address" not in cfg
        assert "ipv4.gateway" not in cfg

    def test_nesting_disabled(self, tmp_path):
        cfg = generate_config("test", tmp_path, nesting=False)
        assert "lxc.mount.auto = proc:mixed sys:mixed cgroup:mixed" in cfg
        assert "nesting.conf" not in cfg
        assert "/dev/fuse" not in cfg
        assert "/dev/net/tun" not in cfg


class TestCreate:
    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_creates_directory_structure(self, mock_root, mock_layers,
                                         mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
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
             patch("kento.create.upper_base", return_value=tmp_path / "myimage_latest-0"):
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
             patch("kento.create.upper_base", return_value=state):
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
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test")
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test")

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_start_calls_lxc_start(self, mock_root, mock_layers,
                                    mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
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
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
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
             patch("kento.pve.write_pve_config", side_effect=fake_write):
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
    def test_vmid_with_lxc_mode_errors(self, mock_root, mock_layers,
                                        mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
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
             patch("kento.pve.write_pve_config", return_value=Path("/etc/pve/lxc/100.conf")):
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
             patch("kento.pve.write_pve_config", return_value=Path("/etc/pve/lxc/200.conf")):
            create("myimage:latest", name="test", mode="pve", vmid=200)

        assert (tmp_path / "200" / "kento-mode").read_text().strip() == "pve"

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_no_bridge_means_no_network(self, mock_root, mock_layers,
                                         mock_run, tmp_path):
        """When bridge is None (omitted), no network config is generated."""
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc")

        cfg = (tmp_path / "test" / "config").read_text()
        assert "lxc.net.0" not in cfg


class TestStaticIp:
    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_ip_creates_net_file_and_network_unit(self, mock_root, mock_layers,
                                                   mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", ip="192.168.0.160/22",
                   gateway="192.168.0.1", dns="8.8.8.8")

        lxc_dir = tmp_path / "test"
        # kento-net metadata
        net = (lxc_dir / "kento-net").read_text()
        assert "ip=192.168.0.160/22" in net
        assert "gateway=192.168.0.1" in net
        assert "dns=8.8.8.8" in net

        # 10-static.network in upper layer
        unit = (lxc_dir / "upper" / "etc" / "systemd" / "network" /
                "10-static.network").read_text()
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
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", ip="10.0.0.5/24")

        unit = (tmp_path / "test" / "upper" / "etc" / "systemd" / "network" /
                "10-static.network").read_text()
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
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", gateway="192.168.0.1")

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_dns_without_ip_writes_resolved_dropin(self, mock_root, mock_layers,
                                                    mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", dns="8.8.8.8")

        dropin = (tmp_path / "test" / "upper" / "etc" / "systemd" /
                  "resolved.conf.d" / "90-kento.conf")
        assert dropin.exists()
        content = dropin.read_text()
        assert "DNS=8.8.8.8" in content
        assert "[Resolve]" in content


class TestGuestConfig:
    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_hostname_injected(self, mock_root, mock_layers, mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test")

        hostname = (tmp_path / "test" / "upper" / "etc" / "hostname").read_text()
        assert hostname.strip() == "test"

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_searchdomain_in_network_file(self, mock_root, mock_layers,
                                           mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", ip="10.0.0.5/24",
                   searchdomain="example.com")

        unit = (tmp_path / "test" / "upper" / "etc" / "systemd" / "network" /
                "10-static.network").read_text()
        assert "Domains=example.com" in unit
        net = (tmp_path / "test" / "kento-net").read_text()
        assert "searchdomain=example.com" in net

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_timezone_injected(self, mock_root, mock_layers, mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", timezone="Asia/Tokyo")

        lxc_dir = tmp_path / "test"
        assert (lxc_dir / "kento-tz").read_text().strip() == "Asia/Tokyo"
        assert (lxc_dir / "upper" / "etc" / "timezone").read_text().strip() == "Asia/Tokyo"
        localtime = lxc_dir / "upper" / "etc" / "localtime"
        assert localtime.is_symlink()
        assert str(localtime.readlink()) == "/usr/share/zoneinfo/Asia/Tokyo"

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_env_injected(self, mock_root, mock_layers, mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", env=["FOO=bar", "BAZ=qux"])

        lxc_dir = tmp_path / "test"
        env_content = (lxc_dir / "kento-env").read_text()
        assert "FOO=bar" in env_content
        assert "BAZ=qux" in env_content
        etc_env = (lxc_dir / "upper" / "etc" / "environment").read_text()
        assert "FOO=bar" in etc_env
        assert "BAZ=qux" in etc_env
        # Also in LXC config
        cfg = (lxc_dir / "config").read_text()
        assert "lxc.environment = FOO=bar" in cfg
        assert "lxc.environment = BAZ=qux" in cfg

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_searchdomain_without_ip_writes_metadata(self, mock_root, mock_layers,
                                                      mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", searchdomain="example.com")

        net = (tmp_path / "test" / "kento-net").read_text()
        assert "searchdomain=example.com" in net
        # No 10-static.network (no IP)
        assert not (tmp_path / "test" / "upper" / "etc" / "systemd" /
                    "network" / "10-static.network").exists()


class TestVmCreate:
    """Tests for plain VM mode (no PVE host)."""

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


class TestPveVmCreate:
    """Tests for pve-vm mode (VM mode on PVE host)."""

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_pve_vm_mode_autodetected(self, mock_root, mock_layers, tmp_path):
        """VM mode on PVE host auto-detects to pve-vm."""
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text(json.dumps({"ids": {}}))

        snippets = tmp_path / "snippets"
        snippets.mkdir()

        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "test"), \
             patch("kento.pve.PVE_DIR", pve), \
             patch("kento.vm_hook.find_snippets_dir", return_value=(snippets, "local")), \
             patch("kento.pve.write_qm_config", return_value=Path("/etc/pve/qemu-server/100.conf")):
            create("myimage:latest", name="test", mode="vm")

        d = vm_dir / "test"
        assert (d / "kento-mode").read_text().strip() == "pve-vm"
        assert (d / "kento-vmid").read_text().strip() == "100"
        assert (d / "kento-hook").is_file()

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_pve_vm_creates_hookscript(self, mock_root, mock_layers, tmp_path):
        """pve-vm mode generates VM hookscript."""
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text(json.dumps({"ids": {}}))

        snippets = tmp_path / "snippets"
        snippets.mkdir()

        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "test"), \
             patch("kento.pve.PVE_DIR", pve), \
             patch("kento.vm_hook.find_snippets_dir", return_value=(snippets, "local")), \
             patch("kento.pve.write_qm_config", return_value=Path("/etc/pve/qemu-server/100.conf")):
            create("myimage:latest", name="test", mode="vm")

        hook = vm_dir / "test" / "kento-hook"
        assert hook.is_file()
        content = hook.read_text()
        assert "pre-start" in content
        assert "post-stop" in content

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_pve_vm_creates_snippets_wrapper(self, mock_root, mock_layers, tmp_path):
        """pve-vm mode creates snippets wrapper."""
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text(json.dumps({"ids": {}}))

        snippets = tmp_path / "snippets"
        snippets.mkdir()

        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "test"), \
             patch("kento.pve.PVE_DIR", pve), \
             patch("kento.vm_hook.find_snippets_dir", return_value=(snippets, "local")), \
             patch("kento.pve.write_qm_config", return_value=Path("/etc/pve/qemu-server/100.conf")):
            create("myimage:latest", name="test", mode="vm")

        wrapper = snippets / "kento-vm-100.sh"
        assert wrapper.is_file()
        assert "exec" in wrapper.read_text()

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_pve_vm_writes_qm_config(self, mock_root, mock_layers, tmp_path):
        """pve-vm mode calls write_qm_config."""
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text(json.dumps({"ids": {}}))

        snippets = tmp_path / "snippets"
        snippets.mkdir()

        written_config = {}
        def fake_write_qm(vmid, content):
            written_config["vmid"] = vmid
            written_config["content"] = content
            return Path(f"/etc/pve/qemu-server/{vmid}.conf")

        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "test"), \
             patch("kento.pve.PVE_DIR", pve), \
             patch("kento.vm_hook.find_snippets_dir", return_value=(snippets, "local")), \
             patch("kento.pve.write_qm_config", side_effect=fake_write_qm):
            create("myimage:latest", name="test", mode="vm")

        assert written_config["vmid"] == 100
        assert "name: test" in written_config["content"]
        assert "hookscript:" in written_config["content"]

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_pve_vm_no_port_for_bridge(self, mock_root, mock_layers, tmp_path):
        """pve-vm with bridge networking doesn't create port file."""
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text(json.dumps({"ids": {}}))

        snippets = tmp_path / "snippets"
        snippets.mkdir()

        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "test"), \
             patch("kento.pve.PVE_DIR", pve), \
             patch("kento.vm_hook.find_snippets_dir", return_value=(snippets, "local")), \
             patch("kento.pve.write_qm_config", return_value=Path("/etc/pve/qemu-server/100.conf")), \
             patch("kento._bridge_exists", return_value=True):
            create("myimage:latest", name="test", mode="vm",
                   net_type="bridge", bridge="vmbr0")

        assert not (vm_dir / "test" / "kento-port").exists()
