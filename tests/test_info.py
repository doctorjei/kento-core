"""Tests for container info/inspect command."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from kento.info import info, _read_meta, _get_size, _get_ssh_host_key_fingerprints


# --- Helper ---


def _make_container(tmp_path: Path, **overrides) -> Path:
    """Create a fake container directory with metadata files."""
    d = tmp_path / "mybox"
    d.mkdir(exist_ok=True)
    (d / "kento-name").write_text("mybox\n")
    (d / "kento-image").write_text("debian:12\n")
    (d / "kento-mode").write_text("lxc\n")
    (d / "kento-state").write_text(str(d) + "\n")
    (d / "kento-layers").write_text("/layers/a:/layers/b:/layers/c\n")
    for filename, content in overrides.items():
        (d / filename).write_text(content)
    return d


def _mock_du_run(args, **kwargs):
    result = subprocess.CompletedProcess(args, 0)
    if "du" in args:
        result.stdout = "16K\t/whatever\n"
    return result


# --- _read_meta ---


def test_read_meta_existing(tmp_path):
    f = tmp_path / "kento-test"
    f.write_text("  hello  \n")
    assert _read_meta(tmp_path, "kento-test") == "hello"


def test_read_meta_missing(tmp_path):
    assert _read_meta(tmp_path, "kento-missing") is None


# --- _get_size ---


@patch("kento.info.subprocess.run", side_effect=_mock_du_run)
def test_get_size(mock_run, tmp_path):
    assert _get_size(tmp_path) == "16K"


@patch("kento.info.subprocess.run")
def test_get_size_failure(mock_run, tmp_path):
    mock_run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="")
    assert _get_size(tmp_path) == "?"


# --- Default output ---


@patch("kento.info.is_running", return_value=False)
def test_info_default_output(mock_running, tmp_path, capsys):
    d = _make_container(tmp_path)
    info("mybox", container_dir=d, mode="lxc")

    output = capsys.readouterr().out
    assert "Name:       mybox" in output
    assert "Image:      debian:12" in output
    assert "Mode:       lxc (LXC)" in output
    assert "Status:     stopped" in output
    assert "Directory:" in output
    assert "State:" in output
    assert "Layers:     3" in output
    assert "Created:" in output


@patch("kento.info.is_running", return_value=True)
def test_info_running_status(mock_running, tmp_path, capsys):
    d = _make_container(tmp_path)
    info("mybox", container_dir=d, mode="lxc")

    output = capsys.readouterr().out
    assert "Status:     running" in output


@patch("kento.info.is_running", return_value=False)
def test_info_vm_mode_type(mock_running, tmp_path, capsys):
    d = _make_container(tmp_path)
    (d / "kento-mode").write_text("vm\n")
    info("mybox", container_dir=d, mode="vm")

    output = capsys.readouterr().out
    assert "Mode:       vm (VM)" in output


@patch("kento.info.is_running", return_value=False)
def test_info_pve_vm_mode_type(mock_running, tmp_path, capsys):
    d = _make_container(tmp_path)
    (d / "kento-mode").write_text("pve-vm\n")
    info("mybox", container_dir=d, mode="pve-vm")

    output = capsys.readouterr().out
    assert "Mode:       pve-vm (VM)" in output


# --- Optional metadata ---


@patch("kento.info.is_running", return_value=False)
def test_info_shows_vmid(mock_running, tmp_path, capsys):
    d = _make_container(tmp_path, **{"kento-vmid": "100\n"})
    info("mybox", container_dir=d, mode="pve")

    output = capsys.readouterr().out
    assert "VMID:       100" in output


@patch("kento.info.is_running", return_value=False)
def test_info_shows_port(mock_running, tmp_path, capsys):
    d = _make_container(tmp_path, **{"kento-port": "10022:22\n"})
    info("mybox", container_dir=d, mode="vm")

    output = capsys.readouterr().out
    assert "Port:       10022:22" in output


@patch("kento.info.is_running", return_value=False)
def test_info_shows_network(mock_running, tmp_path, capsys):
    d = _make_container(tmp_path, **{"kento-net": "192.168.0.100/24\n"})
    info("mybox", container_dir=d, mode="lxc")

    output = capsys.readouterr().out
    assert "Network:    192.168.0.100/24" in output


@patch("kento.info.is_running", return_value=False)
def test_info_shows_timezone(mock_running, tmp_path, capsys):
    d = _make_container(tmp_path, **{"kento-tz": "Europe/Berlin\n"})
    info("mybox", container_dir=d, mode="lxc")

    output = capsys.readouterr().out
    assert "Timezone:   Europe/Berlin" in output


@patch("kento.info.is_running", return_value=False)
def test_info_shows_environment(mock_running, tmp_path, capsys):
    d = _make_container(tmp_path, **{"kento-env": "FOO=bar\nBAZ=qux\n"})
    info("mybox", container_dir=d, mode="lxc")

    output = capsys.readouterr().out
    assert "Env:        FOO=bar, BAZ=qux" in output


@patch("kento.info.is_running", return_value=False)
def test_info_shows_ssh_user_nonroot(mock_running, tmp_path, capsys):
    d = _make_container(tmp_path, **{"kento-ssh-user": "droste\n"})
    info("mybox", container_dir=d, mode="lxc")

    output = capsys.readouterr().out
    assert "SSH user:   droste" in output


@patch("kento.info.is_running", return_value=False)
def test_info_hides_ssh_user_root(mock_running, tmp_path, capsys):
    """ssh_user=root (default) should not clutter the output."""
    d = _make_container(tmp_path)
    info("mybox", container_dir=d, mode="lxc")

    output = capsys.readouterr().out
    assert "SSH user:" not in output


@patch("kento.info.is_running", return_value=False)
def test_info_json_includes_ssh_user(mock_running, tmp_path, capsys):
    d = _make_container(tmp_path, **{"kento-ssh-user": "droste\n"})
    info("mybox", container_dir=d, mode="lxc", as_json=True)

    import json as json_mod
    data = json_mod.loads(capsys.readouterr().out)
    assert data["ssh_user"] == "droste"


@patch("kento.info.is_running", return_value=False)
def test_info_json_ssh_user_default_root(mock_running, tmp_path, capsys):
    """JSON output includes ssh_user=root when no metadata file exists."""
    d = _make_container(tmp_path)
    info("mybox", container_dir=d, mode="lxc", as_json=True)

    import json as json_mod
    data = json_mod.loads(capsys.readouterr().out)
    assert data["ssh_user"] == "root"


@patch("kento.info.is_running", return_value=False)
def test_info_hides_optional_when_absent(mock_running, tmp_path, capsys):
    """Optional fields should not appear when metadata files are missing."""
    d = _make_container(tmp_path)
    info("mybox", container_dir=d, mode="lxc")

    output = capsys.readouterr().out
    assert "VMID:" not in output
    assert "Port:" not in output
    assert "Network:" not in output
    assert "Timezone:" not in output
    assert "Env:" not in output


# --- Minimal metadata ---


@patch("kento.info.is_running", return_value=False)
def test_info_minimal_metadata(mock_running, tmp_path, capsys):
    """With only kento-image and kento-mode, info should still work."""
    d = tmp_path / "minimal"
    d.mkdir()
    (d / "kento-image").write_text("alpine:3\n")
    (d / "kento-mode").write_text("lxc\n")

    info("minimal", container_dir=d, mode="lxc")

    output = capsys.readouterr().out
    assert "Name:       minimal" in output
    assert "Image:      alpine:3" in output
    assert "Layers:     0" in output


# --- JSON output ---


@patch("kento.info.is_running", return_value=False)
def test_info_json_output(mock_running, tmp_path, capsys):
    d = _make_container(tmp_path)
    info("mybox", container_dir=d, mode="lxc", as_json=True)

    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["name"] == "mybox"
    assert data["image"] == "debian:12"
    assert data["mode"] == "lxc"
    assert data["type"] == "LXC"
    assert data["status"] == "stopped"
    assert data["layer_count"] == 3
    assert "directory" in data
    assert "state_directory" in data
    assert "created" in data


@patch("kento.info.is_running", return_value=False)
def test_info_json_with_optional_fields(mock_running, tmp_path, capsys):
    d = _make_container(tmp_path, **{
        "kento-vmid": "200\n",
        "kento-port": "10050:22\n",
        "kento-tz": "UTC\n",
    })
    info("mybox", container_dir=d, mode="pve-vm", as_json=True)

    data = json.loads(capsys.readouterr().out)
    assert data["vmid"] == 200
    assert data["port"] == "10050:22"
    assert data["timezone"] == "UTC"
    assert data["type"] == "VM"


@patch("kento.info.is_running", return_value=False)
def test_info_json_environment_is_list(mock_running, tmp_path, capsys):
    d = _make_container(tmp_path, **{"kento-env": "A=1\nB=2\n"})
    info("mybox", container_dir=d, mode="lxc", as_json=True)

    data = json.loads(capsys.readouterr().out)
    assert data["environment"] == ["A=1", "B=2"]


# --- Verbose output ---


@patch("kento.info.is_running", return_value=False)
@patch("kento.info.subprocess.run", side_effect=_mock_du_run)
def test_info_verbose_shows_upper_size(mock_run, mock_running, tmp_path, capsys):
    d = _make_container(tmp_path)
    (d / "upper").mkdir()

    info("mybox", container_dir=d, mode="lxc", verbose=True)

    output = capsys.readouterr().out
    assert "Upper size: 16K" in output


@patch("kento.info.is_running", return_value=False)
@patch("kento.info.subprocess.run", side_effect=_mock_du_run)
def test_info_verbose_shows_layer_paths(mock_run, mock_running, tmp_path, capsys):
    d = _make_container(tmp_path)
    (d / "upper").mkdir()
    # Create actual layer dirs so _get_size can run
    for name in ("a", "b", "c"):
        (tmp_path / "layers" / name).mkdir(parents=True, exist_ok=True)
    (d / "kento-layers").write_text(
        str(tmp_path / "layers/a") + ":"
        + str(tmp_path / "layers/b") + ":"
        + str(tmp_path / "layers/c")
    )

    info("mybox", container_dir=d, mode="lxc", verbose=True)

    output = capsys.readouterr().out
    assert "Layer paths:" in output
    assert "[0]" in output
    assert "[1]" in output
    assert "[2]" in output
    assert "layers/a" in output


@patch("kento.info.is_running", return_value=False)
@patch("kento.info.subprocess.run", side_effect=_mock_du_run)
def test_info_verbose_json_includes_layers(mock_run, mock_running, tmp_path, capsys):
    d = _make_container(tmp_path)
    (d / "upper").mkdir()
    for name in ("a", "b", "c"):
        (tmp_path / "layers" / name).mkdir(parents=True, exist_ok=True)
    (d / "kento-layers").write_text(
        str(tmp_path / "layers/a") + ":"
        + str(tmp_path / "layers/b") + ":"
        + str(tmp_path / "layers/c")
    )

    info("mybox", container_dir=d, mode="lxc", as_json=True, verbose=True)

    data = json.loads(capsys.readouterr().out)
    assert "upper_size" in data
    assert "layers" in data
    assert len(data["layers"]) == 3
    assert "layer_sizes" in data
    assert len(data["layer_sizes"]) == 3


@patch("kento.info.is_running", return_value=False)
def test_info_verbose_no_layers(mock_running, tmp_path, capsys):
    """Verbose mode without kento-layers should not crash."""
    d = tmp_path / "nolayers"
    d.mkdir()
    (d / "kento-image").write_text("test\n")
    (d / "kento-mode").write_text("lxc\n")

    info("nolayers", container_dir=d, mode="lxc", verbose=True)

    output = capsys.readouterr().out
    assert "Layers:     0" in output
    assert "Layer paths:" not in output


@patch("kento.info.is_running", return_value=False)
def test_info_verbose_no_upper_dir(mock_running, tmp_path, capsys):
    """Verbose mode when upper dir does not exist."""
    d = _make_container(tmp_path)

    info("mybox", container_dir=d, mode="lxc", verbose=True)

    output = capsys.readouterr().out
    assert "Upper size:" not in output


# --- Name fallback ---


@patch("kento.info.is_running", return_value=False)
def test_info_name_from_arg_when_no_file(mock_running, tmp_path, capsys):
    """When kento-name is missing, use the name argument."""
    d = tmp_path / "argname"
    d.mkdir()
    (d / "kento-image").write_text("test\n")
    (d / "kento-mode").write_text("lxc\n")

    info("argname", container_dir=d, mode="lxc")

    output = capsys.readouterr().out
    assert "Name:       argname" in output


@patch("kento.info.is_running", return_value=False)
def test_info_name_from_file_overrides_arg(mock_running, tmp_path, capsys):
    """kento-name file takes precedence over the name argument."""
    d = tmp_path / "100"
    d.mkdir()
    (d / "kento-image").write_text("test\n")
    (d / "kento-mode").write_text("pve\n")
    (d / "kento-name").write_text("webbox\n")

    info("100", container_dir=d, mode="pve")

    output = capsys.readouterr().out
    assert "Name:       webbox" in output


# --- Separate state dir ---


@patch("kento.info.is_running", return_value=False)
def test_info_separate_state_dir(mock_running, tmp_path, capsys):
    """State directory can differ from container directory."""
    d = tmp_path / "mybox"
    d.mkdir()
    state = tmp_path / "state" / "mybox"
    state.mkdir(parents=True)
    (d / "kento-image").write_text("test\n")
    (d / "kento-mode").write_text("lxc\n")
    (d / "kento-state").write_text(str(state) + "\n")

    info("mybox", container_dir=d, mode="lxc")

    output = capsys.readouterr().out
    assert f"State:      {state}" in output


# --- CLI integration ---


class TestCliInfo:
    """Test info/inspect command registration in the CLI."""

    def test_info_in_help(self, capsys):
        from kento.cli import main
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "info" in output
        assert "inspect" in output

    def test_info_help(self, capsys):
        from kento.cli import main
        with pytest.raises(SystemExit) as exc:
            main(["info", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "NAME" in output
        assert "--json" in output
        assert "--verbose" in output

    def test_inspect_help(self, capsys):
        from kento.cli import main
        with pytest.raises(SystemExit) as exc:
            main(["inspect", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "NAME" in output
        assert "--json" in output

    def test_lxc_info_help(self, capsys):
        from kento.cli import main
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "info", "--help"])
        assert exc.value.code == 0

    def test_vm_info_help(self, capsys):
        from kento.cli import main
        with pytest.raises(SystemExit) as exc:
            main(["vm", "info", "--help"])
        assert exc.value.code == 0

    def test_lxc_inspect_help(self, capsys):
        from kento.cli import main
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "inspect", "--help"])
        assert exc.value.code == 0

    def test_vm_inspect_help(self, capsys):
        from kento.cli import main
        with pytest.raises(SystemExit) as exc:
            main(["vm", "inspect", "--help"])
        assert exc.value.code == 0

    def test_info_requires_name(self, capsys):
        from kento.cli import main
        with pytest.raises(SystemExit) as exc:
            main(["info"])
        assert exc.value.code != 0

    def test_info_in_lxc_help(self, capsys):
        from kento.cli import main
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "info" in output
        assert "inspect" in output

    def test_info_in_vm_help(self, capsys):
        from kento.cli import main
        with pytest.raises(SystemExit) as exc:
            main(["vm", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "info" in output
        assert "inspect" in output


# --- SSH host key fingerprints ---


def _make_ssh_host_keys(container_dir: Path, key_types=("rsa", "ecdsa", "ed25519")):
    """Create fake .pub files in ssh-host-keys/."""
    keys_dir = container_dir / "ssh-host-keys"
    keys_dir.mkdir(exist_ok=True)
    for kt in key_types:
        (keys_dir / f"ssh_host_{kt}_key").write_text("PRIVATE_KEY_DATA")
        (keys_dir / f"ssh_host_{kt}_key.pub").write_text(f"ssh-{kt} AAAA...fake {kt}@host\n")
    return keys_dir


def _mock_ssh_keygen_run(args, **kwargs):
    """Mock subprocess.run for ssh-keygen -lf calls."""
    if args[0] == "ssh-keygen" and "-lf" in args:
        pub_path = args[2]
        # Determine key type from filename
        if "rsa" in pub_path:
            return subprocess.CompletedProcess(
                args, 0,
                stdout="3072 SHA256:abcRSA123 comment (RSA)\n", stderr="")
        elif "ecdsa" in pub_path:
            return subprocess.CompletedProcess(
                args, 0,
                stdout="256 SHA256:defECDSA456 comment (ECDSA)\n", stderr="")
        elif "ed25519" in pub_path:
            return subprocess.CompletedProcess(
                args, 0,
                stdout="256 SHA256:ghiED25519789 comment (ED25519)\n", stderr="")
    if args[0] == "du":
        return subprocess.CompletedProcess(args, 0, stdout="16K\t/whatever\n", stderr="")
    return subprocess.CompletedProcess(args, 1, stdout="", stderr="")


def _mock_ssh_keygen_and_du(args, **kwargs):
    """Combined mock for ssh-keygen and du."""
    if args[0] == "ssh-keygen":
        return _mock_ssh_keygen_run(args, **kwargs)
    if args[0] == "du":
        return subprocess.CompletedProcess(args, 0, stdout="16K\t/whatever\n", stderr="")
    return subprocess.CompletedProcess(args, 1, stdout="", stderr="")


# --- _get_ssh_host_key_fingerprints unit tests ---


@patch("kento.info.subprocess.run", side_effect=_mock_ssh_keygen_run)
def test_get_fingerprints_with_keys(mock_run, tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    _make_ssh_host_keys(d)

    fp, has_keys = _get_ssh_host_key_fingerprints(d)
    assert has_keys is True
    assert fp["rsa"] == "SHA256:abcRSA123"
    assert fp["ecdsa"] == "SHA256:defECDSA456"
    assert fp["ed25519"] == "SHA256:ghiED25519789"


def test_get_fingerprints_no_dir(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    fp, has_keys = _get_ssh_host_key_fingerprints(d)
    assert fp == {}
    assert has_keys is False


def test_get_fingerprints_empty_dir(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    (d / "ssh-host-keys").mkdir()
    fp, has_keys = _get_ssh_host_key_fingerprints(d)
    assert fp == {}
    assert has_keys is False


@patch("kento.info.subprocess.run", side_effect=FileNotFoundError("ssh-keygen"))
def test_get_fingerprints_no_ssh_keygen(mock_run, tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    _make_ssh_host_keys(d)

    fp, has_keys = _get_ssh_host_key_fingerprints(d)
    assert fp == {}
    assert has_keys is True  # keys exist but ssh-keygen missing


# --- Human output ---


@patch("kento.info.is_running", return_value=False)
@patch("kento.info.subprocess.run", side_effect=_mock_ssh_keygen_and_du)
def test_info_shows_fingerprints(mock_run, mock_running, tmp_path, capsys):
    d = _make_container(tmp_path)
    _make_ssh_host_keys(d)

    info("mybox", container_dir=d, mode="lxc")

    output = capsys.readouterr().out
    assert "SSH host key fingerprints:" in output
    assert "RSA:" in output
    assert "SHA256:abcRSA123" in output
    assert "ECDSA:" in output
    assert "SHA256:defECDSA456" in output
    assert "ED25519:" in output
    assert "SHA256:ghiED25519789" in output


@patch("kento.info.is_running", return_value=False)
def test_info_no_fingerprints_without_keys(mock_running, tmp_path, capsys):
    d = _make_container(tmp_path)

    info("mybox", container_dir=d, mode="lxc")

    output = capsys.readouterr().out
    assert "SSH host key fingerprints:" not in output


@patch("kento.info.is_running", return_value=False)
@patch("kento.info.subprocess.run", side_effect=FileNotFoundError("ssh-keygen"))
def test_info_ssh_keygen_missing_note(mock_run, mock_running, tmp_path, capsys):
    d = _make_container(tmp_path)
    _make_ssh_host_keys(d)

    info("mybox", container_dir=d, mode="lxc")

    output = capsys.readouterr().out
    assert "ssh-keygen not found, cannot display fingerprints" in output


# --- JSON output ---


@patch("kento.info.is_running", return_value=False)
@patch("kento.info.subprocess.run", side_effect=_mock_ssh_keygen_and_du)
def test_info_json_fingerprints(mock_run, mock_running, tmp_path, capsys):
    d = _make_container(tmp_path)
    _make_ssh_host_keys(d)

    info("mybox", container_dir=d, mode="lxc", as_json=True)

    data = json.loads(capsys.readouterr().out)
    assert "ssh_host_key_fingerprints" in data
    fp = data["ssh_host_key_fingerprints"]
    assert fp["rsa"] == "SHA256:abcRSA123"
    assert fp["ecdsa"] == "SHA256:defECDSA456"
    assert fp["ed25519"] == "SHA256:ghiED25519789"


@patch("kento.info.is_running", return_value=False)
def test_info_json_empty_fingerprints_without_keys(mock_running, tmp_path, capsys):
    d = _make_container(tmp_path)

    info("mybox", container_dir=d, mode="lxc", as_json=True)

    data = json.loads(capsys.readouterr().out)
    assert data["ssh_host_key_fingerprints"] == {}


# --- Verbose mode with fingerprints ---


@patch("kento.info.is_running", return_value=False)
@patch("kento.info.subprocess.run", side_effect=_mock_ssh_keygen_and_du)
def test_info_verbose_still_shows_fingerprints(mock_run, mock_running, tmp_path, capsys):
    d = _make_container(tmp_path)
    _make_ssh_host_keys(d)
    (d / "upper").mkdir()

    info("mybox", container_dir=d, mode="lxc", verbose=True)

    output = capsys.readouterr().out
    assert "SSH host key fingerprints:" in output
    assert "SHA256:abcRSA123" in output
    assert "Upper size:" in output  # verbose field still present


@patch("kento.info.is_running", return_value=False)
@patch("kento.info.subprocess.run", side_effect=_mock_ssh_keygen_and_du)
def test_info_fingerprint_display_order(mock_run, mock_running, tmp_path, capsys):
    """Fingerprints should display in order: RSA, ECDSA, ED25519."""
    d = _make_container(tmp_path)
    _make_ssh_host_keys(d)

    info("mybox", container_dir=d, mode="lxc")

    output = capsys.readouterr().out
    rsa_pos = output.index("RSA:")
    ecdsa_pos = output.index("ECDSA:")
    ed25519_pos = output.index("ED25519:")
    assert rsa_pos < ecdsa_pos < ed25519_pos


@patch("kento.info.is_running", return_value=False)
@patch("kento.info.subprocess.run", side_effect=_mock_ssh_keygen_and_du)
def test_info_fingerprints_single_key_type(mock_run, mock_running, tmp_path, capsys):
    """Only one key type present."""
    d = _make_container(tmp_path)
    _make_ssh_host_keys(d, key_types=("ed25519",))

    info("mybox", container_dir=d, mode="lxc")

    output = capsys.readouterr().out
    assert "SSH host key fingerprints:" in output
    assert "ED25519:" in output
    assert "RSA:" not in output
    assert "ECDSA:" not in output


# --- Pass-through flags (v1.2.0 Phase B4) ---


@patch("kento.info.is_running", return_value=False)
def test_info_verbose_passthrough_both_files(mock_running, tmp_path, capsys):
    """--verbose with both kento-qemu-args and kento-pve-args present."""
    d = _make_container(tmp_path)
    (d / "kento-qemu-args").write_text(
        "-device virtio-rng-pci\n-device virtio-balloon\n")
    (d / "kento-pve-args").write_text("tags: kento-test\nonboot: 1\n")

    info("mybox", container_dir=d, mode="pve-vm", verbose=True)

    output = capsys.readouterr().out
    assert "Pass-through flags:" in output
    assert "  --qemu-arg:" in output
    assert "    -device virtio-rng-pci" in output
    assert "    -device virtio-balloon" in output
    assert "  --pve-arg:" in output
    assert "    tags: kento-test" in output
    assert "    onboot: 1" in output


@patch("kento.info.is_running", return_value=False)
def test_info_verbose_passthrough_only_qemu(mock_running, tmp_path, capsys):
    """--verbose with only kento-qemu-args: only --qemu-arg subheader."""
    d = _make_container(tmp_path)
    (d / "kento-qemu-args").write_text("-device virtio-rng-pci\n")

    info("mybox", container_dir=d, mode="vm", verbose=True)

    output = capsys.readouterr().out
    assert "Pass-through flags:" in output
    assert "  --qemu-arg:" in output
    assert "    -device virtio-rng-pci" in output
    assert "  --pve-arg:" not in output


@patch("kento.info.is_running", return_value=False)
def test_info_verbose_passthrough_only_pve(mock_running, tmp_path, capsys):
    """--verbose with only kento-pve-args: only --pve-arg subheader."""
    d = _make_container(tmp_path)
    (d / "kento-pve-args").write_text("tags: kento-test\n")

    info("mybox", container_dir=d, mode="pve-lxc", verbose=True)

    output = capsys.readouterr().out
    assert "Pass-through flags:" in output
    assert "  --pve-arg:" in output
    assert "    tags: kento-test" in output
    assert "  --qemu-arg:" not in output


@patch("kento.info.is_running", return_value=False)
def test_info_verbose_passthrough_neither(mock_running, tmp_path, capsys):
    """--verbose with neither file: section entirely absent."""
    d = _make_container(tmp_path)

    info("mybox", container_dir=d, mode="lxc", verbose=True)

    output = capsys.readouterr().out
    assert "Pass-through flags:" not in output
    assert "--qemu-arg:" not in output
    assert "--pve-arg:" not in output


@patch("kento.info.is_running", return_value=False)
def test_info_default_hides_passthrough_section(mock_running, tmp_path, capsys):
    """Default (non-verbose) human output never shows Pass-through flags,
    even when both state files are present."""
    d = _make_container(tmp_path)
    (d / "kento-qemu-args").write_text("-device virtio-rng-pci\n")
    (d / "kento-pve-args").write_text("tags: kento-test\n")

    info("mybox", container_dir=d, mode="pve-vm")

    output = capsys.readouterr().out
    assert "Pass-through flags:" not in output
    assert "--qemu-arg:" not in output
    assert "--pve-arg:" not in output


@patch("kento.info.is_running", return_value=False)
def test_info_json_passthrough_both_files(mock_running, tmp_path, capsys):
    """JSON output surfaces qemu_args / pve_args when both files present."""
    d = _make_container(tmp_path)
    (d / "kento-qemu-args").write_text(
        "-device virtio-rng-pci\n-device virtio-balloon\n")
    (d / "kento-pve-args").write_text("tags: kento-test\nonboot: 1\n")

    info("mybox", container_dir=d, mode="pve-vm", as_json=True)

    data = json.loads(capsys.readouterr().out)
    assert data["qemu_args"] == [
        "-device virtio-rng-pci",
        "-device virtio-balloon",
    ]
    assert data["pve_args"] == ["tags: kento-test", "onboot: 1"]


@patch("kento.info.is_running", return_value=False)
def test_info_json_passthrough_empty_when_absent(mock_running, tmp_path, capsys):
    """JSON output includes qemu_args / pve_args as empty lists when files
    are absent. Machine consumers get a stable schema."""
    d = _make_container(tmp_path)

    info("mybox", container_dir=d, mode="lxc", as_json=True)

    data = json.loads(capsys.readouterr().out)
    assert data["qemu_args"] == []
    assert data["pve_args"] == []


@patch("kento.info.is_running", return_value=False)
def test_info_json_verbose_passthrough_same_as_default(mock_running, tmp_path, capsys):
    """--verbose JSON includes the same qemu_args / pve_args keys as
    default JSON (not an --verbose-only surface)."""
    d = _make_container(tmp_path)
    (d / "kento-qemu-args").write_text("-device virtio-rng-pci\n")
    (d / "kento-pve-args").write_text("tags: kento-test\n")

    info("mybox", container_dir=d, mode="pve-vm", as_json=True, verbose=True)

    data = json.loads(capsys.readouterr().out)
    assert data["qemu_args"] == ["-device virtio-rng-pci"]
    assert data["pve_args"] == ["tags: kento-test"]


# --- _read_passthrough_args unit test ---


def test_read_passthrough_args_missing(tmp_path):
    from kento.info import _read_passthrough_args
    assert _read_passthrough_args(tmp_path, "kento-qemu-args") == []


def test_read_passthrough_args_skips_empty_lines(tmp_path):
    from kento.info import _read_passthrough_args
    (tmp_path / "kento-qemu-args").write_text(
        "-device virtio-rng-pci\n\n-device virtio-balloon\n\n")
    assert _read_passthrough_args(tmp_path, "kento-qemu-args") == [
        "-device virtio-rng-pci",
        "-device virtio-balloon",
    ]
