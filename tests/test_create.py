"""Tests for container creation."""

import json
import subprocess
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
        assert "lxc.apparmor.profile = generated" in cfg
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

    def test_lxc_emits_generated_apparmor_lines(self, tmp_path):
        cfg = generate_config("test", tmp_path, mode="lxc")
        assert "lxc.include = /usr/share/lxc/config/common.conf" in cfg
        assert "lxc.apparmor.profile = generated" in cfg
        assert "lxc.apparmor.allow_nesting = 1" in cfg
        assert "lxc.apparmor.allow_incomplete = 1" in cfg
        # common.conf must appear before nesting.conf (order matters).
        common_idx = cfg.index("common.conf")
        nesting_idx = cfg.index("nesting.conf")
        assert common_idx < nesting_idx, \
            "common.conf include must precede nesting.conf include"
        # apparmor.profile must appear after both includes (overrides nesting.conf default).
        apparmor_idx = cfg.index("lxc.apparmor.profile = generated")
        assert apparmor_idx > nesting_idx, \
            "apparmor.profile = generated must come after nesting.conf include"

    def test_pve_mode_omits_apparmor_lines(self, tmp_path):
        # PVE-LXC handles AppArmor via pct's config, not the kento-generated
        # lxc.conf. generate_config() emits apparmor lines only for mode="lxc".
        cfg = generate_config("test", tmp_path, mode="pve")
        assert "common.conf" not in cfg
        assert "lxc.apparmor.profile" not in cfg
        assert "lxc.apparmor.allow_nesting" not in cfg
        assert "lxc.apparmor.allow_incomplete" not in cfg

    def test_kento_apparmor_profile_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KENTO_APPARMOR_PROFILE", "unconfined")
        cfg = generate_config("test", tmp_path, mode="lxc")
        assert "lxc.apparmor.profile = unconfined" in cfg
        assert "lxc.apparmor.profile = generated" not in cfg

    def test_kento_apparmor_profile_rejects_bogus_value(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("KENTO_APPARMOR_PROFILE", "bogus")
        with pytest.raises(SystemExit):
            generate_config("test", tmp_path, mode="lxc")
        assert "must be 'generated' or 'unconfined'" in capsys.readouterr().err


class TestCreate:
    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_creates_directory_structure(self, mock_root, mock_layers,
                                         mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc")

        lxc_dir = tmp_path / "test"
        assert (lxc_dir / "rootfs").is_dir()
        assert (lxc_dir / "upper").is_dir()
        assert (lxc_dir / "work").is_dir()
        assert (lxc_dir / "config").is_file()
        assert (lxc_dir / "kento-hook").is_file()
        assert (lxc_dir / "kento-inject.sh").is_file()
        assert (lxc_dir / "kento-inject.sh").stat().st_mode & 0o755 == 0o755
        assert (lxc_dir / "kento-image").read_text().strip() == "myimage:latest"
        assert (lxc_dir / "kento-layers").read_text().strip() == "/a:/b"
        assert (lxc_dir / "kento-state").is_file()
        assert (lxc_dir / "kento-name").read_text().strip() == "test"
        # LXC mode does not get a kento-mac file — MAC only makes sense for VMs.
        assert not (lxc_dir / "kento-mac").exists()

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_auto_name_from_image(self, mock_root, mock_layers,
                                    mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "myimage_latest-0"):
            create("myimage:latest", mode="lxc")

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
            create("myimage:latest", name="test", mode="lxc")

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
            create("myimage:latest", name="test", mode="lxc")
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", mode="lxc")

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_start_calls_lxc_start(self, mock_root, mock_layers,
                                    mock_run, tmp_path):
        # Return success for every subprocess.run invocation (including the
        # --start tail which now manually checks returncode rather than
        # relying on check=True).
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout="", stderr="")
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc", start=True)

        lxc_calls = [c for c in mock_run.call_args_list
                     if c[0][0][0] == "lxc-start"]
        assert len(lxc_calls) == 1
        assert list(lxc_calls[0][0][0]) == ["lxc-start", "-n", "test"]

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
        assert (lxc_dir / "kento-inject.sh").is_file()
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
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout="", stderr="")

        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "100"), \
             patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve.write_pve_config", return_value=Path("/etc/pve/lxc/100.conf")):
            create("myimage:latest", name="test", mode="pve", start=True)

        pct_calls = [c for c in mock_run.call_args_list
                     if c[0][0][0] == "pct"]
        assert len(pct_calls) == 1
        assert list(pct_calls[0][0][0]) == ["pct", "start", "100"]

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

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_pve_refuses_duplicate_name_across_vmids(self, mock_root,
                                                     mock_layers, mock_run,
                                                     tmp_path):
        """PVE-LXC must reject a reused --name even when VMIDs differ.

        Before the fix the check was `(LXC_BASE / name).exists()`, which
        never matched because PVE-LXC directories are named after the VMID,
        not the kento name. So two `kento lxc create --name foo --pve` calls
        would both succeed with different VMID dirs.
        """
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text(json.dumps({"ids": {}}))

        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.VM_BASE", tmp_path / "vm"), \
             patch("kento.create.upper_base",
                   side_effect=lambda n, b=None: (b or tmp_path) / n), \
             patch("kento.pve.PVE_DIR", pve), \
             patch("kento.pve.write_pve_config",
                   side_effect=lambda vmid, content:
                   Path(f"/etc/pve/lxc/{vmid}.conf")):
            (tmp_path / "vm").mkdir()
            create("myimage:latest", name="dup", mode="pve", vmid=100)
            with pytest.raises(SystemExit):
                create("myimage:latest", name="dup", mode="pve", vmid=101)

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_refuses_duplicate_name_across_namespaces(self, mock_root,
                                                     mock_layers, mock_run,
                                                     tmp_path):
        """An LXC instance name must block a VM with the same name."""
        lxc_base = tmp_path / "lxc"
        vm_base = tmp_path / "vm"
        lxc_base.mkdir()
        vm_base.mkdir()
        with patch("kento.create.LXC_BASE", lxc_base), \
             patch("kento.create.VM_BASE", vm_base), \
             patch("kento.create.upper_base",
                   side_effect=lambda n, b=None: (b or lxc_base) / n):
            create("myimage:latest", name="shared", mode="lxc")
            with pytest.raises(SystemExit):
                create("myimage:latest", name="shared", mode="vm")

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_force_allows_cross_namespace_vm_then_lxc(self, mock_root,
                                                     mock_layers, mock_run,
                                                     tmp_path):
        """force=True lets an LXC reuse a name that already exists as a VM."""
        lxc_base = tmp_path / "lxc"
        vm_base = tmp_path / "vm"
        lxc_base.mkdir()
        vm_base.mkdir()
        with patch("kento.create.LXC_BASE", lxc_base), \
             patch("kento.create.VM_BASE", vm_base), \
             patch("kento.create.upper_base",
                   side_effect=lambda n, b=None: (b or lxc_base) / n):
            create("myimage:latest", name="shared", mode="vm")
            # Without force, this would raise (covered by the previous test
            # in the reverse direction). With force, it must succeed.
            create("myimage:latest", name="shared", mode="lxc",
                   force=True)

        # Both directories should now exist side by side.
        assert (lxc_base / "shared" / "kento-name").read_text().strip() == "shared"
        assert (vm_base / "shared" / "kento-name").read_text().strip() == "shared"

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_force_allows_cross_namespace_lxc_then_vm(self, mock_root,
                                                     mock_layers, mock_run,
                                                     tmp_path):
        """force=True lets a VM reuse a name that already exists as an LXC."""
        lxc_base = tmp_path / "lxc"
        vm_base = tmp_path / "vm"
        lxc_base.mkdir()
        vm_base.mkdir()
        with patch("kento.create.LXC_BASE", lxc_base), \
             patch("kento.create.VM_BASE", vm_base), \
             patch("kento.create.upper_base",
                   side_effect=lambda n, b=None: (b or lxc_base) / n):
            create("myimage:latest", name="shared", mode="lxc")
            create("myimage:latest", name="shared", mode="vm", force=True)

        assert (lxc_base / "shared" / "kento-name").read_text().strip() == "shared"
        assert (vm_base / "shared" / "kento-name").read_text().strip() == "shared"

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_force_does_not_allow_same_namespace_vm(self, mock_root,
                                                    mock_layers, mock_run,
                                                    tmp_path):
        """force=True must still reject a duplicate in the SAME namespace."""
        lxc_base = tmp_path / "lxc"
        vm_base = tmp_path / "vm"
        lxc_base.mkdir()
        vm_base.mkdir()
        with patch("kento.create.LXC_BASE", lxc_base), \
             patch("kento.create.VM_BASE", vm_base), \
             patch("kento.create.upper_base",
                   side_effect=lambda n, b=None: (b or lxc_base) / n):
            create("myimage:latest", name="shared", mode="vm")
            with pytest.raises(SystemExit):
                create("myimage:latest", name="shared", mode="vm", force=True)

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_force_does_not_allow_same_namespace_lxc(self, mock_root,
                                                    mock_layers, mock_run,
                                                    tmp_path):
        """force=True must still reject a duplicate LXC name."""
        lxc_base = tmp_path / "lxc"
        vm_base = tmp_path / "vm"
        lxc_base.mkdir()
        vm_base.mkdir()
        with patch("kento.create.LXC_BASE", lxc_base), \
             patch("kento.create.VM_BASE", vm_base), \
             patch("kento.create.upper_base",
                   side_effect=lambda n, b=None: (b or lxc_base) / n):
            create("myimage:latest", name="shared", mode="lxc")
            with pytest.raises(SystemExit):
                create("myimage:latest", name="shared", mode="lxc",
                       force=True)

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_force_default_false_still_rejects_cross_namespace(
            self, mock_root, mock_layers, mock_run, tmp_path):
        """Regression: force=False (the default) still rejects cross-namespace."""
        lxc_base = tmp_path / "lxc"
        vm_base = tmp_path / "vm"
        lxc_base.mkdir()
        vm_base.mkdir()
        with patch("kento.create.LXC_BASE", lxc_base), \
             patch("kento.create.VM_BASE", vm_base), \
             patch("kento.create.upper_base",
                   side_effect=lambda n, b=None: (b or lxc_base) / n):
            create("myimage:latest", name="shared", mode="vm")
            with pytest.raises(SystemExit):
                create("myimage:latest", name="shared", mode="lxc",
                       )


class TestStaticIp:
    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_ip_creates_net_file_and_network_unit(self, mock_root, mock_layers,
                                                   mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"), \
             patch("kento._bridge_exists", return_value=True):
            create("myimage:latest", name="test", mode="lxc",
                   net_type="bridge", bridge="lxcbr0",
                   ip="192.168.0.160/22",
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
             patch("kento.create.upper_base", return_value=tmp_path / "test"), \
             patch("kento._bridge_exists", return_value=True):
            create("myimage:latest", name="test", mode="lxc",
                   net_type="bridge", bridge="lxcbr0", ip="10.0.0.5/24")

        unit = (tmp_path / "test" / "upper" / "etc" / "systemd" / "network" /
                "10-static.network").read_text()
        assert "Address=10.0.0.5/24" in unit
        assert "Gateway" not in unit
        assert "DNS" not in unit

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_ip_with_plain_vm_rejected(self, mock_root, mock_layers, tmp_path,
                                        capsys):
        """F10: Plain VM only supports usermode networking (F3), and --ip now
        requires bridge (F10) — so plain VM + --ip is now unreachable and
        should be rejected with a clear message.
        """
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()

        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "test"):
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", mode="vm",
                       ip="192.168.0.160/22", gateway="192.168.0.1")
        err = capsys.readouterr().err
        assert "--ip requires bridge networking" in err

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_ip_with_usermode_rejected(self, mock_root, mock_layers, tmp_path,
                                        capsys):
        """F10: --ip + explicit --network usermode is rejected."""
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()

        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "test"):
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", mode="vm",
                       net_type="usermode", ip="10.0.0.5/24")
        err = capsys.readouterr().err
        assert "--ip requires bridge networking" in err

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_gateway_with_usermode_rejected(self, mock_root, mock_layers,
                                             tmp_path, capsys):
        """F10: --gateway + usermode is rejected (even with --ip)."""
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()

        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "test"):
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", mode="vm",
                       net_type="usermode", ip="10.0.0.5/24",
                       gateway="10.0.0.1")
        err = capsys.readouterr().err
        assert "requires bridge networking" in err

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_ip_with_network_none_rejected(self, mock_root, mock_layers,
                                            mock_run, tmp_path, capsys):
        """F10: --ip + --network none is rejected."""
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", mode="lxc",
                       net_type="none", ip="10.0.0.5/24")
        err = capsys.readouterr().err
        assert "--ip requires bridge networking" in err

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_gateway_without_ip_errors(self, mock_root, mock_layers,
                                        mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", mode="lxc", gateway="192.168.0.1")

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_dns_without_ip_writes_resolved_dropin(self, mock_root, mock_layers,
                                                    mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc", dns="8.8.8.8")

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
            create("myimage:latest", name="test", mode="lxc")

        hostname = (tmp_path / "test" / "upper" / "etc" / "hostname").read_text()
        assert hostname.strip() == "test"

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_searchdomain_in_network_file(self, mock_root, mock_layers,
                                           mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"), \
             patch("kento._bridge_exists", return_value=True):
            create("myimage:latest", name="test", mode="lxc",
                   net_type="bridge", bridge="lxcbr0",
                   ip="10.0.0.5/24", searchdomain="example.com")

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
            create("myimage:latest", name="test", mode="lxc", timezone="Asia/Tokyo")

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
            create("myimage:latest", name="test", mode="lxc", env=["FOO=bar", "BAZ=qux"])

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
    def test_ssh_keys_concatenated(self, mock_root, mock_layers, mock_run, tmp_path):
        """Multiple --ssh-key paths are concatenated into kento-authorized-keys."""
        key1 = tmp_path / "id_rsa.pub"
        key1.write_text("ssh-rsa AAAA user1@host\n")
        key2 = tmp_path / "id_ed25519.pub"
        key2.write_text("ssh-ed25519 BBBB user2@host\n")

        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc",
                   ssh_keys=[str(key1), str(key2)])

        content = (tmp_path / "test" / "kento-authorized-keys").read_text()
        assert "ssh-rsa AAAA user1@host" in content
        assert "ssh-ed25519 BBBB user2@host" in content

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_ssh_keys_missing_file_errors(self, mock_root, mock_layers,
                                            mock_run, tmp_path):
        """Missing --ssh-key path exits 1."""
        missing = tmp_path / "does-not-exist.pub"
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            with pytest.raises(SystemExit) as exc:
                create("myimage:latest", name="test", mode="lxc",
                       ssh_keys=[str(missing)])
            assert exc.value.code == 1
        # Container dir should not have been created yet
        assert not (tmp_path / "test" / "kento-authorized-keys").exists()

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_ssh_keys_none_writes_nothing(self, mock_root, mock_layers,
                                            mock_run, tmp_path):
        """ssh_keys=None means no kento-authorized-keys file."""
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc")

        assert not (tmp_path / "test" / "kento-authorized-keys").exists()

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_ssh_key_user_writes_metadata(self, mock_root, mock_layers,
                                           mock_run, tmp_path):
        """--ssh-key-user droste writes kento-ssh-user file."""
        key = tmp_path / "id.pub"
        key.write_text("ssh-rsa AAAA user@host\n")
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc",
                   ssh_keys=[str(key)], ssh_key_user="droste")

        content = (tmp_path / "test" / "kento-ssh-user").read_text().strip()
        assert content == "droste"

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_ssh_key_user_root_no_file(self, mock_root, mock_layers,
                                        mock_run, tmp_path):
        """--ssh-key-user root (default) does not write kento-ssh-user file."""
        key = tmp_path / "id.pub"
        key.write_text("ssh-rsa AAAA user@host\n")
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc", ssh_keys=[str(key)])

        assert not (tmp_path / "test" / "kento-ssh-user").exists()

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_ssh_key_user_without_keys_still_writes(self, mock_root, mock_layers,
                                                      mock_run, tmp_path):
        """--ssh-key-user without --ssh-key still writes the metadata file."""
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc", ssh_key_user="droste")

        content = (tmp_path / "test" / "kento-ssh-user").read_text().strip()
        assert content == "droste"

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_searchdomain_without_ip_writes_metadata(self, mock_root, mock_layers,
                                                      mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc", searchdomain="example.com")

        net = (tmp_path / "test" / "kento-net").read_text()
        assert "searchdomain=example.com" in net
        # No 10-static.network (no IP)
        assert not (tmp_path / "test" / "upper" / "etc" / "systemd" /
                    "network" / "10-static.network").exists()


class TestSSHHostKeys:
    """Tests for --ssh-host-keys and --ssh-host-key-dir create flags."""

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_ssh_host_keys_generates_keys(self, mock_root, mock_layers,
                                           mock_run, tmp_path):
        """--ssh-host-keys calls ssh-keygen for 3 key types."""
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc", ssh_host_keys=True)

        key_dir = tmp_path / "test" / "ssh-host-keys"
        assert key_dir.is_dir()
        # ssh-keygen was called 3 times (rsa, ecdsa, ed25519) + 1 for lxc-start (not called here)
        # We check via the keygen calls in subprocess.run
        keygen_calls = [c for c in mock_run.call_args_list
                        if c[0][0][0] == "ssh-keygen"]
        assert len(keygen_calls) == 3
        types = [c[0][0][c[0][0].index("-t") + 1] for c in keygen_calls]
        assert sorted(types) == ["ecdsa", "ed25519", "rsa"]
        # RSA call includes -b 4096
        rsa_call = [c for c in keygen_calls if "rsa" in c[0][0]][0]
        assert "-b" in rsa_call[0][0]
        assert "4096" in rsa_call[0][0]
        # All calls include -N ""
        for c in keygen_calls:
            args = c[0][0]
            assert "-N" in args
            n_idx = args.index("-N")
            assert args[n_idx + 1] == ""

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_ssh_host_keys_key_paths(self, mock_root, mock_layers,
                                      mock_run, tmp_path):
        """Generated keys are placed in ssh-host-keys/ with correct filenames."""
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc", ssh_host_keys=True)

        key_dir = tmp_path / "test" / "ssh-host-keys"
        keygen_calls = [c for c in mock_run.call_args_list
                        if c[0][0][0] == "ssh-keygen"]
        for c in keygen_calls:
            args = c[0][0]
            f_idx = args.index("-f")
            key_path = args[f_idx + 1]
            assert key_path.startswith(str(key_dir))

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_ssh_host_key_dir_copies_files(self, mock_root, mock_layers,
                                            mock_run, tmp_path):
        """--ssh-host-key-dir copies ssh_host_* files into container dir."""
        src = tmp_path / "src-keys"
        src.mkdir()
        (src / "ssh_host_rsa_key").write_text("RSA_PRIVATE")
        (src / "ssh_host_rsa_key.pub").write_text("RSA_PUBLIC")
        (src / "ssh_host_ed25519_key").write_text("ED25519_PRIVATE")
        (src / "ssh_host_ed25519_key.pub").write_text("ED25519_PUBLIC")
        (src / "unrelated_file").write_text("IGNORE")

        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc",
                   ssh_host_key_dir=str(src))

        key_dir = tmp_path / "test" / "ssh-host-keys"
        assert key_dir.is_dir()
        assert (key_dir / "ssh_host_rsa_key").read_text() == "RSA_PRIVATE"
        assert (key_dir / "ssh_host_rsa_key.pub").read_text() == "RSA_PUBLIC"
        assert (key_dir / "ssh_host_ed25519_key").read_text() == "ED25519_PRIVATE"
        assert (key_dir / "ssh_host_ed25519_key.pub").read_text() == "ED25519_PUBLIC"
        # unrelated_file is NOT copied (doesn't start with ssh_host_)
        assert not (key_dir / "unrelated_file").exists()

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_ssh_host_key_dir_missing_errors(self, mock_root, mock_layers, tmp_path):
        """--ssh-host-key-dir with missing directory exits 1."""
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            with pytest.raises(SystemExit) as exc:
                create("myimage:latest", name="test", mode="lxc",
                       ssh_host_key_dir=str(tmp_path / "nonexistent"))
            assert exc.value.code == 1

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_ssh_host_key_dir_no_keys_errors(self, mock_root, mock_layers, tmp_path):
        """--ssh-host-key-dir with no ssh_host_*_key files exits 1."""
        src = tmp_path / "empty-keys"
        src.mkdir()
        (src / "some_file.txt").write_text("not a key")

        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            with pytest.raises(SystemExit) as exc:
                create("myimage:latest", name="test", mode="lxc",
                       ssh_host_key_dir=str(src))
            assert exc.value.code == 1

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_no_ssh_host_keys_no_dir(self, mock_root, mock_layers,
                                      mock_run, tmp_path):
        """Without either flag, no ssh-host-keys/ directory is created."""
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc")

        assert not (tmp_path / "test" / "ssh-host-keys").exists()

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_ssh_keygen_not_found_errors(self, mock_root, mock_layers,
                                          mock_run, tmp_path):
        """If ssh-keygen is missing, a clear error is printed."""
        def _side_effect(args, **kwargs):
            if args[0] == "ssh-keygen":
                raise FileNotFoundError("ssh-keygen")
            return MagicMock(returncode=0)
        mock_run.side_effect = _side_effect
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            with pytest.raises(SystemExit) as exc:
                create("myimage:latest", name="test", mode="lxc", ssh_host_keys=True)
            assert exc.value.code == 1


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
        assert (lxc_dir / "kento-inject.sh").is_file()
        assert (lxc_dir / "kento-inject.sh").stat().st_mode & 0o755 == 0o755

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

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_vm_writes_kento_mac(self, mock_root, mock_layers, tmp_path):
        """VM mode writes kento-mac with auto-generated MAC from the container name."""
        from kento.vm import generate_mac, is_valid_mac
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()

        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "test"):
            create("myimage:latest", name="test", mode="vm")

        mac_file = vm_dir / "test" / "kento-mac"
        assert mac_file.is_file()
        mac = mac_file.read_text().strip()
        assert is_valid_mac(mac)
        assert mac == generate_mac("test")

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_vm_mac_override(self, mock_root, mock_layers, tmp_path):
        """--mac override writes the given MAC into kento-mac verbatim."""
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()

        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "test"):
            create("myimage:latest", name="test", mode="vm",
                   mac="aa:bb:cc:dd:ee:ff")

        mac = (vm_dir / "test" / "kento-mac").read_text().strip()
        assert mac == "aa:bb:cc:dd:ee:ff"

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_vm_mac_deterministic_across_recreate(self, mock_root, mock_layers, tmp_path):
        """Same name → same auto-generated MAC."""
        from kento.vm import generate_mac
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()

        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "foo"):
            create("myimage:latest", name="foo", mode="vm")

        mac1 = (vm_dir / "foo" / "kento-mac").read_text().strip()
        assert mac1 == generate_mac("foo")


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
    def test_pve_vm_writes_inject_script(self, mock_root, mock_layers, tmp_path):
        """pve-vm mode writes kento-inject.sh alongside the hookscript."""
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

        inject = vm_dir / "test" / "kento-inject.sh"
        assert inject.is_file()
        assert inject.stat().st_mode & 0o755 == 0o755

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_pve_vm_writes_kento_mac_from_vmid(self, mock_root, mock_layers, tmp_path):
        """pve-vm mode writes kento-mac derived from VMID (not name)."""
        from kento.vm import generate_mac, is_valid_mac
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

        mac_file = vm_dir / "test" / "kento-mac"
        assert mac_file.is_file()
        mac = mac_file.read_text().strip()
        assert is_valid_mac(mac)
        # pve-vm derives MAC from VMID (as string), not name
        assert mac == generate_mac("100")

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_pve_vm_qm_config_includes_mac(self, mock_root, mock_layers, tmp_path):
        """pve-vm mode passes MAC through to the QM net0 line (when bridge used)."""
        from kento.vm import generate_mac
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text(json.dumps({"ids": {}}))

        snippets = tmp_path / "snippets"
        snippets.mkdir()

        written = {}

        def fake_write_qm(vmid, content):
            written["content"] = content
            return Path(f"/etc/pve/qemu-server/{vmid}.conf")

        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "test"), \
             patch("kento.pve.PVE_DIR", pve), \
             patch("kento.vm_hook.find_snippets_dir", return_value=(snippets, "local")), \
             patch("kento.pve.write_qm_config", side_effect=fake_write_qm), \
             patch("kento._bridge_exists", return_value=True):
            create("myimage:latest", name="test", mode="vm",
                   net_type="bridge", bridge="vmbr0")

        expected = generate_mac("100")
        assert f"virtio={expected},bridge=vmbr0" in written["content"]

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


    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_pve_vm_no_snippets_exits_clean(self, mock_root, mock_layers, tmp_path):
        """pve-vm mode exits before writing state if no snippets storage."""
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text(json.dumps({"ids": {}}))

        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "test"), \
             patch("kento.pve.PVE_DIR", pve), \
             patch("kento.vm_hook.find_snippets_dir", side_effect=SystemExit(1)):
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", mode="vm")

        # No state should have been written
        assert not (vm_dir / "test").exists()


class TestPveLxcSnippetsWrapper:
    """pve-lxc routes port/memory/cores through a PVE snippets hookscript
    (PVE strips lxc.hook.start-host, so the legacy hook path is dead)."""

    @patch("kento.vm._port_is_free", return_value=True)
    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_pve_lxc_port_writes_snippets_wrapper(self, mock_root, mock_layers,
                                                   mock_run, mock_free, tmp_path):
        """pve-lxc with --port writes a snippets wrapper + emits hookscript."""
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text(json.dumps({"ids": {}}))
        snippets = tmp_path / "snippets"
        snippets.mkdir()
        pve_conf = tmp_path / "pve-conf" / "100.conf"
        pve_conf.parent.mkdir()

        def fake_write(vmid, content):
            pve_conf.write_text(content)
            return pve_conf

        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "100"), \
             patch("kento.pve.PVE_DIR", pve), \
             patch("kento._bridge_exists", return_value=True), \
             patch("kento.vm_hook.find_snippets_dir",
                   return_value=(snippets, "local")), \
             patch("kento.pve.write_pve_config", side_effect=fake_write):
            create("myimage:latest", name="test", mode="pve",
                   port="10205:22", net_type="bridge", bridge="vmbr0")

        wrapper = snippets / "kento-lxc-100.sh"
        assert wrapper.is_file()
        assert wrapper.stat().st_mode & 0o755 == 0o755
        cfg = pve_conf.read_text()
        assert "hookscript: local:snippets/kento-lxc-100.sh" in cfg
        assert "lxc.hook.start-host" not in cfg

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_pve_lxc_memory_writes_snippets_wrapper(self, mock_root, mock_layers,
                                                     mock_run, tmp_path):
        """pve-lxc with --memory writes a snippets wrapper + emits hookscript."""
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text(json.dumps({"ids": {}}))
        snippets = tmp_path / "snippets"
        snippets.mkdir()
        pve_conf = tmp_path / "pve-conf" / "100.conf"
        pve_conf.parent.mkdir()

        def fake_write(vmid, content):
            pve_conf.write_text(content)
            return pve_conf

        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "100"), \
             patch("kento.pve.PVE_DIR", pve), \
             patch("kento.vm_hook.find_snippets_dir",
                   return_value=(snippets, "local")), \
             patch("kento.pve.write_pve_config", side_effect=fake_write):
            create("myimage:latest", name="test", mode="pve", memory=512)

        wrapper = snippets / "kento-lxc-100.sh"
        assert wrapper.is_file()
        cfg = pve_conf.read_text()
        assert "hookscript: local:snippets/kento-lxc-100.sh" in cfg
        assert "lxc.hook.start-host" not in cfg

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_pve_lxc_cores_writes_snippets_wrapper(self, mock_root, mock_layers,
                                                    mock_run, tmp_path):
        """pve-lxc with --cores writes a snippets wrapper + emits hookscript."""
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text(json.dumps({"ids": {}}))
        snippets = tmp_path / "snippets"
        snippets.mkdir()
        pve_conf = tmp_path / "pve-conf" / "100.conf"
        pve_conf.parent.mkdir()

        def fake_write(vmid, content):
            pve_conf.write_text(content)
            return pve_conf

        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "100"), \
             patch("kento.pve.PVE_DIR", pve), \
             patch("kento.vm_hook.find_snippets_dir",
                   return_value=(snippets, "local")), \
             patch("kento.pve.write_pve_config", side_effect=fake_write):
            create("myimage:latest", name="test", mode="pve", cores=2)

        wrapper = snippets / "kento-lxc-100.sh"
        assert wrapper.is_file()
        cfg = pve_conf.read_text()
        assert "hookscript: local:snippets/kento-lxc-100.sh" in cfg
        assert "lxc.hook.start-host" not in cfg

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_pve_lxc_no_resource_flags_no_wrapper(self, mock_root, mock_layers,
                                                    mock_run, tmp_path):
        """pve-lxc without port/memory/cores: no wrapper, no hookscript line."""
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text(json.dumps({"ids": {}}))
        snippets = tmp_path / "snippets"
        snippets.mkdir()
        pve_conf = tmp_path / "pve-conf" / "100.conf"
        pve_conf.parent.mkdir()

        def fake_write(vmid, content):
            pve_conf.write_text(content)
            return pve_conf

        # find_snippets_dir must NOT be called in the no-flag path — if the
        # implementation regresses and calls it anyway, the side_effect will
        # fail the test.
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "100"), \
             patch("kento.pve.PVE_DIR", pve), \
             patch("kento.vm_hook.find_snippets_dir",
                   side_effect=AssertionError("find_snippets_dir should not be called")), \
             patch("kento.pve.write_pve_config", side_effect=fake_write):
            create("myimage:latest", name="test", mode="pve")

        wrapper = snippets / "kento-lxc-100.sh"
        assert not wrapper.exists()
        cfg = pve_conf.read_text()
        assert "hookscript:" not in cfg
        assert "lxc.hook.start-host" not in cfg

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_pve_lxc_no_snippets_exits_clean(self, mock_root, mock_layers,
                                              tmp_path):
        """pve-lxc with resource flags fails early if no snippets storage."""
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text(json.dumps({"ids": {}}))

        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "100"), \
             patch("kento.pve.PVE_DIR", pve), \
             patch("kento.vm_hook.find_snippets_dir", side_effect=SystemExit(1)):
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", mode="pve", memory=512)

        # No state should have been written
        assert not (tmp_path / "100").exists()


class TestLxcPortForwarding:
    """Tests for --port in LXC/PVE modes (Phase 3: nftables DNAT)."""

    @patch("kento.vm._port_is_free", return_value=True)
    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_lxc_port_writes_kento_port(self, mock_root, mock_layers,
                                         mock_run, mock_free, tmp_path):
        """create(mode=lxc, port=10022:22, bridge) writes kento-port file."""
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"), \
             patch("kento._bridge_exists", return_value=True):
            create("myimage:latest", name="test", mode="lxc",
                   port="10022:22", net_type="bridge", bridge="lxcbr0")

        port_file = tmp_path / "test" / "kento-port"
        assert port_file.is_file()
        assert port_file.read_text().strip() == "10022:22"

    @patch("kento.vm._port_is_free", return_value=True)
    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_lxc_config_has_start_host_hook(self, mock_root, mock_layers,
                                             mock_run, mock_free, tmp_path):
        """When port is set, LXC config includes lxc.hook.start-host."""
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"), \
             patch("kento._bridge_exists", return_value=True):
            create("myimage:latest", name="test", mode="lxc",
                   port="10022:22", net_type="bridge", bridge="lxcbr0")

        cfg = (tmp_path / "test" / "config").read_text()
        assert "lxc.hook.start-host" in cfg

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_lxc_config_omits_start_host_no_port(self, mock_root, mock_layers,
                                                   mock_run, tmp_path):
        """Without port, LXC config does NOT include lxc.hook.start-host."""
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc")

        cfg = (tmp_path / "test" / "config").read_text()
        assert "lxc.hook.start-host" not in cfg

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_lxc_port_requires_bridge(self, mock_root, mock_layers, tmp_path):
        """--port with net_type=none errors for LXC mode."""
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", mode="lxc",
                       port="10022:22", net_type="none")

    @patch("kento.vm._port_is_free", return_value=True)
    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_lxc_port_with_bridge_ok(self, mock_root, mock_layers,
                                      mock_run, mock_free, tmp_path):
        """--port + --network bridge is valid for LXC mode."""
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"), \
             patch("kento._bridge_exists", return_value=True):
            # Should not raise
            create("myimage:latest", name="test", mode="lxc",
                   port="10022:22", net_type="bridge", bridge="lxcbr0")

        assert (tmp_path / "test" / "kento-port").is_file()

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_vm_port_with_bridge_errors(self, mock_root, mock_layers, tmp_path):
        """--port + --network bridge is invalid for VM mode."""
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()
        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "test"), \
             patch("kento._bridge_exists", return_value=True):
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", mode="vm",
                       port="10022:22", net_type="bridge", bridge="vmbr0")

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_vm_bridge_named_rejected(self, mock_root, mock_layers,
                                       tmp_path, capsys):
        """Plain VM + --network bridge=<name> is rejected (F3: start_vm has
        no bridge/tap support, silently boots with zero NICs otherwise)."""
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()
        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "test"), \
             patch("kento._bridge_exists", return_value=True):
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", mode="vm",
                       net_type="bridge", bridge="vmbr0")
        err = capsys.readouterr().err
        assert "plain VM mode does not support bridge networking" in err
        # Guidance must lead with usermode (most users aren't on PVE).
        assert "usermode" in err

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_vm_bridge_autodetect_rejected(self, mock_root, mock_layers,
                                            tmp_path, capsys):
        """Plain VM + --network bridge (no name, auto-detect) is rejected.

        In practice resolve_network() already defaults plain VM to usermode
        when net_type is None (see 9bb2eb6), so to hit this path we have to
        pass net_type='bridge' explicitly with no bridge name and rely on
        bridge auto-detection. Either way, a resolved type of 'bridge' must
        be rejected for plain VM mode.
        """
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()
        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "test"), \
             patch("kento.detect_bridge", return_value="lxcbr0"), \
             patch("kento._bridge_exists", return_value=True):
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", mode="vm",
                       net_type="bridge")
        err = capsys.readouterr().err
        assert "plain VM mode does not support bridge networking" in err
        assert "usermode" in err

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_vm_bridge_with_port_still_rejected(self, mock_root, mock_layers,
                                                 tmp_path, capsys):
        """Plain VM + bridge + port: whichever check fires first, exit 1.

        The new F3 block fires before the existing bridge+port block, but
        either way the user must see an error.
        """
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()
        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "test"), \
             patch("kento._bridge_exists", return_value=True):
            with pytest.raises(SystemExit):
                create("myimage:latest", name="test", mode="vm",
                       port="10022:22", net_type="bridge", bridge="vmbr0")

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_pve_vm_bridge_not_rejected(self, mock_root, mock_layers, tmp_path):
        """pve-vm + bridge is valid (qm handles bridged VMs natively).

        Uses mode='vm' + patched PVE_DIR so auto-detection promotes to pve-vm
        (matches the pattern of the existing pve-vm tests in this file)."""
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
             patch("kento.vm_hook.find_snippets_dir",
                   return_value=(snippets, "local")), \
             patch("kento.pve.write_qm_config",
                   return_value=Path("/etc/pve/qemu-server/100.conf")), \
             patch("kento._bridge_exists", return_value=True):
            # Should not raise — pve-vm supports bridge via qm.
            create("myimage:latest", name="test", mode="vm",
                   net_type="bridge", bridge="vmbr0")

    @patch("kento.vm._port_is_free", return_value=True)
    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_vm_usermode_not_rejected(self, mock_root, mock_layers, mock_run,
                                       mock_free, tmp_path):
        """Plain VM + --network usermode is the good path, must not raise."""
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()
        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "test"):
            create("myimage:latest", name="test", mode="vm",
                   net_type="usermode")

        assert (vm_dir / "test" / "kento-port").is_file()

    @patch("kento.vm._port_is_free", return_value=True)
    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_lxc_port_auto_allocates(self, mock_root, mock_layers,
                                      mock_run, mock_free, tmp_path):
        """--port auto allocates a port and defaults guest to 22."""
        lxc_base = tmp_path / "lxc"
        lxc_base.mkdir()
        vm_base = tmp_path / "vm"
        vm_base.mkdir()
        with patch("kento.create.LXC_BASE", lxc_base), \
             patch("kento.create.upper_base", return_value=lxc_base / "test"), \
             patch("kento._bridge_exists", return_value=True), \
             patch("kento.vm.VM_BASE", vm_base), \
             patch("kento.LXC_BASE", lxc_base):
            create("myimage:latest", name="test", mode="lxc",
                   port="auto", net_type="bridge", bridge="lxcbr0")

        port = (lxc_base / "test" / "kento-port").read_text().strip()
        assert port == "10022:22"


class TestCloudInitMode:
    """Tests for --config-mode cloud-init integration in create."""

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers")
    @patch("kento.create.require_root")
    def test_cloudinit_mode_auto_detected(self, mock_root, mock_layers,
                                           mock_run, tmp_path):
        """Auto-detect cloudinit mode when image has cloud-init."""
        # Create layer dir with cloud-init marker
        layer = tmp_path / "layer"
        (layer / "usr" / "bin").mkdir(parents=True)
        (layer / "usr" / "bin" / "cloud-init").write_text("")
        mock_layers.return_value = str(layer)

        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc")

        mode_file = tmp_path / "test" / "kento-config-mode"
        assert mode_file.is_file()
        assert mode_file.read_text().strip() == "cloudinit"

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_injection_mode_when_no_cloudinit(self, mock_root, mock_layers,
                                              mock_run, tmp_path):
        """Default to injection mode when image lacks cloud-init."""
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc")

        mode_file = tmp_path / "test" / "kento-config-mode"
        assert mode_file.is_file()
        assert mode_file.read_text().strip() == "injection"

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers")
    @patch("kento.create.require_root")
    def test_config_mode_forced_injection(self, mock_root, mock_layers,
                                           mock_run, tmp_path):
        """Forced injection mode even when cloud-init is present."""
        layer = tmp_path / "layer"
        (layer / "usr" / "bin").mkdir(parents=True)
        (layer / "usr" / "bin" / "cloud-init").write_text("")
        mock_layers.return_value = str(layer)

        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc", config_mode="injection")

        mode_file = tmp_path / "test" / "kento-config-mode"
        assert mode_file.read_text().strip() == "injection"

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers")
    @patch("kento.create.require_root")
    def test_cloudinit_writes_seed_dir(self, mock_root, mock_layers,
                                       mock_run, tmp_path):
        """Cloud-init mode creates cloud-seed/ with meta-data and user-data."""
        layer = tmp_path / "layer"
        (layer / "etc" / "cloud").mkdir(parents=True)
        (layer / "etc" / "cloud" / "cloud.cfg").write_text("")
        mock_layers.return_value = str(layer)

        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc", timezone="UTC",
                   env=["FOO=bar"])

        seed_dir = tmp_path / "test" / "cloud-seed"
        assert seed_dir.is_dir()
        assert (seed_dir / "meta-data").is_file()
        assert (seed_dir / "user-data").is_file()
        # Verify content
        user_data = (seed_dir / "user-data").read_text()
        assert "#cloud-config" in user_data
        assert "timezone: UTC" in user_data
        assert "FOO=bar" in user_data

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_injection_mode_no_seed_dir(self, mock_root, mock_layers,
                                         mock_run, tmp_path):
        """Injection mode does not create cloud-seed/ directory."""
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc", config_mode="injection")

        assert not (tmp_path / "test" / "cloud-seed").exists()

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_cloudinit_forced_without_cloud_init_errors(self, mock_root, mock_layers,
                                                         mock_run, tmp_path, capsys):
        """F14: forcing cloudinit mode without cloud-init in image is a hard error."""
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            with pytest.raises(SystemExit) as exc:
                create("myimage:latest", name="test", mode="lxc",
                       config_mode="cloudinit")
            assert exc.value.code == 1

        captured = capsys.readouterr()
        assert "requires cloud-init" in captured.err
        assert "--config-mode injection" in captured.err
        # Half-created container is cleaned up — no orphaned dir left behind.
        assert not (tmp_path / "test").exists()

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers")
    @patch("kento.create.require_root")
    def test_cloudinit_forced_with_cloud_init_succeeds(self, mock_root, mock_layers,
                                                        mock_run, tmp_path):
        """F14 counter-test: explicit cloudinit + cloud-init in image is fine."""
        layer = tmp_path / "layer"
        (layer / "usr" / "bin").mkdir(parents=True)
        (layer / "usr" / "bin" / "cloud-init").write_text("")
        mock_layers.return_value = str(layer)

        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc",
                   config_mode="cloudinit")

        mode_file = tmp_path / "test" / "kento-config-mode"
        assert mode_file.read_text().strip() == "cloudinit"
        assert (tmp_path / "test" / "cloud-seed").is_dir()


class TestGenerateConfigMemoryCores:
    """Tests for --memory and --cores in generate_config (plain LXC)."""

    def test_memory_adds_cgroup_line(self, tmp_path):
        cfg = generate_config("test", tmp_path, memory=512)
        assert "lxc.cgroup2.memory.max = 536870912" in cfg

    def test_cores_adds_cgroup_line(self, tmp_path):
        cfg = generate_config("test", tmp_path, cores=2)
        assert "lxc.cgroup2.cpu.max = 200000 100000" in cfg

    def test_memory_and_cores(self, tmp_path):
        cfg = generate_config("test", tmp_path, memory=1024, cores=4)
        assert "lxc.cgroup2.memory.max = 1073741824" in cfg
        assert "lxc.cgroup2.cpu.max = 400000 100000" in cfg

    def test_no_memory_no_cores(self, tmp_path):
        cfg = generate_config("test", tmp_path)
        assert "cgroup2.memory.max" not in cfg
        assert "cgroup2.cpu.max" not in cfg

    def test_memory_none_omitted(self, tmp_path):
        cfg = generate_config("test", tmp_path, memory=None)
        assert "cgroup2.memory.max" not in cfg

    def test_cores_none_omitted(self, tmp_path):
        cfg = generate_config("test", tmp_path, cores=None)
        assert "cgroup2.cpu.max" not in cfg


class TestLxcCreateMemoryCores:
    """Tests for --memory and --cores in LXC create."""

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_lxc_memory_in_config(self, mock_root, mock_layers, mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc", memory=256)

        cfg = (tmp_path / "test" / "config").read_text()
        assert "lxc.cgroup2.memory.max = 268435456" in cfg

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_lxc_cores_in_config(self, mock_root, mock_layers, mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc", cores=2)

        cfg = (tmp_path / "test" / "config").read_text()
        assert "lxc.cgroup2.cpu.max = 200000 100000" in cfg

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_lxc_no_memory_cores_by_default(self, mock_root, mock_layers, mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc")

        cfg = (tmp_path / "test" / "config").read_text()
        assert "cgroup2.memory.max" not in cfg
        assert "cgroup2.cpu.max" not in cfg

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_lxc_writes_memory_metadata_for_start_host_hook(
            self, mock_root, mock_layers, mock_run, tmp_path):
        """kento-memory is read by the start-host hook to propagate the limit
        into PVE-LXC's inner `ns` cgroup. Must be written at create time."""
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc", memory=256)

        assert (tmp_path / "test" / "kento-memory").read_text().strip() == "256"

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_lxc_writes_cores_metadata_for_start_host_hook(
            self, mock_root, mock_layers, mock_run, tmp_path):
        """Same as memory — cores metadata needed for ns-cgroup propagation."""
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc", cores=2)

        assert (tmp_path / "test" / "kento-cores").read_text().strip() == "2"

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_lxc_no_metadata_files_without_limits(
            self, mock_root, mock_layers, mock_run, tmp_path):
        """When --memory/--cores are not passed, don't write placeholder files
        that would fool the hook into writing 0-byte limits."""
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc")

        assert not (tmp_path / "test" / "kento-memory").exists()
        assert not (tmp_path / "test" / "kento-cores").exists()


class TestVmCreateMemoryCores:
    """Tests for --memory and --cores metadata files in VM create."""

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_vm_default_memory_cores_metadata(self, mock_root, mock_layers, tmp_path):
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()

        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "test"):
            create("myimage:latest", name="test", mode="vm")

        assert (vm_dir / "test" / "kento-memory").read_text().strip() == "512"
        assert (vm_dir / "test" / "kento-cores").read_text().strip() == "1"

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_vm_custom_memory_cores_metadata(self, mock_root, mock_layers, tmp_path):
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()

        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "test"):
            create("myimage:latest", name="test", mode="vm", memory=2048, cores=4)

        assert (vm_dir / "test" / "kento-memory").read_text().strip() == "2048"
        assert (vm_dir / "test" / "kento-cores").read_text().strip() == "4"

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_vm_memory_only_defaults_cores(self, mock_root, mock_layers, tmp_path):
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()

        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "test"):
            create("myimage:latest", name="test", mode="vm", memory=1024)

        assert (vm_dir / "test" / "kento-memory").read_text().strip() == "1024"
        assert (vm_dir / "test" / "kento-cores").read_text().strip() == "1"

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_lxc_no_memory_cores_metadata(self, mock_root, mock_layers, tmp_path):
        """LXC mode does not write kento-memory/kento-cores metadata files."""
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc")

        assert not (tmp_path / "test" / "kento-memory").exists()
        assert not (tmp_path / "test" / "kento-cores").exists()


class TestPveAutopromote:
    """PVE auto-promotion smoke test.

    On a PVE host, `kento lxc create foo` (no flags) must flip mode to pve.
    Regression coverage for F1 (edge-case audit 2026-04-23): the former
    --unconfined gate used to run before auto-promotion resolution, aborting
    on PVE hosts even when auto-detection would have promoted to pve.
    """

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_pve_host_autopromote(self, mock_root, mock_layers,
                                    mock_run, tmp_path, capsys):
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
            create("myimage:latest", name="test", mode="lxc")

        lxc_dir = tmp_path / "100"
        assert (lxc_dir / "kento-mode").read_text().strip() == "pve"


class TestCreateRollback:
    """F4: create() must roll back partial state if any post-mkdir step fails.

    Exception raised anywhere after the initial ``container_dir.mkdir`` should
    leave no orphan state behind — no container_dir, no external state_dir,
    no image-hold podman container, no PVE config file, no snippets wrapper.
    Re-raise the original exception so the user sees what failed.
    """

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_lxc_rollback_cleans_container_dir(self, mock_root, mock_layers,
                                                mock_run, tmp_path):
        """LXC mode: if write_hook explodes, container_dir and state_dir are gone."""
        state_dir = tmp_path / "state" / "test"

        def boom(*args, **kwargs):
            raise RuntimeError("simulated hook failure")

        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=state_dir), \
             patch("kento.create.write_hook", side_effect=boom):
            with pytest.raises(RuntimeError, match="simulated hook failure"):
                create("myimage:latest", name="test", mode="lxc",
                       )

        # container_dir is gone
        assert not (tmp_path / "test").exists()
        # separate state_dir is gone
        assert not state_dir.exists()
        # image-hold removal was invoked (podman rm kento-hold.test)
        rm_calls = [c for c in mock_run.call_args_list
                    if len(c[0][0]) >= 3 and c[0][0][1] == "rm"
                    and c[0][0][2] == "kento-hold.test"]
        assert rm_calls, "remove_image_hold was not called on rollback"

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_pve_rollback_cleans_pve_config_and_wrapper(
            self, mock_root, mock_layers, mock_run, tmp_path):
        """pve mode: write_pve_config explodes after wrapper+hook written."""
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text(json.dumps({"ids": {}}))
        snippets = tmp_path / "snippets"
        snippets.mkdir()
        state_dir = tmp_path / "state" / "100"

        def boom(*args, **kwargs):
            raise RuntimeError("pmxcfs is read-only")

        deleted = {"pve_config": 0, "wrapper": 0}

        def count_delete_pve(vmid):
            deleted["pve_config"] += 1

        def count_delete_wrapper(vmid):
            deleted["wrapper"] += 1

        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=state_dir), \
             patch("kento.pve.PVE_DIR", pve), \
             patch("kento._bridge_exists", return_value=True), \
             patch("kento.vm_hook.find_snippets_dir",
                   return_value=(snippets, "local")), \
             patch("kento.pve.write_pve_config", side_effect=boom), \
             patch("kento.pve.delete_pve_config", side_effect=count_delete_pve), \
             patch("kento.lxc_hook.delete_lxc_snippets_wrapper",
                   side_effect=count_delete_wrapper):
            with pytest.raises(RuntimeError, match="pmxcfs is read-only"):
                create("myimage:latest", name="test", mode="pve",
                       port="10205:22", net_type="bridge", bridge="vmbr0")

        # Snippets wrapper was written (before write_pve_config blew up) and
        # must be torn down.
        assert deleted["wrapper"] == 1, \
            "delete_lxc_snippets_wrapper was not called on rollback"
        # write_pve_config raised before writing, but the undo is registered
        # AFTER a successful write, so delete_pve_config should NOT run here.
        assert deleted["pve_config"] == 0, \
            "delete_pve_config must not run for a write that never happened"
        assert not (tmp_path / "100").exists()
        assert not state_dir.exists()

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_vm_rollback_cleans_container_dir(self, mock_root, mock_layers, tmp_path):
        """plain vm mode: explode in write_inject; container_dir cleaned."""
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()
        state_dir = tmp_path / "state" / "test"

        def boom(*args, **kwargs):
            raise RuntimeError("disk full")

        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=state_dir), \
             patch("kento.create.write_inject", side_effect=boom):
            with pytest.raises(RuntimeError, match="disk full"):
                create("myimage:latest", name="test", mode="vm")

        assert not (vm_dir / "test").exists()
        assert not state_dir.exists()

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_pve_vm_rollback_cleans_qm_config_and_wrapper(
            self, mock_root, mock_layers, tmp_path):
        """pve-vm mode: write_qm_config explodes; snippets wrapper + container
        state are all cleaned. delete_qm_config is NOT called because the
        write never succeeded (undo is only registered after a successful write)."""
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()
        pve = tmp_path / "pve"
        pve.mkdir()
        (pve / ".vmlist").write_text(json.dumps({"ids": {}}))
        snippets = tmp_path / "snippets"
        snippets.mkdir()
        state_dir = tmp_path / "state" / "test"

        deleted = {"wrapper": 0, "qm": 0}

        def count_delete_wrapper(vmid):
            deleted["wrapper"] += 1

        def count_delete_qm(vmid):
            deleted["qm"] += 1

        def boom(*args, **kwargs):
            raise RuntimeError("qm rejects config")

        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=state_dir), \
             patch("kento.pve.PVE_DIR", pve), \
             patch("kento.vm_hook.find_snippets_dir",
                   return_value=(snippets, "local")), \
             patch("kento.pve.write_qm_config", side_effect=boom), \
             patch("kento.vm_hook.delete_snippets_wrapper",
                   side_effect=count_delete_wrapper), \
             patch("kento.pve.delete_qm_config", side_effect=count_delete_qm):
            with pytest.raises(RuntimeError, match="qm rejects config"):
                create("myimage:latest", name="test", mode="vm")

        assert deleted["wrapper"] == 1, \
            "delete_snippets_wrapper was not called on rollback"
        assert deleted["qm"] == 0, \
            "delete_qm_config must not run for a write that never happened"
        assert not (vm_dir / "test").exists()
        assert not state_dir.exists()

    @patch("kento.create.require_root")
    def test_early_failure_no_rollback_needed(self, mock_root, tmp_path):
        """Failure BEFORE any side-effects (e.g. missing ssh key file) must
        not attempt rollback — there's nothing to roll back."""
        missing = tmp_path / "absent.pub"
        with patch("kento.create.LXC_BASE", tmp_path):
            with pytest.raises(SystemExit) as exc:
                create("myimage:latest", name="test", mode="lxc",
                       ssh_keys=[str(missing)])
        assert exc.value.code == 1
        # Nothing got created.
        assert not (tmp_path / "test").exists()

    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_start_failure_rolls_back_and_stops(self, mock_root, mock_layers,
                                                  tmp_path, capsys):
        """--start path: lxc-start fails after config is written. Rollback
        must stop the (partially-started) container AND clean container_dir.

        F8: a failed lxc-start must surface a clean kento error, not a bare
        CalledProcessError traceback. _run_start_or_rollback raises
        RuntimeError which the surrounding try/except catches for rollback.
        """
        state_dir = tmp_path / "state" / "test"

        stop_calls = []
        start_called = []

        def fake_run(cmd, *args, **kwargs):
            if cmd[0] == "lxc-start":
                start_called.append(cmd)
                return subprocess.CompletedProcess(cmd, 1, stdout="",
                                                   stderr="lxc-start: boot failed")
            if cmd[0] == "lxc-stop":
                stop_calls.append(cmd)
            # podman / other subprocess: stub
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=state_dir), \
             patch("kento.create.subprocess.run", side_effect=fake_run):
            with pytest.raises(RuntimeError) as exc:
                create("myimage:latest", name="test", mode="lxc",
                       start=True)
            assert "failed to start test" in str(exc.value)
            assert "kento lxc start test" in str(exc.value) \
                or "kento lxc destroy test" in str(exc.value)

        assert start_called, "lxc-start was not invoked"
        assert stop_calls, "lxc-stop was not called on rollback"
        # Container state is fully cleaned
        assert not (tmp_path / "test").exists()
        assert not state_dir.exists()

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_rollback_preserves_original_exception(self, mock_root, mock_layers,
                                                     mock_run, tmp_path):
        """When a cleanup helper itself errors, the original exception still
        reaches the caller — cleanup failures must not mask the root cause."""
        def boom(*args, **kwargs):
            raise RuntimeError("original failure")

        def cleanup_boom(*args, **kwargs):
            raise OSError("cleanup also failed")

        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base",
                   return_value=tmp_path / "state" / "test"), \
             patch("kento.create.write_hook", side_effect=boom), \
             patch("kento.layers.remove_image_hold", side_effect=cleanup_boom):
            with pytest.raises(RuntimeError, match="original failure"):
                create("myimage:latest", name="test", mode="lxc",
                       )
