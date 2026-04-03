"""Tests for container info/inspect command."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from kento.info import info, _read_meta, _get_size


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
        assert "CONTAINER" in output
        assert "--json" in output
        assert "--verbose" in output

    def test_inspect_help(self, capsys):
        from kento.cli import main
        with pytest.raises(SystemExit) as exc:
            main(["inspect", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "CONTAINER" in output
        assert "--json" in output

    def test_container_info_help(self, capsys):
        from kento.cli import main
        with pytest.raises(SystemExit) as exc:
            main(["container", "info", "--help"])
        assert exc.value.code == 0

    def test_vm_info_help(self, capsys):
        from kento.cli import main
        with pytest.raises(SystemExit) as exc:
            main(["vm", "info", "--help"])
        assert exc.value.code == 0

    def test_container_inspect_help(self, capsys):
        from kento.cli import main
        with pytest.raises(SystemExit) as exc:
            main(["container", "inspect", "--help"])
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

    def test_info_in_container_help(self, capsys):
        from kento.cli import main
        with pytest.raises(SystemExit) as exc:
            main(["container", "--help"])
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
