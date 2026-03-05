"""Tests for CLI argument parsing."""

import pytest

from kento.cli import main


def test_help(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "create" in output
    assert "destroy" in output
    assert "list" in output
    assert "reset" in output


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "0.1.0" in output


def test_no_command(capsys):
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "create" in output


def test_create_requires_image(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["create", "test"])
    assert exc.value.code != 0
