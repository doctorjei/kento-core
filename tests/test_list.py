"""Tests for container listing."""

import subprocess
from unittest.mock import patch

from kento.list import list_containers


@patch("kento.list.subprocess.run")
def test_list_shows_containers(mock_run, tmp_path, capsys):
    lxc_dir = tmp_path / "mybox"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "upper").mkdir()

    def side_effect(args, **kwargs):
        result = subprocess.CompletedProcess(args, 0)
        if "lxc-info" in args:
            result.stdout = "RUNNING"
        elif "du" in args:
            result.stdout = "16K\t/whatever\n"
        return result

    mock_run.side_effect = side_effect

    with patch("kento.list.LXC_BASE", tmp_path):
        list_containers()

    output = capsys.readouterr().out
    assert "mybox" in output
    assert "myimage:latest" in output
    assert "running" in output


@patch("kento.list.subprocess.run")
def test_list_empty(mock_run, tmp_path, capsys):
    with patch("kento.list.LXC_BASE", tmp_path):
        list_containers()

    output = capsys.readouterr().out
    assert "no kento-managed containers found" in output
