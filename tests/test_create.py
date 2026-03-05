"""Tests for container creation."""

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
        with patch("kento.create.LXC_BASE", tmp_path):
            create("test", "myimage:latest")

        lxc_dir = tmp_path / "test"
        assert (lxc_dir / "rootfs").is_dir()
        assert (lxc_dir / "upper").is_dir()
        assert (lxc_dir / "work").is_dir()
        assert (lxc_dir / "config").is_file()
        assert (lxc_dir / "kento-hook").is_file()
        assert (lxc_dir / "kento-image").read_text().strip() == "myimage:latest"
        assert (lxc_dir / "kento-layers").read_text().strip() == "/a:/b"

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_refuses_existing_container(self, mock_root, mock_layers,
                                         mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path):
            create("test", "myimage:latest")
            with pytest.raises(SystemExit):
                create("test", "myimage:latest")

    @patch("kento.create.subprocess.run")
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_start_calls_lxc_start(self, mock_root, mock_layers,
                                    mock_run, tmp_path):
        with patch("kento.create.LXC_BASE", tmp_path):
            create("test", "myimage:latest", start=True)

        mock_run.assert_called_once_with(
            ["lxc-start", "-n", "test"], check=True,
        )
