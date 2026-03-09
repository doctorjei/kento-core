"""Tests for CLI argument parsing."""

import pytest

from kento.cli import main


def test_help(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "container" in output


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "0.5.1" in output


def test_no_command(capsys):
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "container" in output


def test_container_no_subcommand(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["container"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "create" in output
    assert "rm" in output
    assert "list" in output


def test_container_create_requires_image(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["container", "create"])
    assert exc.value.code != 0


def test_container_create_help(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["container", "create", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "--name" in output
    assert "--pve" in output
    assert "--lxc" in output
    assert "--vm" in output
    assert "--vmid" in output
    assert "--port" in output
    assert "--start" in output


def test_pve_lxc_mutually_exclusive(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["container", "create", "debian:12", "--pve", "--lxc"])
    assert exc.value.code != 0


def test_vm_pve_mutually_exclusive(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["container", "create", "debian:12", "--vm", "--pve"])
    assert exc.value.code != 0


def test_vm_lxc_mutually_exclusive(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["container", "create", "debian:12", "--vm", "--lxc"])
    assert exc.value.code != 0


def test_container_subcommands_in_help(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["container", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "create" in output
    assert "start" in output
    assert "stop" in output
    assert "rm" in output
    assert "reset" in output
    assert "list" in output
