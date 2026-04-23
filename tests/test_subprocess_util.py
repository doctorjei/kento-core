"""Tests for kento.subprocess_util.run_or_die."""

import pytest

from kento.subprocess_util import run_or_die


def test_success_returns_completed_process():
    """run_or_die returns a CompletedProcess on exit 0."""
    result = run_or_die(["true"], "run true")
    assert result.returncode == 0


def test_nonzero_exits_with_code_1_and_prints_error(capsys):
    """Non-zero exit raises SystemExit(1) and prints 'Error: failed to <what>'."""
    with pytest.raises(SystemExit) as exc_info:
        run_or_die(["false"], "run false")
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Error: failed to run false" in captured.err
    assert "(exit 1)" in captured.err


def test_name_is_included_in_error(capsys):
    """When name is supplied, it appears after the 'what' label."""
    with pytest.raises(SystemExit) as exc_info:
        run_or_die(["false"], "start LXC container", name="web01")
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Error: failed to start LXC container web01" in captured.err


def test_hint_adds_hint_line(capsys):
    """When hint is supplied, a 'hint: ...' line follows the error."""
    with pytest.raises(SystemExit) as exc_info:
        run_or_die(["false"], "run false", hint="check your config")
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Error: failed to run false" in captured.err
    assert "hint: check your config" in captured.err


def test_missing_binary_exits_with_code_2(capsys):
    """Missing executable raises SystemExit(2) and mentions 'not found'."""
    with pytest.raises(SystemExit) as exc_info:
        run_or_die(["this-does-not-exist-xyz"], "run missing tool")
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "not found" in captured.err
    assert "this-does-not-exist-xyz" in captured.err


def test_missing_binary_includes_hint(capsys):
    """Missing executable path also emits hint if provided."""
    with pytest.raises(SystemExit) as exc_info:
        run_or_die(
            ["this-does-not-exist-xyz"],
            "run missing tool",
            hint="install the thing",
        )
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "hint: install the thing" in captured.err


def test_stderr_truncated_when_longer_than_500_chars(capsys):
    """Long stderr is truncated to 500 chars with a truncation marker."""
    # Generate > 500 chars of stderr via a shell one-liner.
    long_payload = "x" * 800
    with pytest.raises(SystemExit) as exc_info:
        run_or_die(
            ["sh", "-c", f"printf '%s' '{long_payload}' >&2; exit 1"],
            "emit long stderr",
        )
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    # Should contain the truncation marker, and not the entire 800-char string.
    assert "... (truncated)" in captured.err
    # 800 x's would be present without truncation; 500 + marker should be the cap.
    assert "x" * 800 not in captured.err
    # But there should still be a healthy chunk of x's.
    assert "x" * 500 in captured.err


def test_empty_stderr_message_still_clean(capsys):
    """If stderr is empty, no trailing ': ' garbage; message is still scannable."""
    with pytest.raises(SystemExit) as exc_info:
        run_or_die(["sh", "-c", "exit 3"], "silent failure")
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    # Exact shape: no ": " suffix after "(exit 3)".
    assert "Error: failed to silent failure (exit 3)" in captured.err
    # Make sure we didn't leave a dangling colon/space after the exit code.
    for line in captured.err.splitlines():
        if line.startswith("Error: failed to silent failure"):
            assert line.rstrip() == "Error: failed to silent failure (exit 3)"
