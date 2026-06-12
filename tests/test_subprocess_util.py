"""Tests for kento.subprocess_util.run_or_die."""

from unittest.mock import patch

import pytest

from kento.errors import SubprocessError
from kento.subprocess_util import run_or_die


def test_success_returns_completed_process():
    """run_or_die returns a CompletedProcess on exit 0."""
    result = run_or_die(["true"], "run true")
    assert result.returncode == 0


def test_nonzero_raises_subprocess_error():
    """Non-zero exit raises SubprocessError with 'failed to <what>' message."""
    with pytest.raises(SubprocessError, match=r"failed to run false") as exc_info:
        run_or_die(["false"], "run false")
    assert "(exit 1)" in str(exc_info.value)
    assert exc_info.value.returncode == 1
    assert exc_info.value.cmd == ["false"]


def test_name_is_included_in_error():
    """When name is supplied, it appears after the 'what' label."""
    with pytest.raises(SubprocessError, match=r"failed to start LXC container web01") as exc_info:
        run_or_die(["false"], "start LXC container", name="web01")
    assert exc_info.value.returncode == 1
    assert exc_info.value.cmd == ["false"]


def test_hint_adds_hint_line(caplog):
    """When hint is supplied, the error carries the core text AND the hint is
    emitted via logger.info on the "kento" logger."""
    import logging
    with caplog.at_level(logging.INFO, logger="kento"):
        with pytest.raises(SubprocessError, match=r"failed to run false") as exc_info:
            run_or_die(["false"], "run false", hint="check your config")
    assert exc_info.value.returncode == 1
    assert any("check your config" in r.message for r in caplog.records)


def test_missing_binary_raises_subprocess_error():
    """Missing executable raises SubprocessError and mentions 'not found'."""
    with pytest.raises(SubprocessError, match=r"not found") as exc_info:
        run_or_die(["this-does-not-exist-xyz"], "run missing tool")
    assert "this-does-not-exist-xyz" in str(exc_info.value)
    assert exc_info.value.returncode is None
    assert exc_info.value.cmd == ["this-does-not-exist-xyz"]


def test_missing_binary_includes_cmd():
    """Missing executable path carries cmd on SubprocessError."""
    with pytest.raises(SubprocessError) as exc_info:
        run_or_die(
            ["this-does-not-exist-xyz"],
            "run missing tool",
            hint="install the thing",
        )
    assert exc_info.value.cmd == ["this-does-not-exist-xyz"]
    assert exc_info.value.returncode is None


def test_permission_error_raises_subprocess_error():
    """A non-executable binary (PermissionError) yields a branded SubprocessError.

    PermissionError is an OSError subclass distinct from FileNotFoundError;
    without the broadened except it would propagate as a traceback.
    """
    with patch("kento.subprocess_util.subprocess.run",
               side_effect=PermissionError(13, "Permission denied")):
        with pytest.raises(SubprocessError, match=r"cannot execute '/some/tool'") as exc_info:
            run_or_die(["/some/tool"], "run tool")
    assert "Permission denied" in str(exc_info.value)
    assert "check permissions/arch" in str(exc_info.value)
    assert exc_info.value.returncode is None
    assert exc_info.value.cmd == ["/some/tool"]


def test_oserror_exec_format_raises_subprocess_error():
    """A wrong-arch / ENOEXEC binary (OSError) yields a branded SubprocessError."""
    with patch("kento.subprocess_util.subprocess.run",
               side_effect=OSError(8, "Exec format error")):
        with pytest.raises(SubprocessError, match=r"cannot execute '/some/tool'") as exc_info:
            run_or_die(["/some/tool"], "run tool", hint="rebuild for this arch")
    assert "Exec format error" in str(exc_info.value)
    assert exc_info.value.returncode is None
    assert exc_info.value.cmd == ["/some/tool"]


def test_stderr_truncated_when_longer_than_500_chars():
    """Long stderr is truncated to 500 chars with a truncation marker."""
    long_payload = "x" * 800
    with pytest.raises(SubprocessError) as exc_info:
        run_or_die(
            ["sh", "-c", f"printf '%s' '{long_payload}' >&2; exit 1"],
            "emit long stderr",
        )
    msg = str(exc_info.value)
    # Should contain the truncation marker, and not the entire 800-char string.
    assert "... (truncated)" in msg
    # 800 x's would be present without truncation; 500 + marker should be the cap.
    assert "x" * 800 not in msg
    # But there should still be a healthy chunk of x's.
    assert "x" * 500 in msg
    assert exc_info.value.returncode == 1


def test_empty_stderr_message_still_clean():
    """If stderr is empty, no trailing ': ' garbage; message is still scannable."""
    with pytest.raises(SubprocessError) as exc_info:
        run_or_die(["sh", "-c", "exit 3"], "silent failure")
    msg = str(exc_info.value)
    # Exact shape: no ": " suffix after "(exit 3)".
    assert "failed to silent failure (exit 3)" in msg
    # Make sure we didn't leave a dangling colon/space after the exit code.
    for part in msg.splitlines():
        if "failed to silent failure" in part:
            assert part.rstrip() == "failed to silent failure (exit 3)"
    assert exc_info.value.returncode == 3
