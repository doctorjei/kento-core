"""Tests for kento.validate_name — rejects names that enable injection or path traversal."""

import pytest

from kento import validate_name


class TestValidateNameAccepts:

    def test_simple_alphanumeric(self):
        # Should not raise
        validate_name("debian13")

    def test_with_underscore_dot_dash(self):
        validate_name("my.container_01-a")

    def test_single_letter(self):
        validate_name("a")

    def test_single_digit(self):
        validate_name("0")

    def test_exactly_63_chars(self):
        name = "a" * 63
        validate_name(name)


class TestValidateNameRejects:

    def test_empty_string(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            validate_name("")
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "cannot be empty" in err

    def test_none(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            validate_name(None)  # type: ignore[arg-type]
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "cannot be empty" in err

    def test_too_long_64_chars(self, capsys):
        name = "a" * 64
        with pytest.raises(SystemExit) as excinfo:
            validate_name(name)
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "too long" in err

    def test_too_long_200_chars(self, capsys):
        name = "a" * 200
        with pytest.raises(SystemExit) as excinfo:
            validate_name(name)
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "too long" in err

    def test_leading_dash(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            validate_name("-a")
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "invalid" in err

    def test_leading_underscore(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            validate_name("_a")
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "invalid" in err

    def test_leading_dot(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            validate_name(".a")
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "invalid" in err

    def test_contains_slash(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            validate_name("a/b")
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "invalid" in err

    def test_path_traversal(self, capsys):
        # "../foo" starts with "." so fails the leading-alphanumeric check,
        # and also contains "/" which isn't in the allowed set.
        with pytest.raises(SystemExit) as excinfo:
            validate_name("../foo")
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "invalid" in err

    def test_nul_byte(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            validate_name("a\x00b")
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "NUL byte" in err

    def test_whitespace_space(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            validate_name("a b")
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "invalid" in err

    def test_whitespace_tab(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            validate_name("a\tb")
        assert excinfo.value.code == 1

    def test_whitespace_newline(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            validate_name("a\nb")
        assert excinfo.value.code == 1

    def test_shell_double_quote(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            validate_name('x"y')
        assert excinfo.value.code == 1

    def test_shell_command_substitution(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            validate_name("$(whoami)")
        assert excinfo.value.code == 1

    def test_shell_semicolon(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            validate_name("a;b")
        assert excinfo.value.code == 1

    def test_shell_pipe(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            validate_name("a|b")
        assert excinfo.value.code == 1

    def test_shell_backtick(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            validate_name("a`b`c")
        assert excinfo.value.code == 1

    def test_shell_dollar(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            validate_name("a$b")
        assert excinfo.value.code == 1


class TestValidateNameWhatParameter:

    def test_custom_what_appears_in_error(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            validate_name("-bad", what="auto-generated name")
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "auto-generated name" in err

    def test_custom_what_in_empty_error(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            validate_name("", what="auto-generated name")
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "auto-generated name" in err
        assert "cannot be empty" in err

    def test_custom_what_in_too_long_error(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            validate_name("a" * 100, what="auto-generated name")
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "auto-generated name" in err
        assert "too long" in err

    def test_custom_what_in_nul_error(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            validate_name("a\x00b", what="auto-generated name")
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "auto-generated name" in err
        assert "NUL byte" in err

    def test_default_what_is_instance_name(self, capsys):
        with pytest.raises(SystemExit):
            validate_name("-bad")
        err = capsys.readouterr().err
        assert "instance name" in err


class TestCLIIntegration:
    """CLI entry points must reject bad names before any real work runs."""

    def test_create_rejects_shell_metacharacter_name(self, capsys):
        from kento import cli
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["lxc", "create", "debian:13", "--name", "bad;name"])
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "invalid instance name" in err

    def test_create_rejects_path_traversal_name(self, capsys):
        from kento import cli
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["lxc", "create", "debian:13", "--name", "../evil"])
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "invalid instance name" in err

    def test_start_rejects_slash_in_name(self, capsys):
        from kento import cli
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["start", "a/b"])
        # _dispatch_multi catches the inner SystemExit and re-exits 1.
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "invalid instance name" in err

    def test_info_rejects_double_quote_in_name(self, capsys):
        from kento import cli
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["info", 'x"y'])
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "invalid instance name" in err

    def test_valid_name_does_not_raise_validate_error(self, capsys, monkeypatch):
        """A valid name passes validate_name; later errors are fine, but the
        first error seen must NOT mention 'invalid instance name'."""
        from kento import cli

        # Suppress require_root so the path gets to the resolver, which will
        # error out on 'not found' — that's the error we expect, not a
        # validate_name rejection.
        monkeypatch.setattr("os.getuid", lambda: 0)

        # resolve_any will fail with "no instance named" / "Error: instance
        # not found". Both live in kento.__init__ and raise SystemExit.
        with pytest.raises(SystemExit):
            cli.main(["info", "valid-name-01"])
        err = capsys.readouterr().err
        assert "invalid instance name" not in err


class TestResolverValidateName:
    """Resolver entry points must validate names at the top."""

    def test_resolve_container_rejects_shell_metacharacter(self, capsys):
        from kento import resolve_container
        with pytest.raises(SystemExit) as excinfo:
            resolve_container("bad;name")
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "invalid instance name" in err

    def test_resolve_in_namespace_rejects_path_traversal(self, capsys):
        from kento import resolve_in_namespace
        with pytest.raises(SystemExit) as excinfo:
            resolve_in_namespace("../etc", "lxc")
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "invalid instance name" in err

    def test_resolve_any_rejects_nul_byte(self, capsys):
        from kento import resolve_any
        with pytest.raises(SystemExit) as excinfo:
            resolve_any("\x00")
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        # NUL triggers the empty-string branch first (since the initial
        # emptiness check fires on falsy input) or the explicit NUL branch.
        assert "NUL byte" in err or "cannot be empty" in err

    def test_resolve_any_rejects_embedded_nul(self, capsys):
        from kento import resolve_any
        with pytest.raises(SystemExit) as excinfo:
            resolve_any("a\x00b")
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "NUL byte" in err

    def test_check_name_conflict_rejects_slash(self, capsys):
        from kento import check_name_conflict
        with pytest.raises(SystemExit) as excinfo:
            check_name_conflict("a/b", "lxc")
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "invalid instance name" in err
