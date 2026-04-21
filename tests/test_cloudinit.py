"""Tests for cloud-init NoCloud seed generation."""

from pathlib import Path

import pytest

from kento.cloudinit import (
    compute_instance_id,
    detect_cloudinit,
    generate_meta_data,
    generate_network_config,
    generate_user_data,
    write_seed,
)


class TestDetectCloudinit:
    def test_detects_cloud_init_binary(self, tmp_path):
        layer = tmp_path / "layer1"
        (layer / "usr" / "bin").mkdir(parents=True)
        (layer / "usr" / "bin" / "cloud-init").write_text("")
        assert detect_cloudinit(str(layer)) is True

    def test_detects_cloud_cfg(self, tmp_path):
        layer = tmp_path / "layer1"
        (layer / "etc" / "cloud").mkdir(parents=True)
        (layer / "etc" / "cloud" / "cloud.cfg").write_text("")
        assert detect_cloudinit(str(layer)) is True

    def test_no_cloud_init(self, tmp_path):
        layer = tmp_path / "layer1"
        layer.mkdir()
        assert detect_cloudinit(str(layer)) is False

    def test_multi_layer_detection(self, tmp_path):
        layer1 = tmp_path / "layer1"
        layer1.mkdir()
        layer2 = tmp_path / "layer2"
        (layer2 / "usr" / "bin").mkdir(parents=True)
        (layer2 / "usr" / "bin" / "cloud-init").write_text("")
        layers_str = f"{layer1}:{layer2}"
        assert detect_cloudinit(layers_str) is True

    def test_multi_layer_no_cloud_init(self, tmp_path):
        layer1 = tmp_path / "layer1"
        layer1.mkdir()
        layer2 = tmp_path / "layer2"
        layer2.mkdir()
        layers_str = f"{layer1}:{layer2}"
        assert detect_cloudinit(layers_str) is False


class TestComputeInstanceId:
    def test_deterministic(self):
        id1 = compute_instance_id("test", ip="10.0.0.1/24", timezone="UTC")
        id2 = compute_instance_id("test", ip="10.0.0.1/24", timezone="UTC")
        assert id1 == id2

    def test_changes_on_config_change(self):
        id1 = compute_instance_id("test", ip="10.0.0.1/24")
        id2 = compute_instance_id("test", ip="10.0.0.2/24")
        assert id1 != id2

    def test_changes_on_name_change(self):
        id1 = compute_instance_id("foo")
        id2 = compute_instance_id("bar")
        assert id1 != id2

    def test_format(self):
        iid = compute_instance_id("test", timezone="Europe/Berlin")
        assert iid.startswith("kento-")
        hex_part = iid[len("kento-"):]
        assert len(hex_part) == 16
        # Verify it's valid hex
        int(hex_part, 16)

    def test_env_sorted(self):
        """Env list order does not affect the hash."""
        id1 = compute_instance_id("test", env=["A=1", "B=2"])
        id2 = compute_instance_id("test", env=["B=2", "A=1"])
        assert id1 == id2


class TestGenerateMetaData:
    def test_contains_instance_id(self):
        result = generate_meta_data("myhost", "kento-abc123")
        assert "instance-id: kento-abc123" in result

    def test_contains_hostname(self):
        result = generate_meta_data("myhost", "kento-abc123")
        assert "local-hostname: myhost" in result

    def test_ends_with_newline(self):
        result = generate_meta_data("host", "kento-id")
        assert result.endswith("\n")


class TestGenerateUserData:
    def test_cloud_config_header(self):
        result = generate_user_data()
        assert result.startswith("#cloud-config")

    def test_timezone(self):
        result = generate_user_data(timezone="America/New_York")
        assert "timezone: America/New_York" in result

    def test_ssh_keys_root(self):
        result = generate_user_data(ssh_keys="ssh-rsa AAAA user@host\n")
        assert "ssh_authorized_keys:" in result
        assert "  - ssh-rsa AAAA user@host" in result
        assert "users:" not in result

    def test_ssh_keys_user(self):
        result = generate_user_data(ssh_keys="ssh-rsa AAAA user@host\n",
                                    ssh_key_user="droste")
        assert "users:" in result
        assert "  - name: droste" in result
        assert "    ssh_authorized_keys:" in result
        assert "      - ssh-rsa AAAA user@host" in result
        assert "ssh_authorized_keys:" not in result.split("users:")[0]

    def test_ssh_keys_multiple(self):
        keys = "ssh-rsa AAAA user1@host\nssh-ed25519 BBBB user2@host\n"
        result = generate_user_data(ssh_keys=keys)
        assert "  - ssh-rsa AAAA user1@host" in result
        assert "  - ssh-ed25519 BBBB user2@host" in result

    def test_ssh_keys_skips_comments_and_blanks(self):
        keys = "# comment\n\nssh-rsa AAAA user@host\n"
        result = generate_user_data(ssh_keys=keys)
        assert "# comment" not in result
        assert "  - ssh-rsa AAAA user@host" in result

    def test_env_write_files(self):
        result = generate_user_data(env=["FOO=bar", "BAZ=qux"])
        assert "write_files:" in result
        assert "  - path: /etc/environment" in result
        assert "      FOO=bar" in result
        assert "      BAZ=qux" in result

    def test_ssh_host_keys(self, tmp_path):
        key_dir = tmp_path / "keys"
        key_dir.mkdir()
        (key_dir / "ssh_host_rsa_key").write_text("-----BEGIN RSA PRIVATE KEY-----\nfoo\n-----END RSA PRIVATE KEY-----\n")
        (key_dir / "ssh_host_rsa_key.pub").write_text("ssh-rsa AAAA host")
        (key_dir / "ssh_host_ed25519_key").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nbar\n-----END OPENSSH PRIVATE KEY-----\n")
        (key_dir / "ssh_host_ed25519_key.pub").write_text("ssh-ed25519 BBBB host")

        result = generate_user_data(ssh_host_key_dir=key_dir)
        assert "ssh_keys:" in result
        assert "  rsa_private: |" in result
        assert "  rsa_public: ssh-rsa AAAA host" in result
        assert "  ed25519_private: |" in result
        assert "  ed25519_public: ssh-ed25519 BBBB host" in result

    def test_minimal_no_options(self):
        result = generate_user_data()
        # Just the header and a trailing newline
        assert result == "#cloud-config\n"

    def test_all_options(self, tmp_path):
        key_dir = tmp_path / "keys"
        key_dir.mkdir()
        (key_dir / "ssh_host_rsa_key").write_text("PRIVATE")
        (key_dir / "ssh_host_rsa_key.pub").write_text("PUBLIC")

        result = generate_user_data(
            timezone="UTC",
            ssh_keys="ssh-rsa AAAA test\n",
            ssh_key_user="root",
            ssh_host_key_dir=key_dir,
            env=["X=1"],
        )
        assert "timezone: UTC" in result
        assert "ssh_authorized_keys:" in result
        assert "ssh_keys:" in result
        assert "write_files:" in result


class TestGenerateNetworkConfig:
    def test_static_ip(self):
        result = generate_network_config(ip="192.168.1.100/24",
                                         gateway="192.168.1.1")
        assert result is not None
        assert "addresses:" in result
        assert "        - 192.168.1.100/24" in result
        assert "      routes:" in result
        assert "        - to: default" in result
        assert "          via: 192.168.1.1" in result
        assert "dhcp4" not in result

    def test_static_ip_no_gateway(self):
        result = generate_network_config(ip="10.0.0.5/24")
        assert result is not None
        assert "        - 10.0.0.5/24" in result
        assert "routes:" not in result
        assert "gateway" not in result

    def test_dhcp_with_dns(self):
        result = generate_network_config(dns="8.8.8.8",
                                         searchdomain="example.com")
        assert result is not None
        assert "      dhcp4: true" in result
        assert "      nameservers:" in result
        assert "          - 8.8.8.8" in result
        assert "          - example.com" in result
        # No static IP addresses line (6 spaces = ethernets level)
        # The only "addresses:" should be under nameservers (8 spaces)
        lines = result.splitlines()
        addr_lines = [l for l in lines if "addresses:" in l]
        for line in addr_lines:
            assert line.startswith("        ")  # all under nameservers

    def test_no_network(self):
        result = generate_network_config()
        assert result is None

    def test_dns_only(self):
        result = generate_network_config(dns="1.1.1.1")
        assert result is not None
        assert "      dhcp4: true" in result
        assert "          - 1.1.1.1" in result

    def test_searchdomain_only(self):
        result = generate_network_config(searchdomain="local.net")
        assert result is not None
        assert "      dhcp4: true" in result
        assert "          - local.net" in result

    def test_full_static(self):
        result = generate_network_config(
            ip="10.0.0.5/24", gateway="10.0.0.1",
            dns="8.8.8.8", searchdomain="test.local",
        )
        assert result is not None
        assert "        - 10.0.0.5/24" in result
        assert "          via: 10.0.0.1" in result
        assert "          - 8.8.8.8" in result
        assert "          - test.local" in result
        assert "dhcp4" not in result

    def test_v2_format(self):
        result = generate_network_config(ip="10.0.0.1/24")
        assert "network:" in result
        assert "  version: 2" in result
        assert "  ethernets:" in result
        assert '        name: "*"' in result


class TestWriteSeed:
    def test_creates_seed_dir(self, tmp_path):
        write_seed(tmp_path, name="test")
        assert (tmp_path / "cloud-seed").is_dir()

    def test_writes_all_files(self, tmp_path):
        write_seed(tmp_path, name="test", ip="10.0.0.5/24", dns="8.8.8.8")
        seed = tmp_path / "cloud-seed"
        assert (seed / "meta-data").is_file()
        assert (seed / "user-data").is_file()
        assert (seed / "network-config").is_file()

    def test_omits_network_config_when_not_needed(self, tmp_path):
        write_seed(tmp_path, name="test")
        seed = tmp_path / "cloud-seed"
        assert (seed / "meta-data").is_file()
        assert (seed / "user-data").is_file()
        assert not (seed / "network-config").exists()

    def test_removes_stale_network_config(self, tmp_path):
        seed = tmp_path / "cloud-seed"
        seed.mkdir()
        (seed / "network-config").write_text("stale")
        write_seed(tmp_path, name="test")
        assert not (seed / "network-config").exists()

    def test_meta_data_content(self, tmp_path):
        write_seed(tmp_path, name="myhost", timezone="UTC")
        content = (tmp_path / "cloud-seed" / "meta-data").read_text()
        assert "local-hostname: myhost" in content
        assert "instance-id: kento-" in content

    def test_user_data_content(self, tmp_path):
        write_seed(tmp_path, name="test", timezone="Asia/Tokyo",
                   env=["FOO=bar"])
        content = (tmp_path / "cloud-seed" / "user-data").read_text()
        assert content.startswith("#cloud-config")
        assert "timezone: Asia/Tokyo" in content
        assert "FOO=bar" in content

    def test_ssh_keys_in_seed(self, tmp_path):
        write_seed(tmp_path, name="test",
                   ssh_keys="ssh-rsa AAAA user@host\n")
        content = (tmp_path / "cloud-seed" / "user-data").read_text()
        assert "ssh_authorized_keys:" in content
        assert "ssh-rsa AAAA user@host" in content

    def test_instance_id_changes_on_reconfig(self, tmp_path):
        write_seed(tmp_path, name="test", timezone="UTC")
        meta1 = (tmp_path / "cloud-seed" / "meta-data").read_text()

        write_seed(tmp_path, name="test", timezone="US/Pacific")
        meta2 = (tmp_path / "cloud-seed" / "meta-data").read_text()

        # Instance IDs should differ
        assert meta1 != meta2
