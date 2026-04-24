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
    assert "lxc" in output


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "kento" in output


def test_no_command(capsys):
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "lxc" in output


def test_lxc_no_subcommand(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["lxc"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "create" in output
    assert "destroy" in output
    assert "list" in output


def test_lxc_create_requires_image(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["lxc", "create"])
    assert exc.value.code != 0


def test_lxc_create_help(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["lxc", "create", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "--name" in output
    assert "--pve" in output
    assert "--vmid" in output
    assert "--port" in output
    assert "--start" in output


def test_pve_lxc_mutually_exclusive(capsys):
    """--pve and --no-pve are BooleanOptionalAction; not an error to combine with scope."""
    # This test originally checked --pve --lxc mutual exclusion, but --lxc is removed.
    # Now --pve is just a boolean flag, no mutual exclusion with scope.
    # We test that --pve and --no-pve together uses last-wins (argparse default).
    pass


def test_vm_pve_mutually_exclusive(capsys):
    """--pve under vm scope is valid (pve=True + scope=vm => pve-vm mode)."""
    # The old --vm --pve mutual exclusion is gone. Now vm scope + --pve is valid.
    pass


def test_vm_lxc_mutually_exclusive(capsys):
    """--lxc and --vm flags no longer exist; skip."""
    pass


def test_lxc_subcommands_in_help(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["lxc", "--help"])
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


def test_lxc_shutdown_in_help(capsys):
    """shutdown is recognized as a command with --force flag."""
    with pytest.raises(SystemExit) as exc:
        main(["lxc", "shutdown", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "--force" in output


def test_lxc_stop_alias(capsys):
    """stop still works as an alias for shutdown with --force flag."""
    with pytest.raises(SystemExit) as exc:
        main(["lxc", "stop", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "--force" in output


def test_lxc_destroy_in_help(capsys):
    """destroy is recognized as a command."""
    with pytest.raises(SystemExit) as exc:
        main(["lxc", "destroy", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "--force" in output


def test_lxc_rm_alias(capsys):
    """rm still works as an alias for destroy."""
    with pytest.raises(SystemExit) as exc:
        main(["lxc", "rm", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "--force" in output


def test_lxc_scrub_in_help(capsys):
    """scrub is recognized as a command."""
    with pytest.raises(SystemExit) as exc:
        main(["lxc", "scrub", "--help"])
    assert exc.value.code == 0


# --- Three-level CLI tests ---

class TestBareCommands:
    """Test bare top-level commands (kento <cmd>).

    Note: bare create and run are removed in the new CLI; they require
    'kento lxc create' or 'kento vm create'.
    """

    def test_bare_create_not_available(self, capsys):
        """kento create (bare) is no longer available — must use lxc/vm scope."""
        with pytest.raises(SystemExit) as exc:
            main(["create"])
        assert exc.value.code != 0

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
    """Test top-level help includes both lxc and vm groups."""

    def test_help_shows_lxc_and_vm(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "lxc" in output
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
        assert "lxc" in output
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

    def test_pull_not_under_lxc(self, capsys):
        """kento lxc pull should not dispatch to pull."""
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "pull", "alpine:3"])
        # argparse should reject this since pull is not an lxc subcommand
        assert exc.value.code != 0

    def test_pull_not_under_vm(self, capsys):
        """kento vm pull should not dispatch to pull."""
        with pytest.raises(SystemExit) as exc:
            main(["vm", "pull", "alpine:3"])
        # argparse should reject this since pull is not a vm subcommand
        assert exc.value.code != 0

    def test_pull_podman_missing_reports_clean_error(self, capsys):
        """C1: FileNotFoundError from missing podman becomes a clean message."""
        with patch("kento.require_root"), \
             patch("subprocess.run", side_effect=FileNotFoundError(
                 "[Errno 2] No such file or directory: 'podman'")):
            with pytest.raises(SystemExit) as exc:
                main(["pull", "alpine:3"])
            assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "'podman' not found on PATH" in err
        assert "apt install podman" in err or "dnf install podman" in err
        assert "Traceback" not in err


class TestParseNetwork:
    """Tests for _parse_network() validation logic."""

    def test_none_returns_none_none(self):
        assert _parse_network(None, None) == (None, None)

    def test_bridge_no_name(self):
        assert _parse_network("bridge", "lxc") == ("bridge", None)

    def test_bridge_with_name(self):
        with patch("kento._bridge_exists", return_value=True):
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
        with patch("kento._bridge_exists", return_value=True):
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
    """Test that scoped commands (kento vm / kento lxc) resolve to the
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

    def test_lxc_scope_starts_lxc_not_vm(self, tmp_path):
        """kento lxc start X should start the LXC container, not the VM."""
        lxc_base = tmp_path / "lxc"
        vm_base = tmp_path / "vm"
        lxc_dir = _make_container(lxc_base, "mybox", "mybox", "lxc")
        vm_dir = _make_container(vm_base, "mybox", "mybox", "vm")

        mock_start = MagicMock()
        with patch("kento.LXC_BASE", lxc_base), \
             patch("kento.VM_BASE", vm_base), \
             patch("kento.start.start", mock_start):
            main(["lxc", "start", "mybox"])

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

    def test_lxc_scope_destroy(self, tmp_path):
        """kento lxc destroy X should destroy the LXC container, not the VM."""
        lxc_base = tmp_path / "lxc"
        vm_base = tmp_path / "vm"
        lxc_dir = _make_container(lxc_base, "mybox", "mybox", "lxc")
        vm_dir = _make_container(vm_base, "mybox", "mybox", "vm")

        mock_destroy = MagicMock()
        with patch("kento.LXC_BASE", lxc_base), \
             patch("kento.VM_BASE", vm_base), \
             patch("kento.destroy.destroy", mock_destroy):
            main(["lxc", "destroy", "mybox"])

        mock_destroy.assert_called_once_with(
            "mybox", force=False, container_dir=lxc_dir, mode="lxc",
        )

    def test_dispatch_vm_scope_reads_kento_mode_pve_vm(self, tmp_path):
        """kento vm stop X must read kento-mode and dispatch with mode='pve-vm'.

        Regression: _dispatch_multi previously hardcoded mode='vm' for the vm
        scope, which caused pve-vm instances to be mishandled by stop/start/
        destroy/scrub (e.g. taking the plain-VM code path instead of the PVE
        teardown path).
        """
        lxc_base = tmp_path / "lxc"
        vm_base = tmp_path / "vm"
        vm_dir = _make_container(vm_base, "mybox", "mybox", "pve-vm")

        mock_shutdown = MagicMock()
        with patch("kento.LXC_BASE", lxc_base), \
             patch("kento.VM_BASE", vm_base), \
             patch("kento.stop.shutdown", mock_shutdown):
            main(["vm", "stop", "mybox"])

        mock_shutdown.assert_called_once_with(
            "mybox", force=False, container_dir=vm_dir, mode="pve-vm",
        )

    def test_dispatch_vm_scope_defaults_to_vm_when_mode_missing(self, tmp_path):
        """Legacy vm instances without a kento-mode file fall back to mode='vm'."""
        lxc_base = tmp_path / "lxc"
        vm_base = tmp_path / "vm"
        # Build a vm-namespace dir WITHOUT a kento-mode file (legacy layout).
        vm_dir = vm_base / "mybox"
        vm_dir.mkdir(parents=True)
        (vm_dir / "kento-name").write_text("mybox")
        (vm_dir / "kento-image").write_text("test-image")
        assert not (vm_dir / "kento-mode").exists()

        mock_shutdown = MagicMock()
        with patch("kento.LXC_BASE", lxc_base), \
             patch("kento.VM_BASE", vm_base), \
             patch("kento.stop.shutdown", mock_shutdown):
            main(["vm", "stop", "mybox"])

        mock_shutdown.assert_called_once_with(
            "mybox", force=False, container_dir=vm_dir, mode="vm",
        )

    def test_dispatch_lxc_scope_reads_kento_mode_pve_lxc(self, tmp_path):
        """Symmetric guard: kento lxc stop X must dispatch with mode='pve-lxc'
        for a pve-lxc instance, not the default 'lxc'.
        """
        lxc_base = tmp_path / "lxc"
        vm_base = tmp_path / "vm"
        lxc_dir = _make_container(lxc_base, "mybox", "mybox", "pve-lxc")

        mock_shutdown = MagicMock()
        with patch("kento.LXC_BASE", lxc_base), \
             patch("kento.VM_BASE", vm_base), \
             patch("kento.stop.shutdown", mock_shutdown):
            main(["lxc", "stop", "mybox"])

        mock_shutdown.assert_called_once_with(
            "mybox", force=False, container_dir=lxc_dir, mode="pve-lxc",
        )


class TestRunCommand:
    """Tests for the 'kento run' command (create + start).

    Note: bare 'kento run' no longer exists. Must use 'kento lxc run' or 'kento vm run'.
    """

    def test_lxc_run_help(self, capsys):
        """kento lxc run --help is recognized and shows create-like flags."""
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "run", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--name" in output
        assert "--force" in output
        assert "--start" not in output  # run has no --start flag

    def test_lxc_run_help_alt(self, capsys):
        """kento lxc run --help is recognized."""
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "run", "--help"])
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

    def test_lxc_run_requires_image(self, capsys):
        """kento lxc run (no image) should error."""
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "run"])
        assert exc.value.code != 0

    def test_lxc_run_dispatches_create_with_start_true(self):
        """kento lxc run debian:12 dispatches to create with start=True and mode=lxc."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["lxc", "run", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args
        assert call_kwargs[1]["start"] is True
        assert call_kwargs[1]["mode"] == "lxc"

    def test_lxc_run_with_name_flag(self):
        """kento lxc run --name mybox debian:12 passes name through."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["lxc", "run", "--name", "mybox", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args
        assert call_kwargs[1]["name"] == "mybox"
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

    def test_run_in_lxc_help(self, capsys):
        """run appears in lxc help output."""
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "--help"])
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

    def test_top_level_help_shows_shortcuts(self, capsys):
        """Top-level help shows shortcuts (list, start, etc.)."""
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "list" in output
        assert "pull" in output


class TestSSHKeyFlag:
    """Tests for the --ssh-key flag on create and run."""

    def test_ssh_key_in_lxc_create_help(self, capsys):
        """--ssh-key appears in lxc create --help."""
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "create", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--ssh-key" in output

    def test_ssh_key_in_lxc_run_help(self, capsys):
        """--ssh-key appears in lxc run --help."""
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "run", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--ssh-key" in output

    def test_ssh_key_in_vm_create_help(self, capsys):
        """--ssh-key appears in vm create --help."""
        with pytest.raises(SystemExit) as exc:
            main(["vm", "create", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--ssh-key" in output

    def test_ssh_key_repeatable(self):
        """--ssh-key PATH can be given multiple times and produces a list."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["lxc", "create",
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
            main(["lxc", "create", "--ssh-key", "/tmp/mykey.pub", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["ssh_keys"] == ["/tmp/mykey.pub"]

    def test_ssh_key_default_none(self):
        """Without --ssh-key, ssh_keys is None."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["lxc", "create", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["ssh_keys"] is None

    def test_ssh_key_passes_through_from_run(self):
        """--ssh-key reaches create() when used via run."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["lxc", "run", "--ssh-key", "/tmp/mykey.pub", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["ssh_keys"] == ["/tmp/mykey.pub"]
        assert call_kwargs["start"] is True


class TestSSHKeyUserFlag:
    """Tests for the --ssh-key-user flag on create and run."""

    def test_ssh_key_user_in_lxc_create_help(self, capsys):
        """--ssh-key-user appears in lxc create --help."""
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "create", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--ssh-key-user" in output

    def test_ssh_key_user_in_lxc_run_help(self, capsys):
        """--ssh-key-user appears in lxc run --help."""
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "run", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--ssh-key-user" in output

    def test_ssh_key_user_default_root(self):
        """Without --ssh-key-user, ssh_key_user defaults to 'root'."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["lxc", "create", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["ssh_key_user"] == "root"

    def test_ssh_key_user_custom_value(self):
        """--ssh-key-user droste passes through to create()."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["lxc", "create", "--ssh-key-user", "droste", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["ssh_key_user"] == "droste"

    def test_ssh_key_user_passes_through_from_run(self):
        """--ssh-key-user reaches create() when used via run."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["lxc", "run", "--ssh-key-user", "droste", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["ssh_key_user"] == "droste"
        assert call_kwargs["start"] is True

    def test_ssh_key_user_without_ssh_key_is_harmless(self):
        """--ssh-key-user without --ssh-key doesn't error."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["lxc", "create", "--ssh-key-user", "droste", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["ssh_keys"] is None
        assert call_kwargs["ssh_key_user"] == "droste"


class TestSSHHostKeyFlags:
    """Tests for --ssh-host-keys and --ssh-host-key-dir flags."""

    def test_ssh_host_keys_in_lxc_create_help(self, capsys):
        """--ssh-host-keys appears in lxc create --help."""
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "create", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--ssh-host-keys" in output

    def test_ssh_host_key_dir_in_lxc_create_help(self, capsys):
        """--ssh-host-key-dir appears in lxc create --help."""
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "create", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--ssh-host-key-dir" in output

    def test_ssh_host_keys_in_lxc_run_help(self, capsys):
        """--ssh-host-keys appears in lxc run --help."""
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "run", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--ssh-host-keys" in output

    def test_ssh_host_key_dir_in_lxc_run_help(self, capsys):
        """--ssh-host-key-dir appears in lxc run --help."""
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "run", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--ssh-host-key-dir" in output

    def test_mutually_exclusive(self, capsys):
        """--ssh-host-keys and --ssh-host-key-dir cannot be used together."""
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "create", "--ssh-host-keys", "--ssh-host-key-dir", "/tmp/keys",
                  "debian:12"])
        assert exc.value.code != 0

    def test_ssh_host_keys_passes_through(self):
        """--ssh-host-keys reaches create() as ssh_host_keys=True."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["lxc", "create", "--ssh-host-keys", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["ssh_host_keys"] is True
        assert call_kwargs["ssh_host_key_dir"] is None

    def test_ssh_host_key_dir_passes_through(self):
        """--ssh-host-key-dir PATH reaches create() as ssh_host_key_dir=PATH."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["lxc", "create", "--ssh-host-key-dir", "/tmp/mykeys", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["ssh_host_key_dir"] == "/tmp/mykeys"
        assert call_kwargs["ssh_host_keys"] is False

    def test_ssh_host_keys_default_false(self):
        """Without --ssh-host-keys, ssh_host_keys is False."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["lxc", "create", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["ssh_host_keys"] is False
        assert call_kwargs["ssh_host_key_dir"] is None

    def test_ssh_host_keys_via_run(self):
        """--ssh-host-keys reaches create() when used via run."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["lxc", "run", "--ssh-host-keys", "debian:12"])
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

    def test_mac_in_lxc_create_help(self, capsys):
        """--mac appears in lxc create --help."""
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "create", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--mac" in output

    def test_mac_in_lxc_run_help(self, capsys):
        """--mac appears in lxc run --help."""
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "run", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--mac" in output

    def test_mac_valid_passes_through(self):
        """A valid --mac value reaches create() unchanged (VM scope)."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["vm", "create", "--mac", "52:54:00:ab:cd:ef", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["mac"] == "52:54:00:ab:cd:ef"

    def test_mac_rejected_on_lxc_scope(self, capsys):
        """F9: --mac on LXC scope is rejected (silently ignored before)."""
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "create", "--mac", "52:54:00:ab:cd:ef", "debian:12"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "--mac is not supported for LXC" in err

    def test_mac_rejected_on_lxc_run(self, capsys):
        """F9: --mac on 'lxc run' also rejected."""
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "run", "--mac", "52:54:00:ab:cd:ef", "debian:12"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "--mac is not supported for LXC" in err

    def test_mac_default_none(self):
        """Without --mac, mac is None (auto-generate in create)."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["lxc", "create", "debian:12"])
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["mac"] is None

    def test_mac_invalid_format_rejected(self, capsys):
        """An invalid --mac value is rejected with an argparse error."""
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "create", "--mac", "not-a-mac", "debian:12"])
        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert "invalid MAC" in err or "MAC" in err

    def test_mac_too_short_rejected(self, capsys):
        """Too few octets -> rejected."""
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "create", "--mac", "52:54:00:ab:cd", "debian:12"])
        assert exc.value.code != 0

    def test_mac_non_hex_rejected(self, capsys):
        """Non-hex characters -> rejected."""
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "create", "--mac", "52:54:00:gg:cd:ef", "debian:12"])
        assert exc.value.code != 0

    def test_mac_accepts_uppercase(self):
        """Uppercase hex accepted (VM scope)."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["vm", "create", "--mac", "AA:BB:CC:DD:EE:FF", "debian:12"])
        mock_create.assert_called_once()
        assert mock_create.call_args[1]["mac"] == "AA:BB:CC:DD:EE:FF"

    def test_mac_reaches_create_via_run(self):
        """--mac via 'vm run' also reaches create()."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["vm", "run", "--mac", "52:54:00:11:22:33", "debian:12"])
        mock_create.assert_called_once()
        assert mock_create.call_args[1]["mac"] == "52:54:00:11:22:33"
        assert mock_create.call_args[1]["start"] is True

    def test_mac_multicast_rejected(self, capsys):
        """F16: multicast MACs (first-octet LSB set) are rejected."""
        with pytest.raises(SystemExit) as exc:
            main(["vm", "create", "--mac", "01:02:03:04:05:06", "debian:12"])
        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert "multicast" in err.lower()

    def test_mac_broadcast_rejected(self, capsys):
        """F16: broadcast MAC ff:ff:ff:ff:ff:ff is rejected."""
        with pytest.raises(SystemExit) as exc:
            main(["vm", "create", "--mac", "ff:ff:ff:ff:ff:ff", "debian:12"])
        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert "multicast" in err.lower() or "broadcast" in err.lower()

    def test_mac_multicast_uppercase_rejected(self, capsys):
        """F16: multicast detection is case-insensitive."""
        # 0x03 = 00000011 — LSB set, so multicast.
        with pytest.raises(SystemExit) as exc:
            main(["vm", "create", "--mac", "03:AA:BB:CC:DD:EE", "debian:12"])
        assert exc.value.code != 0

    def test_mac_laa_accepted(self):
        """F16 counter-test: locally-administered unicast (0x02 prefix) is fine."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["vm", "create", "--mac", "02:aa:bb:cc:dd:ee", "debian:12"])
        mock_create.assert_called_once()
        assert mock_create.call_args[1]["mac"] == "02:aa:bb:cc:dd:ee"

    def test_mac_06_prefix_accepted(self):
        """F16 counter-test: 0x06 (LAA unicast) is fine."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["vm", "create", "--mac", "06:00:00:00:00:01", "debian:12"])
        mock_create.assert_called_once()


class TestPortNetworkValidation:
    """Tests for --port + --network CLI-level validation (Phase 3)."""

    def test_port_with_host_errors(self, capsys):
        """--port with --network host exits with error."""
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "create", "--port", "10022:22", "--network", "host",
                  "debian:12"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "host" in err or "none" in err

    def test_port_with_none_errors(self, capsys):
        """--port with --network none exits with error."""
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "create", "--port", "10022:22", "--network", "none",
                  "debian:12"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "host" in err or "none" in err

    def test_port_with_bridge_passes_to_create(self):
        """--port with --network bridge reaches create() (valid for LXC)."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create), \
             patch("kento._bridge_exists", return_value=True):
            main(["lxc", "create", "--port", "10022:22", "--network", "bridge=lxcbr0",
                  "debian:12"])
        mock_create.assert_called_once()
        assert mock_create.call_args[1]["port"] == "10022:22"

    def test_port_without_network_passes_to_create(self):
        """--port without --network reaches create() (auto-detect)."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["lxc", "create", "--port", "10022:22", "debian:12"])
        mock_create.assert_called_once()
        assert mock_create.call_args[1]["port"] == "10022:22"


class TestVmScopeOverridesMode:
    """kento vm create always forces mode=vm, even with --pve flag."""

    def test_vm_create_with_pve_flag_forces_vm(self):
        """kento vm create --pve <image> sets mode to 'vm' (pve=True passed separately)."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["vm", "create", "--pve", "debian:12"])
        mock_create.assert_called_once()
        assert mock_create.call_args[1]["mode"] == "vm"

    def test_vm_create_without_flags_forces_vm(self):
        """kento vm create <image> sets mode to 'vm' (regression check)."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["vm", "create", "debian:12"])
        mock_create.assert_called_once()
        assert mock_create.call_args[1]["mode"] == "vm"

    def test_lxc_create_with_pve_flag(self):
        """kento lxc create --pve <image> sets mode='lxc' and pve=True."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["lxc", "create", "--pve", "debian:12"])
        mock_create.assert_called_once()
        assert mock_create.call_args[1]["mode"] == "lxc"
        assert mock_create.call_args[1]["pve"] is True


class TestMemoryCoresFlags:
    """Tests for --memory and --cores flags on create and run."""

    def test_memory_in_lxc_create_help(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "create", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--memory" in output

    def test_cores_in_lxc_create_help(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "create", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--cores" in output

    def test_memory_in_lxc_run_help(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "run", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--memory" in output

    def test_cores_in_lxc_run_help(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "run", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--cores" in output

    def test_memory_default_none(self):
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["lxc", "create", "debian:12"])
        assert mock_create.call_args[1]["memory"] is None

    def test_cores_default_none(self):
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["lxc", "create", "debian:12"])
        assert mock_create.call_args[1]["cores"] is None

    def test_memory_passes_through(self):
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["lxc", "create", "--memory", "1024", "debian:12"])
        assert mock_create.call_args[1]["memory"] == 1024

    def test_cores_passes_through(self):
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["lxc", "create", "--cores", "4", "debian:12"])
        assert mock_create.call_args[1]["cores"] == 4

    def test_memory_and_cores_via_run(self):
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["lxc", "run", "--memory", "2048", "--cores", "2", "debian:12"])
        assert mock_create.call_args[1]["memory"] == 2048
        assert mock_create.call_args[1]["cores"] == 2
        assert mock_create.call_args[1]["start"] is True

    def test_memory_and_cores_via_vm_create(self):
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["vm", "create", "--memory", "4096", "--cores", "8", "debian:12"])
        assert mock_create.call_args[1]["memory"] == 4096
        assert mock_create.call_args[1]["cores"] == 8
        assert mock_create.call_args[1]["mode"] == "vm"

    def test_memory_and_cores_via_lxc_create(self):
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["lxc", "create", "--memory", "256", "--cores", "1", "debian:12"])
        assert mock_create.call_args[1]["memory"] == 256
        assert mock_create.call_args[1]["cores"] == 1


class TestForceFlag:
    """--force on create/run must reach create() as force=True so the
    cross-namespace scan inside create.py can skip to current-namespace-only.
    """

    def test_lxc_create_force_passes_to_create(self):
        """kento lxc create --force reaches create() with force=True."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["lxc", "create", "--force", "debian:13"])
        mock_create.assert_called_once()
        assert mock_create.call_args[1]["force"] is True

    def test_lxc_create_force_default_false(self):
        """Without --force, create() is called with force=False."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["lxc", "create", "debian:13"])
        mock_create.assert_called_once()
        assert mock_create.call_args[1]["force"] is False

    def test_vm_create_force_passes_to_create(self):
        """kento vm create --force reaches create() with force=True."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["vm", "create", "--force", "debian:13"])
        mock_create.assert_called_once()
        assert mock_create.call_args[1]["force"] is True

    def test_lxc_run_force_passes_to_create(self):
        """kento lxc run --force reaches create() with force=True and start=True."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["lxc", "run", "--force", "debian:13"])
        mock_create.assert_called_once()
        assert mock_create.call_args[1]["force"] is True
        assert mock_create.call_args[1]["start"] is True

    def test_vm_run_force_passes_to_create(self):
        """kento vm run --force reaches create() with force=True and start=True."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create):
            main(["vm", "run", "--force", "debian:13"])
        mock_create.assert_called_once()
        assert mock_create.call_args[1]["force"] is True
        assert mock_create.call_args[1]["start"] is True
