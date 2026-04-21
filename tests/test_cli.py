"""Tests for CLI argument parsing."""

from pathlib import Path
from unittest.mock import patch, MagicMock

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
    assert "0.8.0" in output


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


class TestPullCommand:
    """Tests for the bare-only 'kento pull' command."""

    def test_pull_help(self, capsys):
        """kento pull --help is recognized."""
        with pytest.raises(SystemExit) as exc:
            main(["pull", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "image" in output

    def test_pull_requires_image(self, capsys):
        """kento pull (no image) should error."""
        with pytest.raises(SystemExit) as exc:
            main(["pull"])
        assert exc.value.code != 0

    def test_pull_calls_podman(self):
        """kento pull <image> calls podman pull with the image arg."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("kento.require_root"), \
             patch("subprocess.run", return_value=mock_result) as mock_run:
            main(["pull", "docker.io/library/alpine:3"])
        mock_run.assert_called_once_with(["podman", "pull", "docker.io/library/alpine:3"])

    def test_pull_forwards_exit_code(self):
        """kento pull forwards non-zero exit codes from podman."""
        mock_result = MagicMock()
        mock_result.returncode = 125
        with patch("kento.require_root"), \
             patch("subprocess.run", return_value=mock_result):
            with pytest.raises(SystemExit) as exc:
                main(["pull", "nonexistent/image:latest"])
            assert exc.value.code == 125

    def test_pull_not_under_container(self, capsys):
        """kento container pull should not dispatch to pull."""
        with pytest.raises(SystemExit) as exc:
            main(["container", "pull", "alpine:3"])
        # argparse should reject this since pull is not a container subcommand
        assert exc.value.code != 0

    def test_pull_not_under_vm(self, capsys):
        """kento vm pull should not dispatch to pull."""
        with pytest.raises(SystemExit) as exc:
            main(["vm", "pull", "alpine:3"])
        # argparse should reject this since pull is not a vm subcommand
        assert exc.value.code != 0


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


def _make_container(base: Path, dirname: str, name: str, mode: str) -> Path:
    """Create a minimal container directory with kento metadata files."""
    d = base / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / "kento-name").write_text(name)
    (d / "kento-mode").write_text(mode)
    (d / "kento-image").write_text("test-image")
    return d


class TestDispatchScope:
    """Test that scoped commands (kento vm / kento container) resolve to the
    correct namespace when a name exists in both LXC_BASE and VM_BASE.

    This is the regression test for the T3 dispatch scope bug.
    """

    def test_vm_scope_starts_vm_not_lxc(self, tmp_path):
        """kento vm start X should start the VM, not the LXC container."""
        lxc_base = tmp_path / "lxc"
        vm_base = tmp_path / "vm"
        lxc_dir = _make_container(lxc_base, "mybox", "mybox", "lxc")
        vm_dir = _make_container(vm_base, "mybox", "mybox", "vm")

        mock_start = MagicMock()
        with patch("kento.LXC_BASE", lxc_base), \
             patch("kento.VM_BASE", vm_base), \
             patch("kento.start.start", mock_start):
            main(["vm", "start", "mybox"])

        mock_start.assert_called_once_with(
            "mybox", container_dir=vm_dir, mode="vm",
        )

    def test_container_scope_starts_lxc_not_vm(self, tmp_path):
        """kento container start X should start the LXC container, not the VM."""
        lxc_base = tmp_path / "lxc"
        vm_base = tmp_path / "vm"
        lxc_dir = _make_container(lxc_base, "mybox", "mybox", "lxc")
        vm_dir = _make_container(vm_base, "mybox", "mybox", "vm")

        mock_start = MagicMock()
        with patch("kento.LXC_BASE", lxc_base), \
             patch("kento.VM_BASE", vm_base), \
             patch("kento.start.start", mock_start):
            main(["container", "start", "mybox"])

        mock_start.assert_called_once_with(
            "mybox", container_dir=lxc_dir, mode="lxc",
        )

    def test_bare_start_errors_on_ambiguous_name(self, tmp_path):
        """kento start X (bare) should error when name exists in both namespaces."""
        lxc_base = tmp_path / "lxc"
        vm_base = tmp_path / "vm"
        _make_container(lxc_base, "mybox", "mybox", "lxc")
        _make_container(vm_base, "mybox", "mybox", "vm")

        with patch("kento.LXC_BASE", lxc_base), \
             patch("kento.VM_BASE", vm_base):
            with pytest.raises(SystemExit) as exc:
                main(["start", "mybox"])
            assert exc.value.code == 1

    def test_vm_scope_shutdown(self, tmp_path):
        """kento vm shutdown X should shut down the VM, not the LXC container."""
        lxc_base = tmp_path / "lxc"
        vm_base = tmp_path / "vm"
        lxc_dir = _make_container(lxc_base, "mybox", "mybox", "lxc")
        vm_dir = _make_container(vm_base, "mybox", "mybox", "vm")

        mock_shutdown = MagicMock()
        with patch("kento.LXC_BASE", lxc_base), \
             patch("kento.VM_BASE", vm_base), \
             patch("kento.stop.shutdown", mock_shutdown):
            main(["vm", "shutdown", "mybox"])

        mock_shutdown.assert_called_once_with(
            "mybox", force=False, container_dir=vm_dir, mode="vm",
        )

    def test_container_scope_destroy(self, tmp_path):
        """kento container destroy X should destroy the LXC container, not the VM."""
        lxc_base = tmp_path / "lxc"
        vm_base = tmp_path / "vm"
        lxc_dir = _make_container(lxc_base, "mybox", "mybox", "lxc")
        vm_dir = _make_container(vm_base, "mybox", "mybox", "vm")

        mock_destroy = MagicMock()
        with patch("kento.LXC_BASE", lxc_base), \
             patch("kento.VM_BASE", vm_base), \
             patch("kento.destroy.destroy", mock_destroy):
            main(["container", "destroy", "mybox"])

        mock_destroy.assert_called_once_with(
            "mybox", force=False, container_dir=lxc_dir, mode="lxc",
        )


class TestRunCommand:
    """Tests for the 'kento run' command (create + start)."""

    def test_bare_run_help(self, capsys):
        """kento run --help is recognized and shows create-like flags."""
        with pytest.raises(SystemExit) as exc:
            main(["run", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--name" in output
        assert "--vm" in output
        assert "--force" in output
        assert "--start" not in output  # run has no --start flag

    def test_container_run_help(self, capsys):
        """kento container run --help is recognized."""
        with pytest.raises(SystemExit) as exc:
            main(["container", "run", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--name" in output

    def test_vm_run_help(self, capsys):
        """kento vm run --help is recognized."""
        with pytest.raises(SystemExit) as exc:
            main(["vm", "run", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--name" in output

    def test_bare_run_requires_image(self, capsys):
        """kento run (no image) should error."""
        with pytest.raises(SystemExit) as exc:
            main(["run"])
        assert exc.value.code != 0

    def test_run_dispatches_create_with_start_true(self):
        """kento run debian:12 dispatches to create with start=True."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["run", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args
        assert call_kwargs[1]["start"] is True

    def test_run_with_name_flag(self):
        """kento run --name mybox debian:12 passes name through."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["run", "--name", "mybox", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args
        assert call_kwargs[1]["name"] == "mybox"
        assert call_kwargs[1]["start"] is True

    def test_run_with_vm_flag(self):
        """kento run --vm debian:12 passes mode=vm through."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["run", "--vm", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args
        assert call_kwargs[1]["mode"] == "vm"
        assert call_kwargs[1]["start"] is True

    def test_vm_run_forces_vm_mode(self):
        """kento vm run debian:12 forces VM mode."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["vm", "run", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args
        assert call_kwargs[1]["mode"] == "vm"
        assert call_kwargs[1]["start"] is True

    def test_run_in_subcommand_help(self, capsys):
        """run appears in container and vm help output."""
        with pytest.raises(SystemExit) as exc:
            main(["container", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "run" in output

    def test_run_in_vm_help(self, capsys):
        """run appears in vm help output."""
        with pytest.raises(SystemExit) as exc:
            main(["vm", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "run" in output

    def test_run_in_top_level_help(self, capsys):
        """run appears in top-level help output."""
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "run" in output


class TestSSHKeyFlag:
    """Tests for the --ssh-key flag on create and run."""

    def test_ssh_key_in_create_help(self, capsys):
        """--ssh-key appears in create --help."""
        with pytest.raises(SystemExit) as exc:
            main(["create", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--ssh-key" in output

    def test_ssh_key_in_run_help(self, capsys):
        """--ssh-key appears in run --help."""
        with pytest.raises(SystemExit) as exc:
            main(["run", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--ssh-key" in output

    def test_ssh_key_in_container_create_help(self, capsys):
        """--ssh-key appears in container create --help."""
        with pytest.raises(SystemExit) as exc:
            main(["container", "create", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--ssh-key" in output

    def test_ssh_key_repeatable(self):
        """--ssh-key PATH can be given multiple times and produces a list."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["create",
                  "--ssh-key", "/tmp/key1.pub",
                  "--ssh-key", "/tmp/key2.pub",
                  "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["ssh_keys"] == ["/tmp/key1.pub", "/tmp/key2.pub"]

    def test_ssh_key_passes_through_to_create(self):
        """--ssh-key PATH reaches create() as ssh_keys=[...]."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["create", "--ssh-key", "/tmp/mykey.pub", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["ssh_keys"] == ["/tmp/mykey.pub"]

    def test_ssh_key_default_none(self):
        """Without --ssh-key, ssh_keys is None."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["create", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["ssh_keys"] is None

    def test_ssh_key_passes_through_from_run(self):
        """--ssh-key reaches create() when used via run."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["run", "--ssh-key", "/tmp/mykey.pub", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["ssh_keys"] == ["/tmp/mykey.pub"]
        assert call_kwargs["start"] is True


class TestSSHKeyUserFlag:
    """Tests for the --ssh-key-user flag on create and run."""

    def test_ssh_key_user_in_create_help(self, capsys):
        """--ssh-key-user appears in create --help."""
        with pytest.raises(SystemExit) as exc:
            main(["create", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--ssh-key-user" in output

    def test_ssh_key_user_in_run_help(self, capsys):
        """--ssh-key-user appears in run --help."""
        with pytest.raises(SystemExit) as exc:
            main(["run", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--ssh-key-user" in output

    def test_ssh_key_user_default_root(self):
        """Without --ssh-key-user, ssh_key_user defaults to 'root'."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["create", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["ssh_key_user"] == "root"

    def test_ssh_key_user_custom_value(self):
        """--ssh-key-user droste passes through to create()."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["create", "--ssh-key-user", "droste", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["ssh_key_user"] == "droste"

    def test_ssh_key_user_passes_through_from_run(self):
        """--ssh-key-user reaches create() when used via run."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["run", "--ssh-key-user", "droste", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["ssh_key_user"] == "droste"
        assert call_kwargs["start"] is True

    def test_ssh_key_user_without_ssh_key_is_harmless(self):
        """--ssh-key-user without --ssh-key doesn't error."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["create", "--ssh-key-user", "droste", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["ssh_keys"] is None
        assert call_kwargs["ssh_key_user"] == "droste"


class TestSSHHostKeyFlags:
    """Tests for --ssh-host-keys and --ssh-host-key-dir flags."""

    def test_ssh_host_keys_in_create_help(self, capsys):
        """--ssh-host-keys appears in create --help."""
        with pytest.raises(SystemExit) as exc:
            main(["create", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--ssh-host-keys" in output

    def test_ssh_host_key_dir_in_create_help(self, capsys):
        """--ssh-host-key-dir appears in create --help."""
        with pytest.raises(SystemExit) as exc:
            main(["create", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--ssh-host-key-dir" in output

    def test_ssh_host_keys_in_run_help(self, capsys):
        """--ssh-host-keys appears in run --help."""
        with pytest.raises(SystemExit) as exc:
            main(["run", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--ssh-host-keys" in output

    def test_ssh_host_key_dir_in_run_help(self, capsys):
        """--ssh-host-key-dir appears in run --help."""
        with pytest.raises(SystemExit) as exc:
            main(["run", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--ssh-host-key-dir" in output

    def test_mutually_exclusive(self, capsys):
        """--ssh-host-keys and --ssh-host-key-dir cannot be used together."""
        with pytest.raises(SystemExit) as exc:
            main(["create", "--ssh-host-keys", "--ssh-host-key-dir", "/tmp/keys",
                  "debian:12"])
        assert exc.value.code != 0

    def test_ssh_host_keys_passes_through(self):
        """--ssh-host-keys reaches create() as ssh_host_keys=True."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["create", "--ssh-host-keys", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["ssh_host_keys"] is True
        assert call_kwargs["ssh_host_key_dir"] is None

    def test_ssh_host_key_dir_passes_through(self):
        """--ssh-host-key-dir PATH reaches create() as ssh_host_key_dir=PATH."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["create", "--ssh-host-key-dir", "/tmp/mykeys", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["ssh_host_key_dir"] == "/tmp/mykeys"
        assert call_kwargs["ssh_host_keys"] is False

    def test_ssh_host_keys_default_false(self):
        """Without --ssh-host-keys, ssh_host_keys is False."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["create", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["ssh_host_keys"] is False
        assert call_kwargs["ssh_host_key_dir"] is None

    def test_ssh_host_keys_via_run(self):
        """--ssh-host-keys reaches create() when used via run."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["run", "--ssh-host-keys", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["ssh_host_keys"] is True
        assert call_kwargs["start"] is True

    def test_ssh_host_key_dir_via_vm_create(self):
        """--ssh-host-key-dir reaches create() via vm create."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["vm", "create", "--ssh-host-key-dir", "/tmp/k", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["ssh_host_key_dir"] == "/tmp/k"


class TestMacFlag:
    """Tests for the --mac flag on create and run."""

    def test_mac_in_create_help(self, capsys):
        """--mac appears in create --help."""
        with pytest.raises(SystemExit) as exc:
            main(["create", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--mac" in output

    def test_mac_in_run_help(self, capsys):
        """--mac appears in run --help."""
        with pytest.raises(SystemExit) as exc:
            main(["run", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--mac" in output

    def test_mac_valid_passes_through(self):
        """A valid --mac value reaches create() unchanged."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["create", "--mac", "52:54:00:ab:cd:ef", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["mac"] == "52:54:00:ab:cd:ef"

    def test_mac_default_none(self):
        """Without --mac, mac is None (auto-generate in create)."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["create", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["mac"] is None

    def test_mac_invalid_format_rejected(self, capsys):
        """An invalid --mac value is rejected with an argparse error."""
        with pytest.raises(SystemExit) as exc:
            main(["create", "--mac", "not-a-mac", "debian:12"])
        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert "invalid MAC" in err or "MAC" in err

    def test_mac_too_short_rejected(self, capsys):
        """Too few octets → rejected."""
        with pytest.raises(SystemExit) as exc:
            main(["create", "--mac", "52:54:00:ab:cd", "debian:12"])
        assert exc.value.code != 0

    def test_mac_non_hex_rejected(self, capsys):
        """Non-hex characters → rejected."""
        with pytest.raises(SystemExit) as exc:
            main(["create", "--mac", "52:54:00:gg:cd:ef", "debian:12"])
        assert exc.value.code != 0

    def test_mac_accepts_uppercase(self):
        """Uppercase hex accepted."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["create", "--mac", "AA:BB:CC:DD:EE:FF", "debian:12"])
        mock_create.assert_called_once()
        assert mock_create.call_args[1]["mac"] == "AA:BB:CC:DD:EE:FF"

    def test_mac_reaches_create_via_run(self):
        """--mac via 'run' also reaches create()."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["run", "--mac", "52:54:00:11:22:33", "debian:12"])
        mock_create.assert_called_once()
        assert mock_create.call_args[1]["mac"] == "52:54:00:11:22:33"
        assert mock_create.call_args[1]["start"] is True


class TestPortNetworkValidation:
    """Tests for --port + --network CLI-level validation (Phase 3)."""

    def test_port_with_host_errors(self, capsys):
        """--port with --network host exits with error."""
        with pytest.raises(SystemExit) as exc:
            main(["create", "--port", "10022:22", "--network", "host",
                  "debian:12"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "host" in err or "none" in err

    def test_port_with_none_errors(self, capsys):
        """--port with --network none exits with error."""
        with pytest.raises(SystemExit) as exc:
            main(["create", "--port", "10022:22", "--network", "none",
                  "debian:12"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "host" in err or "none" in err

    def test_port_with_bridge_passes_to_create(self):
        """--port with --network bridge reaches create() (valid for LXC)."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["create", "--port", "10022:22", "--network", "bridge=lxcbr0",
                  "debian:12"])
        mock_create.assert_called_once()
        assert mock_create.call_args[1]["port"] == "10022:22"

    def test_port_without_network_passes_to_create(self):
        """--port without --network reaches create() (auto-detect)."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["create", "--port", "10022:22", "debian:12"])
        mock_create.assert_called_once()
        assert mock_create.call_args[1]["port"] == "10022:22"
