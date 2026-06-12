"""Tests for the attach/enter command."""

import subprocess
from unittest.mock import patch

import pytest

from kento.attach import ESCAPE_BYTE, EscapeDetector, _write_all, attach


def _ok(*args, **kwargs):
    return subprocess.CompletedProcess(args[0] if args else [], 0)


# -- Per-mode dispatch (mocked subprocess) --


@patch("kento.attach.subprocess.run", side_effect=_ok)
@patch("kento.attach.require_root")
def test_attach_lxc_calls_lxc_attach(mock_root, mock_run, tmp_path):
    d = tmp_path / "mybox"
    d.mkdir()

    with patch("kento.attach.resolve_any", return_value=(d, "lxc")):
        rc = attach("mybox")

    assert rc == 0
    mock_run.assert_called_once()
    assert list(mock_run.call_args[0][0]) == ["lxc-attach", "-n", "mybox"]


@patch("kento.attach.subprocess.run", side_effect=_ok)
@patch("kento.attach.require_root")
def test_attach_pve_lxc_calls_pct_enter(mock_root, mock_run, tmp_path):
    d = tmp_path / "100"
    d.mkdir()

    with patch("kento.attach.resolve_any", return_value=(d, "pve")):
        rc = attach("mybox")

    assert rc == 0
    mock_run.assert_called_once()
    assert list(mock_run.call_args[0][0]) == ["pct", "enter", "100"]


@patch("kento.attach.subprocess.run", side_effect=_ok)
@patch("kento.attach.require_root")
def test_attach_pve_vm_calls_qm_terminal(mock_root, mock_run, tmp_path):
    d = tmp_path / "vmdir"
    d.mkdir()
    (d / "kento-vmid").write_text("200\n")

    with patch("kento.attach.resolve_any", return_value=(d, "pve-vm")):
        rc = attach("myvm")

    assert rc == 0
    mock_run.assert_called_once()
    assert list(mock_run.call_args[0][0]) == ["qm", "terminal", "200"]


@patch("kento.attach.subprocess.run", side_effect=_ok)
@patch("kento.attach.require_root")
def test_attach_propagates_returncode(mock_root, mock_run, tmp_path):
    d = tmp_path / "mybox"
    d.mkdir()
    mock_run.side_effect = lambda *a, **k: subprocess.CompletedProcess(a[0], 7)

    with patch("kento.attach.resolve_any", return_value=(d, "lxc")):
        rc = attach("mybox")

    assert rc == 7


# -- VM serial relay: socket-absent error path (no subprocess, no connection) --


@patch("kento.attach.socket.socket")
@patch("kento.attach.require_root")
def test_attach_vm_missing_socket_errors(mock_root, mock_socket, tmp_path, capsys):
    d = tmp_path / "testvm"
    d.mkdir()  # no serial.sock created

    with patch("kento.attach.resolve_any", return_value=(d, "vm")):
        rc = attach("testvm")

    assert rc != 0
    mock_socket.assert_not_called()
    captured = capsys.readouterr()
    assert "serial socket not found" in captured.err
    assert "testvm" in captured.err


@patch("kento.attach.os.isatty", return_value=False)
@patch("kento.attach.socket.socket")
@patch("kento.attach.require_root")
def test_attach_vm_non_tty_errors(mock_root, mock_socket, mock_isatty,
                                  tmp_path, capsys):
    d = tmp_path / "testvm"
    d.mkdir()
    (d / "serial.sock").write_bytes(b"")  # present, but stdin is not a tty

    with patch("kento.attach.resolve_any", return_value=(d, "vm")):
        rc = attach("testvm")

    assert rc != 0
    mock_socket.assert_not_called()
    captured = capsys.readouterr()
    assert "interactive terminal" in captured.err


# -- EscapeDetector state machine (pure, no tty/socket) --


class TestEscapeDetector:
    def test_normal_byte_forwards(self):
        det = EscapeDetector()
        assert det.feed(ord("a")) == ("forward", b"a")
        assert not det.armed

    def test_ctrl_rbracket_swallows_and_arms(self):
        det = EscapeDetector()
        assert det.feed(ESCAPE_BYTE) == ("swallow", None)
        assert det.armed

    def test_ctrl_rbracket_then_Q_detaches(self):
        det = EscapeDetector()
        det.feed(ESCAPE_BYTE)
        assert det.feed(ord("Q")) == ("detach", None)
        assert not det.armed

    def test_ctrl_rbracket_then_lowercase_q_detaches(self):
        det = EscapeDetector()
        det.feed(ESCAPE_BYTE)
        assert det.feed(ord("q")) == ("detach", None)

    def test_ctrl_rbracket_then_other_forwards_both(self):
        det = EscapeDetector()
        det.feed(ESCAPE_BYTE)
        action, payload = det.feed(ord("x"))
        assert action == "forward"
        assert payload == bytes([ESCAPE_BYTE, ord("x")])
        assert not det.armed

    def test_doubled_ctrl_rbracket_forwards_one_literal(self):
        det = EscapeDetector()
        det.feed(ESCAPE_BYTE)
        action, payload = det.feed(ESCAPE_BYTE)
        assert action == "forward"
        assert payload == bytes([ESCAPE_BYTE])
        # Disarmed after a doubled Ctrl-], not re-armed.
        assert not det.armed

    def test_sequence_after_resolved_escape_resets(self):
        det = EscapeDetector()
        det.feed(ESCAPE_BYTE)
        det.feed(ord("x"))  # resolves escape, forwards both
        # Now a normal byte forwards plainly again.
        assert det.feed(ord("b")) == ("forward", b"b")


# -- _write_all: short-write looping (serial relay -> stdout pipe/file) --


class TestWriteAll:
    def test_loops_past_short_writes(self):
        """os.write may return a short count when fd is a pipe/file; _write_all
        must loop until every byte is flushed, in order, with no data dropped."""
        data = b"abcdefghij" * 1000  # 10_000 bytes
        written = bytearray()

        def short_write(fd, buf):
            # Accept at most 3 bytes per call to force many short writes.
            chunk = bytes(buf[:3])
            written.extend(chunk)
            return len(chunk)

        with patch("kento.attach.os.write", side_effect=short_write):
            _write_all(7, data)

        # Every byte eventually written, in original order.
        assert bytes(written) == data

    def test_single_full_write(self):
        """When os.write accepts everything at once, _write_all writes once."""
        data = b"hello world"
        calls = []

        def full_write(fd, buf):
            calls.append(bytes(buf))
            return len(buf)

        with patch("kento.attach.os.write", side_effect=full_write):
            _write_all(7, data)

        assert calls == [data]

    def test_empty_data_no_write(self):
        """Empty payload performs no os.write calls."""
        with patch("kento.attach.os.write") as mock_write:
            _write_all(7, b"")
        mock_write.assert_not_called()
