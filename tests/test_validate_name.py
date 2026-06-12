"""Tests for kento.validate_name — rejects names that enable injection or path traversal."""

import pytest

from kento import validate_name
from kento.errors import ValidationError, InstanceNotFoundError


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

    def test_empty_string(self):
        with pytest.raises(ValidationError, match="cannot be empty"):
            validate_name("")

    def test_none(self):
        with pytest.raises(ValidationError, match="cannot be empty"):
            validate_name(None)  # type: ignore[arg-type]

    def test_too_long_64_chars(self):
        name = "a" * 64
        with pytest.raises(ValidationError, match="too long"):
            validate_name(name)

    def test_too_long_200_chars(self):
        name = "a" * 200
        with pytest.raises(ValidationError, match="too long"):
            validate_name(name)

    def test_leading_dash(self):
        with pytest.raises(ValidationError, match="invalid"):
            validate_name("-a")

    def test_leading_underscore(self):
        with pytest.raises(ValidationError, match="invalid"):
            validate_name("_a")

    def test_leading_dot(self):
        with pytest.raises(ValidationError, match="invalid"):
            validate_name(".a")

    def test_contains_slash(self):
        with pytest.raises(ValidationError, match="invalid"):
            validate_name("a/b")

    def test_path_traversal(self):
        # "../foo" starts with "." so fails the leading-alphanumeric check,
        # and also contains "/" which isn't in the allowed set.
        with pytest.raises(ValidationError, match="invalid"):
            validate_name("../foo")

    def test_nul_byte(self):
        with pytest.raises(ValidationError, match="NUL byte"):
            validate_name("a\x00b")

    def test_whitespace_space(self):
        with pytest.raises(ValidationError, match="invalid"):
            validate_name("a b")

    def test_whitespace_tab(self):
        with pytest.raises(ValidationError):
            validate_name("a\tb")

    def test_whitespace_newline(self):
        with pytest.raises(ValidationError):
            validate_name("a\nb")

    def test_shell_double_quote(self):
        with pytest.raises(ValidationError):
            validate_name('x"y')

    def test_shell_command_substitution(self):
        with pytest.raises(ValidationError):
            validate_name("$(whoami)")

    def test_shell_semicolon(self):
        with pytest.raises(ValidationError):
            validate_name("a;b")

    def test_shell_pipe(self):
        with pytest.raises(ValidationError):
            validate_name("a|b")

    def test_shell_backtick(self):
        with pytest.raises(ValidationError):
            validate_name("a`b`c")

    def test_shell_dollar(self):
        with pytest.raises(ValidationError):
            validate_name("a$b")


class TestValidateNameWhatParameter:

    def test_custom_what_appears_in_error(self):
        with pytest.raises(ValidationError, match="auto-generated name"):
            validate_name("-bad", what="auto-generated name")

    def test_custom_what_in_empty_error(self):
        with pytest.raises(ValidationError) as exc_info:
            validate_name("", what="auto-generated name")
        msg = str(exc_info.value)
        assert "auto-generated name" in msg
        assert "cannot be empty" in msg

    def test_custom_what_in_too_long_error(self):
        with pytest.raises(ValidationError) as exc_info:
            validate_name("a" * 100, what="auto-generated name")
        msg = str(exc_info.value)
        assert "auto-generated name" in msg
        assert "too long" in msg

    def test_custom_what_in_nul_error(self):
        with pytest.raises(ValidationError) as exc_info:
            validate_name("a\x00b", what="auto-generated name")
        msg = str(exc_info.value)
        assert "auto-generated name" in msg
        assert "NUL byte" in msg

    def test_default_what_is_instance_name(self):
        with pytest.raises(ValidationError, match="instance name"):
            validate_name("-bad")


class TestResolverValidateName:
    """Resolver entry points must validate names at the top."""

    def test_resolve_container_rejects_shell_metacharacter(self):
        from kento import resolve_container
        with pytest.raises(ValidationError, match="invalid instance name"):
            resolve_container("bad;name")

    def test_resolve_in_namespace_rejects_path_traversal(self):
        from kento import resolve_in_namespace
        with pytest.raises(ValidationError, match="invalid instance name"):
            resolve_in_namespace("../etc", "lxc")

    def test_resolve_any_rejects_nul_byte(self):
        from kento import resolve_any
        with pytest.raises(ValidationError) as exc_info:
            resolve_any("\x00")
        msg = str(exc_info.value)
        # NUL triggers the empty-string branch first (since the initial
        # emptiness check fires on falsy input) or the explicit NUL branch.
        assert "NUL byte" in msg or "cannot be empty" in msg

    def test_resolve_any_rejects_embedded_nul(self):
        from kento import resolve_any
        with pytest.raises(ValidationError, match="NUL byte"):
            resolve_any("a\x00b")

    def test_check_name_conflict_rejects_slash(self):
        from kento import check_name_conflict
        with pytest.raises(ValidationError, match="invalid instance name"):
            check_name_conflict("a/b", "lxc")
