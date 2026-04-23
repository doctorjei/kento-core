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
