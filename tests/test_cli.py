"""Tests for CLI argument parsing."""

import pytest

from kento.cli import main, _parse_network


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
    assert "0.7.0" in output


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
    assert "destroy" in output
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
    assert "shutdown" in output
    assert "stop" in output
    assert "destroy" in output
    assert "rm" in output
    assert "scrub" in output
    assert "list" in output


def test_container_shutdown_in_help(capsys):
    """shutdown is recognized as a command with --force flag."""
    with pytest.raises(SystemExit) as exc:
        main(["container", "shutdown", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "--force" in output


def test_container_stop_alias(capsys):
    """stop still works as an alias for shutdown with --force flag."""
    with pytest.raises(SystemExit) as exc:
        main(["container", "stop", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "--force" in output


def test_container_destroy_in_help(capsys):
    """destroy is recognized as a command."""
    with pytest.raises(SystemExit) as exc:
        main(["container", "destroy", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "--force" in output


def test_container_rm_alias(capsys):
    """rm still works as an alias for destroy."""
    with pytest.raises(SystemExit) as exc:
        main(["container", "rm", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "--force" in output


def test_container_scrub_in_help(capsys):
    """scrub is recognized as a command."""
    with pytest.raises(SystemExit) as exc:
        main(["container", "scrub", "--help"])
    assert exc.value.code == 0


# --- Three-level CLI tests ---

class TestBareCommands:
    """Test bare top-level commands (kento <cmd>)."""

    def test_bare_create_requires_image(self, capsys):
        """kento create (no image) should error."""
        with pytest.raises(SystemExit) as exc:
            main(["create"])
        assert exc.value.code != 0

    def test_bare_create_help(self, capsys):
        """kento create --help works and shows all flags."""
        with pytest.raises(SystemExit) as exc:
            main(["create", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--name" in output
        assert "--vm" in output
        assert "--force" in output

    def test_bare_start_help(self, capsys):
        """kento start --help is recognized."""
        with pytest.raises(SystemExit) as exc:
            main(["start", "--help"])
        assert exc.value.code == 0

    def test_bare_list_help(self, capsys):
        """kento list --help is recognized."""
        with pytest.raises(SystemExit) as exc:
            main(["list", "--help"])
        assert exc.value.code == 0

    def test_bare_ls_help(self, capsys):
        """kento ls --help is recognized."""
        with pytest.raises(SystemExit) as exc:
            main(["ls", "--help"])
        assert exc.value.code == 0

    def test_bare_shutdown_help(self, capsys):
        """kento shutdown --help is recognized."""
        with pytest.raises(SystemExit) as exc:
            main(["shutdown", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--force" in output

    def test_bare_stop_help(self, capsys):
        """kento stop --help is recognized."""
        with pytest.raises(SystemExit) as exc:
            main(["stop", "--help"])
        assert exc.value.code == 0

    def test_bare_destroy_help(self, capsys):
        """kento destroy --help is recognized."""
        with pytest.raises(SystemExit) as exc:
            main(["destroy", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--force" in output

    def test_bare_rm_help(self, capsys):
        """kento rm --help is recognized."""
        with pytest.raises(SystemExit) as exc:
            main(["rm", "--help"])
        assert exc.value.code == 0

    def test_bare_scrub_help(self, capsys):
        """kento scrub --help is recognized."""
        with pytest.raises(SystemExit) as exc:
            main(["scrub", "--help"])
        assert exc.value.code == 0


class TestVmCommands:
    """Test vm subcommand group (kento vm <cmd>)."""

    def test_vm_no_subcommand(self, capsys):
        """kento vm with no subcommand shows help."""
        with pytest.raises(SystemExit) as exc:
            main(["vm"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "create" in output

    def test_vm_create_help(self, capsys):
        """kento vm create --help works."""
        with pytest.raises(SystemExit) as exc:
            main(["vm", "create", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--name" in output

    def test_vm_create_requires_image(self, capsys):
        """kento vm create (no image) should error."""
        with pytest.raises(SystemExit) as exc:
            main(["vm", "create"])
        assert exc.value.code != 0

    def test_vm_start_help(self, capsys):
        """kento vm start --help is recognized."""
        with pytest.raises(SystemExit) as exc:
            main(["vm", "start", "--help"])
        assert exc.value.code == 0

    def test_vm_stop_help(self, capsys):
        """kento vm stop --help is recognized."""
        with pytest.raises(SystemExit) as exc:
            main(["vm", "stop", "--help"])
        assert exc.value.code == 0

    def test_vm_destroy_help(self, capsys):
        """kento vm destroy --help is recognized."""
        with pytest.raises(SystemExit) as exc:
            main(["vm", "destroy", "--help"])
        assert exc.value.code == 0

    def test_vm_list_help(self, capsys):
        """kento vm list --help is recognized."""
        with pytest.raises(SystemExit) as exc:
            main(["vm", "list", "--help"])
        assert exc.value.code == 0

    def test_vm_subcommands_in_help(self, capsys):
        """kento vm --help shows all subcommands."""
        with pytest.raises(SystemExit) as exc:
            main(["vm", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "create" in output
        assert "start" in output
        assert "shutdown" in output
        assert "destroy" in output
        assert "list" in output


class TestTopLevelHelp:
    """Test top-level help includes both container and vm groups."""

    def test_help_shows_container_and_vm(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "container" in output
        assert "vm" in output

    def test_version_still_works(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "kento" in output

    def test_no_args_shows_help(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main([])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "container" in output
        assert "vm" in output


class TestParseNetwork:
    """Tests for _parse_network() validation logic."""

    def test_none_returns_none_none(self):
        assert _parse_network(None, None) == (None, None)

    def test_bridge_no_name(self):
        assert _parse_network("bridge", "lxc") == ("bridge", None)

    def test_bridge_with_name(self):
        assert _parse_network("bridge=vmbr0", "lxc") == ("bridge", "vmbr0")

    def test_host_mode(self):
        assert _parse_network("host", "lxc") == ("host", None)

    def test_host_errors_for_vm(self):
        with pytest.raises(SystemExit):
            _parse_network("host", "vm")

    def test_usermode(self):
        assert _parse_network("usermode", "vm") == ("usermode", None)

    def test_usermode_errors_for_lxc(self):
        with pytest.raises(SystemExit):
            _parse_network("usermode", "lxc")

    def test_usermode_errors_for_pve(self):
        with pytest.raises(SystemExit):
            _parse_network("usermode", "pve")

    def test_usermode_allowed_for_bare(self):
        """Bare command (mode=None) allows usermode since it might be VM."""
        assert _parse_network("usermode", None) == ("usermode", None)

    def test_none_mode(self):
        assert _parse_network("none", "lxc") == ("none", None)

    def test_unknown_mode_errors(self):
        with pytest.raises(SystemExit):
            _parse_network("invalid", "lxc")

    def test_bridge_empty_name_errors(self):
        with pytest.raises(SystemExit):
            _parse_network("bridge=", "lxc")

    def test_host_allowed_for_bare(self):
        """Bare command (mode=None) allows host."""
        assert _parse_network("host", None) == ("host", None)

    def test_bridge_with_custom_name(self):
        assert _parse_network("bridge=br-lan", "pve") == ("bridge", "br-lan")
