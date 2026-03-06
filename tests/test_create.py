"""Tests for container creation."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from kento.create import create, generate_config


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

    def test_nesting_disabled(self, tmp_path):
        cfg = generate_config("test", tmp_path, nesting=False)
        assert "nesting.conf" not in cfg


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
             patch("kento.pve.write_pve_config", side_effect=fake_write):
            create("myimage:latest", name="test", mode="pve")

        pve_cfg = pve_conf.read_text()
        assert "bridge=vmbr0" in pve_cfg

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_lxc_bridge_default(self, mock_root, mock_layers,
                                 mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path), \
             patch("kento.create.upper_base", return_value=tmp_path / "test"):
            create("myimage:latest", name="test", mode="lxc")

        cfg = (tmp_path / "test" / "config").read_text()
        assert "lxc.net.0.link = lxcbr0" in cfg

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
