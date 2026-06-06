"""Tests for layer resolution."""

from unittest.mock import patch
import subprocess

import pytest

from kento.layers import resolve_layers, ensure_image_hold, _podman_cmd


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


class TestEnsureImageHold:
    """ensure_image_hold backfills the hold container only when missing."""

    def _exists_check_cmd(self, calls):
        for c in calls:
            cmd = list(c.args[0])
            if "container" in cmd and "exists" in cmd:
                return cmd
        return None

    @patch("kento.layers.subprocess.run")
    def test_creates_hold_when_missing(self, mock_run):
        def side_effect(args, **kwargs):
            cmd = list(args)
            if "exists" in cmd:
                return subprocess.CompletedProcess(cmd, 1)  # missing
            return subprocess.CompletedProcess(cmd, 0)

        mock_run.side_effect = side_effect
        ensure_image_hold("myimage:latest", "mybox")

        # exists-check uses `podman container exists kento-hold.mybox`
        exists_cmd = self._exists_check_cmd(mock_run.call_args_list)
        assert exists_cmd == ["podman", "container", "exists", "kento-hold.mybox"]

        # create_image_hold was invoked
        create_calls = [list(c.args[0]) for c in mock_run.call_args_list
                        if "create" in list(c.args[0])]
        assert len(create_calls) == 1
        cmd = create_calls[0]
        assert "--name" in cmd and "kento-hold.mybox" in cmd
        assert "io.kento.hold-for=mybox" in cmd
        assert "myimage:latest" in cmd

    @patch("kento.layers.subprocess.run")
    def test_no_create_when_hold_exists(self, mock_run):
        def side_effect(args, **kwargs):
            cmd = list(args)
            if "exists" in cmd:
                return subprocess.CompletedProcess(cmd, 0)  # present
            return subprocess.CompletedProcess(cmd, 0)

        mock_run.side_effect = side_effect
        ensure_image_hold("myimage:latest", "mybox")

        create_calls = [c for c in mock_run.call_args_list
                        if "create" in list(c.args[0])]
        assert create_calls == []

    @patch("kento.layers.subprocess.run", side_effect=OSError("podman gone"))
    def test_tolerates_subprocess_failure(self, mock_run):
        # best-effort: never raises even if podman blows up
        ensure_image_hold("myimage:latest", "mybox")


class TestPodmanCmd:
    def test_returns_podman(self):
        assert _podman_cmd() == ["podman"]

    def test_returns_podman_with_sudo_user(self):
        with patch.dict("os.environ", {"SUDO_USER": "alice"}):
            assert _podman_cmd() == ["podman"]
