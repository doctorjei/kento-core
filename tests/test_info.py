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
def test_info_default_output(mock_running, tmp_path):
    d = _make_container(tmp_path)
    result = info("mybox", container_dir=d, mode="lxc")

    assert "Name:       mybox" in result
    assert "Image:      debian:12" in result
    assert "Mode:       lxc (LXC)" in result
    assert "Status:     stopped" in result
    assert "Directory:" in result
    assert "State:" in result
    assert "Layers:     3" in result
    assert "Created:" in result


@patch("kento.info.is_running", return_value=True)
def test_info_running_status(mock_running, tmp_path):
    d = _make_container(tmp_path)
    result = info("mybox", container_dir=d, mode="lxc")

    assert "Status:     running" in result


@patch("kento.info.is_running", return_value=False)
def test_info_vm_mode_type(mock_running, tmp_path):
    d = _make_container(tmp_path)
    (d / "kento-mode").write_text("vm\n")
    result = info("mybox", container_dir=d, mode="vm")

    assert "Mode:       vm (VM)" in result


@patch("kento.info.is_running", return_value=False)
def test_info_pve_vm_mode_type(mock_running, tmp_path):
    d = _make_container(tmp_path)
    (d / "kento-mode").write_text("pve-vm\n")
    result = info("mybox", container_dir=d, mode="pve-vm")

    assert "Mode:       pve-vm (VM)" in result


# --- Optional metadata ---


@patch("kento.info.is_running", return_value=False)
def test_info_shows_vmid(mock_running, tmp_path):
    d = _make_container(tmp_path, **{"kento-vmid": "100\n"})
    result = info("mybox", container_dir=d, mode="pve")

    assert "VMID:       100" in result


@patch("kento.info.is_running", return_value=False)
def test_info_shows_port(mock_running, tmp_path):
    d = _make_container(tmp_path, **{"kento-port": "10022:22\n"})
    result = info("mybox", container_dir=d, mode="vm")

    assert "Port:       10022:22" in result


@patch("kento.info.is_running", return_value=False)
def test_info_shows_network(mock_running, tmp_path):
    d = _make_container(tmp_path, **{"kento-net": "192.168.0.100/24\n"})
    result = info("mybox", container_dir=d, mode="lxc")

    assert "Network:    192.168.0.100/24" in result


@patch("kento.info.is_running", return_value=False)
def test_info_shows_timezone(mock_running, tmp_path):
    d = _make_container(tmp_path, **{"kento-tz": "Europe/Berlin\n"})
    result = info("mybox", container_dir=d, mode="lxc")

    assert "Timezone:   Europe/Berlin" in result


@patch("kento.info.is_running", return_value=False)
def test_info_shows_environment(mock_running, tmp_path):
    d = _make_container(tmp_path, **{"kento-env": "FOO=bar\nBAZ=qux\n"})
    result = info("mybox", container_dir=d, mode="lxc")

    assert "Env:        FOO=bar, BAZ=qux" in result


@patch("kento.info.is_running", return_value=False)
def test_info_shows_ssh_user_nonroot(mock_running, tmp_path):
    d = _make_container(tmp_path, **{"kento-ssh-user": "droste\n"})
    result = info("mybox", container_dir=d, mode="lxc")

    assert "SSH user:   droste" in result


@patch("kento.info.is_running", return_value=False)
def test_info_hides_ssh_user_root(mock_running, tmp_path):
    """ssh_user=root (default) should not clutter the output."""
    d = _make_container(tmp_path)
    result = info("mybox", container_dir=d, mode="lxc")

    assert "SSH user:" not in result


@patch("kento.info.is_running", return_value=False)
def test_info_json_includes_ssh_user(mock_running, tmp_path):
    d = _make_container(tmp_path, **{"kento-ssh-user": "droste\n"})
    data = json.loads(info("mybox", container_dir=d, mode="lxc", as_json=True))
    assert data["ssh_user"] == "droste"


@patch("kento.info.is_running", return_value=False)
def test_info_json_ssh_user_default_root(mock_running, tmp_path):
    """JSON output includes ssh_user=root when no metadata file exists."""
    d = _make_container(tmp_path)
    data = json.loads(info("mybox", container_dir=d, mode="lxc", as_json=True))
    assert data["ssh_user"] == "root"


@patch("kento.info.is_running", return_value=False)
def test_info_nesting_allowed_human(mock_running, tmp_path):
    d = _make_container(tmp_path, **{"kento-nesting": "1\n"})
    result = info("mybox", container_dir=d, mode="lxc")
    assert "Nesting:    allowed" in result


@patch("kento.info.is_running", return_value=False)
def test_info_nesting_disabled_human(mock_running, tmp_path):
    d = _make_container(tmp_path, **{"kento-nesting": "0\n"})
    result = info("mybox", container_dir=d, mode="vm")
    assert "Nesting:    disabled" in result


@patch("kento.info.is_running", return_value=False)
def test_info_nesting_json(mock_running, tmp_path):
    d = _make_container(tmp_path, **{"kento-nesting": "1\n"})
    data = json.loads(info("mybox", container_dir=d, mode="vm", as_json=True))
    assert data["nesting"] is True

    two = tmp_path / "two"
    two.mkdir()
    d2 = _make_container(two, **{"kento-nesting": "0\n"})
    data2 = json.loads(info("mybox", container_dir=d2, mode="lxc", as_json=True))
    assert data2["nesting"] is False


@patch("kento.info.is_running", return_value=False)
def test_info_nesting_absent_no_line(mock_running, tmp_path):
    d = _make_container(tmp_path)
    result = info("mybox", container_dir=d, mode="lxc")
    assert "Nesting:" not in result


@patch("kento.info.is_running", return_value=False)
def test_info_hides_optional_when_absent(mock_running, tmp_path):
    """Optional fields should not appear when metadata files are missing."""
    d = _make_container(tmp_path)
    result = info("mybox", container_dir=d, mode="lxc")

    assert "VMID:" not in result
    assert "Port:" not in result
    assert "Network:" not in result
    assert "Timezone:" not in result
    assert "Env:" not in result


# --- Minimal metadata ---


@patch("kento.info.is_running", return_value=False)
def test_info_minimal_metadata(mock_running, tmp_path):
    """With only kento-image and kento-mode, info should still work."""
    d = tmp_path / "minimal"
    d.mkdir()
    (d / "kento-image").write_text("alpine:3\n")
    (d / "kento-mode").write_text("lxc\n")

    result = info("minimal", container_dir=d, mode="lxc")

    assert "Name:       minimal" in result
    assert "Image:      alpine:3" in result
    assert "Layers:     0" in result


# --- JSON output ---


@patch("kento.info.is_running", return_value=False)
def test_info_json_output(mock_running, tmp_path):
    d = _make_container(tmp_path)
    data = json.loads(info("mybox", container_dir=d, mode="lxc", as_json=True))
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
def test_info_json_with_optional_fields(mock_running, tmp_path):
    d = _make_container(tmp_path, **{
        "kento-vmid": "200\n",
        "kento-port": "10050:22\n",
        "kento-tz": "UTC\n",
    })
    data = json.loads(info("mybox", container_dir=d, mode="pve-vm", as_json=True))
    assert data["vmid"] == 200
    assert data["port"] == "10050:22"
    assert data["timezone"] == "UTC"
    assert data["type"] == "VM"


@patch("kento.info.is_running", return_value=False)
def test_info_json_mode_pve_normalized_to_pve_lxc(mock_running, tmp_path):
    """inspect --json normalizes the raw 'pve' mode to 'pve-lxc' so it agrees
    with `list --json`. data["type"] stays the LXC/VM family."""
    d = _make_container(tmp_path, **{"kento-mode": "pve\n"})
    data = json.loads(info("mybox", container_dir=d, mode="pve", as_json=True))
    assert data["mode"] == "pve-lxc"
    assert data["type"] == "LXC"


@patch("kento.info.is_running", return_value=False)
def test_info_json_environment_is_list(mock_running, tmp_path):
    d = _make_container(tmp_path, **{"kento-env": "A=1\nB=2\n"})
    data = json.loads(info("mybox", container_dir=d, mode="lxc", as_json=True))
    assert data["environment"] == ["A=1", "B=2"]


# --- Verbose output ---


@patch("kento.info.is_running", return_value=False)
@patch("kento.info.subprocess.run", side_effect=_mock_du_run)
def test_info_verbose_shows_upper_size(mock_run, mock_running, tmp_path):
    d = _make_container(tmp_path)
    (d / "upper").mkdir()

    result = info("mybox", container_dir=d, mode="lxc", verbose=True)

    assert "Upper size: 16K" in result


@patch("kento.info.is_running", return_value=False)
@patch("kento.info.subprocess.run", side_effect=_mock_du_run)
def test_info_verbose_shows_layer_paths(mock_run, mock_running, tmp_path):
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

    result = info("mybox", container_dir=d, mode="lxc", verbose=True)

    assert "Layer paths:" in result
    assert "[0]" in result
    assert "[1]" in result
    assert "[2]" in result
    assert "layers/a" in result


@patch("kento.info.is_running", return_value=False)
@patch("kento.info.subprocess.run", side_effect=_mock_du_run)
def test_info_verbose_json_includes_layers(mock_run, mock_running, tmp_path):
    d = _make_container(tmp_path)
    (d / "upper").mkdir()
    for name in ("a", "b", "c"):
        (tmp_path / "layers" / name).mkdir(parents=True, exist_ok=True)
    (d / "kento-layers").write_text(
        str(tmp_path / "layers/a") + ":"
        + str(tmp_path / "layers/b") + ":"
        + str(tmp_path / "layers/c")
    )

    data = json.loads(info("mybox", container_dir=d, mode="lxc", as_json=True, verbose=True))
    assert "upper_size" in data
    assert "layers" in data
    assert len(data["layers"]) == 3
    assert "layer_sizes" in data
    assert len(data["layer_sizes"]) == 3


def _mock_du_per_path(args, **kwargs):
    """du mock returning a distinct size derived from the target path,
    so a misaligned size can be told apart from the right one."""
    if "du" in args:
        target = args[-1]
        # Size encodes the trailing path component (a/b/c -> 1K/2K/3K).
        last = Path(target).name
        size = {"a": "1K", "b": "2K", "c": "3K"}.get(last, "9K")
        return subprocess.CompletedProcess(args, 0, stdout=f"{size}\t{target}\n", stderr="")
    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


@patch("kento.info.is_running", return_value=False)
@patch("kento.info.subprocess.run", side_effect=_mock_du_per_path)
def test_info_verbose_layer_sizes_aligned_when_middle_dir_missing(
        mock_run, mock_running, tmp_path):
    """A missing MIDDLE layer dir must not shift sizes onto the wrong layer.

    Regression for the bug where layer_sizes skipped absent dirs, so a
    non-last missing layer shifted later sizes left (the last layer fell
    through to '?'). With the fix, sizes stay positionally aligned: present
    layers keep their own size and the absent one shows a placeholder.
    """
    d = _make_container(tmp_path)
    (d / "upper").mkdir()
    # Create layers a and c but NOT b (the middle one).
    (tmp_path / "layers" / "a").mkdir(parents=True)
    (tmp_path / "layers" / "c").mkdir(parents=True)
    (d / "kento-layers").write_text(
        str(tmp_path / "layers/a") + ":"
        + str(tmp_path / "layers/b") + ":"
        + str(tmp_path / "layers/c")
    )

    result = info("mybox", container_dir=d, mode="lxc", verbose=True)

    lines = result.splitlines()
    line0 = next(line for line in lines if "[0]" in line)
    line1 = next(line for line in lines if "[1]" in line)
    line2 = next(line for line in lines if "[2]" in line)
    # layer a (index 0) -> its own size, NOT shifted from b/c.
    assert "layers/a" in line0 and "(1K)" in line0
    # layer b (index 1) is missing -> placeholder, not a byte size.
    assert "layers/b" in line1 and "missing" in line1
    assert "(2K)" not in line1 and "(3K)" not in line1
    # layer c (index 2) keeps its own size (the bug made this '?').
    assert "layers/c" in line2 and "(3K)" in line2
    assert "(?)" not in line2


@patch("kento.info.is_running", return_value=False)
@patch("kento.info.subprocess.run", side_effect=_mock_du_per_path)
def test_info_verbose_json_layer_sizes_index_aligned(
        mock_run, mock_running, tmp_path):
    """JSON layer_sizes stays index-aligned with layers; the absent middle
    layer is a null placeholder rather than a dropped element."""
    d = _make_container(tmp_path)
    (d / "upper").mkdir()
    (tmp_path / "layers" / "a").mkdir(parents=True)
    (tmp_path / "layers" / "c").mkdir(parents=True)
    (d / "kento-layers").write_text(
        str(tmp_path / "layers/a") + ":"
        + str(tmp_path / "layers/b") + ":"
        + str(tmp_path / "layers/c")
    )

    data = json.loads(info("mybox", container_dir=d, mode="lxc", as_json=True, verbose=True))
    assert len(data["layers"]) == 3
    assert len(data["layer_sizes"]) == 3
    assert data["layer_sizes"][0] == "1K"
    assert data["layer_sizes"][1] is None
    assert data["layer_sizes"][2] == "3K"


@patch("kento.info.is_running", return_value=False)
def test_info_verbose_no_layers(mock_running, tmp_path):
    """Verbose mode without kento-layers should not crash."""
    d = tmp_path / "nolayers"
    d.mkdir()
    (d / "kento-image").write_text("test\n")
    (d / "kento-mode").write_text("lxc\n")

    result = info("nolayers", container_dir=d, mode="lxc", verbose=True)

    assert "Layers:     0" in result
    assert "Layer paths:" not in result


@patch("kento.info.is_running", return_value=False)
def test_info_verbose_no_upper_dir(mock_running, tmp_path):
    """Verbose mode when upper dir does not exist."""
    d = _make_container(tmp_path)

    result = info("mybox", container_dir=d, mode="lxc", verbose=True)

    assert "Upper size:" not in result


# --- Name fallback ---


@patch("kento.info.is_running", return_value=False)
def test_info_name_from_arg_when_no_file(mock_running, tmp_path):
    """When kento-name is missing, use the name argument."""
    d = tmp_path / "argname"
    d.mkdir()
    (d / "kento-image").write_text("test\n")
    (d / "kento-mode").write_text("lxc\n")

    result = info("argname", container_dir=d, mode="lxc")

    assert "Name:       argname" in result


@patch("kento.info.is_running", return_value=False)
def test_info_name_from_file_overrides_arg(mock_running, tmp_path):
    """kento-name file takes precedence over the name argument."""
    d = tmp_path / "100"
    d.mkdir()
    (d / "kento-image").write_text("test\n")
    (d / "kento-mode").write_text("pve\n")
    (d / "kento-name").write_text("webbox\n")

    result = info("100", container_dir=d, mode="pve")

    assert "Name:       webbox" in result


# --- Separate state dir ---


@patch("kento.info.is_running", return_value=False)
def test_info_separate_state_dir(mock_running, tmp_path):
    """State directory can differ from container directory."""
    d = tmp_path / "mybox"
    d.mkdir()
    state = tmp_path / "state" / "mybox"
    state.mkdir(parents=True)
    (d / "kento-image").write_text("test\n")
    (d / "kento-mode").write_text("lxc\n")
    (d / "kento-state").write_text(str(state) + "\n")

    result = info("mybox", container_dir=d, mode="lxc")

    assert f"State:      {state}" in result


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
def test_info_shows_fingerprints(mock_run, mock_running, tmp_path):
    d = _make_container(tmp_path)
    _make_ssh_host_keys(d)

    result = info("mybox", container_dir=d, mode="lxc")

    assert "SSH host key fingerprints:" in result
    assert "RSA:" in result
    assert "SHA256:abcRSA123" in result
    assert "ECDSA:" in result
    assert "SHA256:defECDSA456" in result
    assert "ED25519:" in result
    assert "SHA256:ghiED25519789" in result


@patch("kento.info.is_running", return_value=False)
def test_info_no_fingerprints_without_keys(mock_running, tmp_path):
    d = _make_container(tmp_path)

    result = info("mybox", container_dir=d, mode="lxc")

    assert "SSH host key fingerprints:" not in result


@patch("kento.info.is_running", return_value=False)
@patch("kento.info.subprocess.run", side_effect=FileNotFoundError("ssh-keygen"))
def test_info_ssh_keygen_missing_note(mock_run, mock_running, tmp_path):
    d = _make_container(tmp_path)
    _make_ssh_host_keys(d)

    result = info("mybox", container_dir=d, mode="lxc")

    assert "ssh-keygen not found, cannot display fingerprints" in result


# --- JSON output ---


@patch("kento.info.is_running", return_value=False)
@patch("kento.info.subprocess.run", side_effect=_mock_ssh_keygen_and_du)
def test_info_json_fingerprints(mock_run, mock_running, tmp_path):
    d = _make_container(tmp_path)
    _make_ssh_host_keys(d)

    data = json.loads(info("mybox", container_dir=d, mode="lxc", as_json=True))
    assert "ssh_host_key_fingerprints" in data
    fp = data["ssh_host_key_fingerprints"]
    assert fp["rsa"] == "SHA256:abcRSA123"
    assert fp["ecdsa"] == "SHA256:defECDSA456"
    assert fp["ed25519"] == "SHA256:ghiED25519789"


@patch("kento.info.is_running", return_value=False)
def test_info_json_empty_fingerprints_without_keys(mock_running, tmp_path):
    d = _make_container(tmp_path)

    data = json.loads(info("mybox", container_dir=d, mode="lxc", as_json=True))
    assert data["ssh_host_key_fingerprints"] == {}


# --- Verbose mode with fingerprints ---


@patch("kento.info.is_running", return_value=False)
@patch("kento.info.subprocess.run", side_effect=_mock_ssh_keygen_and_du)
def test_info_verbose_still_shows_fingerprints(mock_run, mock_running, tmp_path):
    d = _make_container(tmp_path)
    _make_ssh_host_keys(d)
    (d / "upper").mkdir()

    result = info("mybox", container_dir=d, mode="lxc", verbose=True)

    assert "SSH host key fingerprints:" in result
    assert "SHA256:abcRSA123" in result
    assert "Upper size:" in result  # verbose field still present


@patch("kento.info.is_running", return_value=False)
@patch("kento.info.subprocess.run", side_effect=_mock_ssh_keygen_and_du)
def test_info_fingerprint_display_order(mock_run, mock_running, tmp_path):
    """Fingerprints should display in order: RSA, ECDSA, ED25519."""
    d = _make_container(tmp_path)
    _make_ssh_host_keys(d)

    result = info("mybox", container_dir=d, mode="lxc")

    rsa_pos = result.index("RSA:")
    ecdsa_pos = result.index("ECDSA:")
    ed25519_pos = result.index("ED25519:")
    assert rsa_pos < ecdsa_pos < ed25519_pos


@patch("kento.info.is_running", return_value=False)
@patch("kento.info.subprocess.run", side_effect=_mock_ssh_keygen_and_du)
def test_info_fingerprints_single_key_type(mock_run, mock_running, tmp_path):
    """Only one key type present."""
    d = _make_container(tmp_path)
    _make_ssh_host_keys(d, key_types=("ed25519",))

    result = info("mybox", container_dir=d, mode="lxc")

    assert "SSH host key fingerprints:" in result
    assert "ED25519:" in result
    assert "RSA:" not in result
    assert "ECDSA:" not in result


# --- Pass-through flags (v1.2.0 Phase B4) ---


@patch("kento.info.is_running", return_value=False)
def test_info_verbose_passthrough_both_files(mock_running, tmp_path):
    """--verbose with both kento-qemu-args and kento-pve-args present."""
    d = _make_container(tmp_path)
    (d / "kento-qemu-args").write_text(
        "-device virtio-rng-pci\n-device virtio-balloon\n")
    (d / "kento-pve-args").write_text("tags: kento-test\nonboot: 1\n")

    result = info("mybox", container_dir=d, mode="pve-vm", verbose=True)

    assert "Pass-through flags:" in result
    assert "  --qemu-arg:" in result
    assert "    -device virtio-rng-pci" in result
    assert "    -device virtio-balloon" in result
    assert "  --pve-arg:" in result
    assert "    tags: kento-test" in result
    assert "    onboot: 1" in result


@patch("kento.info.is_running", return_value=False)
def test_info_verbose_passthrough_only_qemu(mock_running, tmp_path):
    """--verbose with only kento-qemu-args: only --qemu-arg subheader."""
    d = _make_container(tmp_path)
    (d / "kento-qemu-args").write_text("-device virtio-rng-pci\n")

    result = info("mybox", container_dir=d, mode="vm", verbose=True)

    assert "Pass-through flags:" in result
    assert "  --qemu-arg:" in result
    assert "    -device virtio-rng-pci" in result
    assert "  --pve-arg:" not in result


@patch("kento.info.is_running", return_value=False)
def test_info_verbose_passthrough_only_pve(mock_running, tmp_path):
    """--verbose with only kento-pve-args: only --pve-arg subheader."""
    d = _make_container(tmp_path)
    (d / "kento-pve-args").write_text("tags: kento-test\n")

    result = info("mybox", container_dir=d, mode="pve-lxc", verbose=True)

    assert "Pass-through flags:" in result
    assert "  --pve-arg:" in result
    assert "    tags: kento-test" in result
    assert "  --qemu-arg:" not in result


@patch("kento.info.is_running", return_value=False)
def test_info_verbose_passthrough_neither(mock_running, tmp_path):
    """--verbose with neither file: section entirely absent."""
    d = _make_container(tmp_path)

    result = info("mybox", container_dir=d, mode="lxc", verbose=True)

    assert "Pass-through flags:" not in result
    assert "--qemu-arg:" not in result
    assert "--pve-arg:" not in result


@patch("kento.info.is_running", return_value=False)
def test_info_default_hides_passthrough_section(mock_running, tmp_path):
    """Default (non-verbose) human output never shows Pass-through flags,
    even when both state files are present."""
    d = _make_container(tmp_path)
    (d / "kento-qemu-args").write_text("-device virtio-rng-pci\n")
    (d / "kento-pve-args").write_text("tags: kento-test\n")

    result = info("mybox", container_dir=d, mode="pve-vm")

    assert "Pass-through flags:" not in result
    assert "--qemu-arg:" not in result
    assert "--pve-arg:" not in result


@patch("kento.info.is_running", return_value=False)
def test_info_json_passthrough_both_files(mock_running, tmp_path):
    """JSON output surfaces qemu_args / pve_args when both files present."""
    d = _make_container(tmp_path)
    (d / "kento-qemu-args").write_text(
        "-device virtio-rng-pci\n-device virtio-balloon\n")
    (d / "kento-pve-args").write_text("tags: kento-test\nonboot: 1\n")

    data = json.loads(info("mybox", container_dir=d, mode="pve-vm", as_json=True))
    assert data["qemu_args"] == [
        "-device virtio-rng-pci",
        "-device virtio-balloon",
    ]
    assert data["pve_args"] == ["tags: kento-test", "onboot: 1"]


@patch("kento.info.is_running", return_value=False)
def test_info_passthrough_lxc_args(mock_running, tmp_path):
    """lxc_args surface in JSON and under --verbose human output, on par
    with qemu_args / pve_args."""
    d = _make_container(tmp_path)
    (d / "kento-lxc-args").write_text(
        "lxc.cgroup2.devices.allow = c 10:200 rwm\nlxc.cap.drop = sys_admin\n")

    data = json.loads(info("mybox", container_dir=d, mode="lxc", as_json=True))
    assert data["lxc_args"] == [
        "lxc.cgroup2.devices.allow = c 10:200 rwm",
        "lxc.cap.drop = sys_admin",
    ]

    result = info("mybox", container_dir=d, mode="lxc", verbose=True)
    assert "--lxc-arg:" in result
    assert "lxc.cgroup2.devices.allow = c 10:200 rwm" in result


@patch("kento.info.is_running", return_value=False)
def test_info_json_passthrough_empty_when_absent(mock_running, tmp_path):
    """JSON output includes qemu_args / pve_args as empty lists when files
    are absent. Machine consumers get a stable schema."""
    d = _make_container(tmp_path)

    data = json.loads(info("mybox", container_dir=d, mode="lxc", as_json=True))
    assert data["qemu_args"] == []
    assert data["pve_args"] == []
    assert data["lxc_args"] == []


@patch("kento.info.is_running", return_value=False)
def test_info_json_verbose_passthrough_same_as_default(mock_running, tmp_path):
    """--verbose JSON includes the same qemu_args / pve_args keys as
    default JSON (not an --verbose-only surface)."""
    d = _make_container(tmp_path)
    (d / "kento-qemu-args").write_text("-device virtio-rng-pci\n")
    (d / "kento-pve-args").write_text("tags: kento-test\n")

    data = json.loads(info("mybox", container_dir=d, mode="pve-vm", as_json=True, verbose=True))
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
