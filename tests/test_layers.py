"""Tests for layer resolution."""

from unittest.mock import patch
import subprocess

import pytest

from kento.layers import resolve_layers, _podman_cmd


def _mock_run(args, **kwargs):
    """Mock podman subprocess calls."""
    result = subprocess.CompletedProcess(args, 0)
    if "exists" in args:
        return result
    if "UpperDir" in args[-1]:
        result.stdout = "/upper/diff\n"
    elif "LowerDir" in args[-1]:
        result.stdout = "/lower1/diff:/lower2/diff\n"
    return result


def _mock_run_single_layer(args, **kwargs):
    """Mock podman for a single-layer image."""
    result = subprocess.CompletedProcess(args, 0)
    if "exists" in args:
        return result
    if "UpperDir" in args[-1]:
        result.stdout = "/upper/diff\n"
    elif "LowerDir" in args[-1]:
        result.stdout = "<no value>\n"
    return result


def _mock_run_no_image(args, **kwargs):
    """Mock podman image exists failure."""
    if "exists" in args:
        return subprocess.CompletedProcess(args, 1)
    return subprocess.CompletedProcess(args, 0, stdout="")


@patch("kento.layers.subprocess.run")
def test_resolve_layers_multi(mock_run):
    mock_run.side_effect = _mock_run
    result = resolve_layers("myimage:latest")
    assert result == "/upper/diff:/lower1/diff:/lower2/diff"


@patch("kento.layers.subprocess.run")
def test_resolve_layers_single(mock_run):
    mock_run.side_effect = _mock_run_single_layer
    result = resolve_layers("myimage:latest")
    assert result == "/upper/diff"
    assert "<no value>" not in result


@patch("kento.layers.subprocess.run")
def test_resolve_layers_missing_image(mock_run):
    mock_run.side_effect = _mock_run_no_image
    with pytest.raises(SystemExit):
        resolve_layers("nonexistent:latest")


class TestPodmanCmd:
    def test_lxc_mode_ignores_sudo_user(self):
        with patch.dict("os.environ", {"SUDO_USER": "alice"}):
            assert _podman_cmd(mode="lxc") == ["podman"]

    def test_pve_mode_ignores_sudo_user(self):
        with patch.dict("os.environ", {"SUDO_USER": "alice"}):
            assert _podman_cmd(mode="pve") == ["podman"]

    def test_vm_mode_uses_sudo_user(self):
        with patch.dict("os.environ", {"SUDO_USER": "alice"}):
            assert _podman_cmd(mode="vm") == ["runuser", "-u", "alice", "--", "podman"]

    def test_vm_mode_no_sudo_user(self):
        with patch.dict("os.environ", {}, clear=True):
            assert _podman_cmd(mode="vm") == ["podman"]

    def test_none_mode_ignores_sudo_user(self):
        with patch.dict("os.environ", {"SUDO_USER": "alice"}):
            assert _podman_cmd() == ["podman"]
