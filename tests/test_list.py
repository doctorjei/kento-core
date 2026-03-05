"""Tests for container listing."""

import subprocess
from unittest.mock import patch

from kento.list import list_containers


def _mock_run(args, **kwargs):
    result = subprocess.CompletedProcess(args, 0)
    if "lxc-info" in args:
        result.stdout = "RUNNING"
    elif "du" in args:
        result.stdout = "16K\t/whatever\n"
    return result


@patch("kento.list.subprocess.run", side_effect=_mock_run)
def test_list_shows_containers(mock_run, tmp_path, capsys):
    lxc_dir = tmp_path / "mybox"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()

    with patch("kento.list.LXC_BASE", tmp_path):
        list_containers()

    output = capsys.readouterr().out
    assert "mybox" in output
    assert "myimage:latest" in output
    assert "running" in output


@patch("kento.list.subprocess.run", side_effect=_mock_run)
def test_list_with_separate_state_dir(mock_run, tmp_path, capsys):
    lxc_dir = tmp_path / "mybox"
    lxc_dir.mkdir()
    state = tmp_path / "user-state" / "mybox"
    state.mkdir(parents=True)
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-state").write_text(str(state) + "\n")
    (state / "upper").mkdir()

    with patch("kento.list.LXC_BASE", tmp_path):
        list_containers()

    output = capsys.readouterr().out
    assert "mybox" in output


@patch("kento.list.subprocess.run")
def test_list_empty(mock_run, tmp_path, capsys):
    with patch("kento.list.LXC_BASE", tmp_path):
        list_containers()

    output = capsys.readouterr().out
    assert "no kento-managed containers found" in output
