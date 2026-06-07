"""Tests for `kento suspend` / `kento resume` (VM-modes-only vCPU pause)."""

import json
import socket
import threading
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from kento.suspend import qmp_command, resume, suspend


# ---------------------------------------------------------------------------
# qmp_command: real AF_UNIX server in a thread, speaking the QMP protocol.
# ---------------------------------------------------------------------------

class _FakeQMPServer:
    """A minimal QMP server over a unix socket for one client connection.

    ``script`` is a list of byte lines the server sends *after* the client's
    qmp_capabilities (the greeting + capabilities reply are sent automatically).
    Each entry corresponds to one command reply (events may be interleaved by
    putting multiple lines in one entry — they are sent before the reply line).
    Records every JSON object the client sends in ``received``.
    """

    def __init__(self, sock_path, replies):
        self._path = str(sock_path)
        self._replies = replies
        self.received = []
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(self._path)
        self._srv.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self._thread.start()

    def _recv_line(self, conn, buf):
        while b"\n" not in buf[0]:
            chunk = conn.recv(65536)
            if not chunk:
                return None
            buf[0] += chunk
        line, _, buf[0] = buf[0].partition(b"\n")
        return line

    def _serve(self):
        conn, _ = self._srv.accept()
        buf = [b""]
        try:
            # Greeting first.
            conn.sendall(json.dumps({"QMP": {"version": {}}}).encode() + b"\n")
            # Read qmp_capabilities, ack it.
            line = self._recv_line(conn, buf)
            self.received.append(json.loads(line.decode()))
            conn.sendall(json.dumps({"return": {}}).encode() + b"\n")
            # Then service each scripted reply.
            for reply in self._replies:
                line = self._recv_line(conn, buf)
                if line is None:
                    break
                self.received.append(json.loads(line.decode()))
                conn.sendall(reply)
        finally:
            conn.close()
            self._srv.close()


def test_qmp_command_negotiates_and_returns(tmp_path):
    sock_path = tmp_path / "qmp.sock"
    reply = json.dumps({"return": {}}).encode() + b"\n"
    srv = _FakeQMPServer(sock_path, [reply])
    srv.start()

    (resp,) = qmp_command(sock_path, {"execute": "stop"})

    assert resp == {"return": {}}
    # First message the server saw is the capabilities handshake, then stop.
    assert srv.received[0] == {"execute": "qmp_capabilities"}
    assert srv.received[1] == {"execute": "stop"}


def test_qmp_command_skips_interleaved_events(tmp_path):
    sock_path = tmp_path / "qmp.sock"
    # An async event arrives BEFORE the command's return — must be skipped.
    event = json.dumps({"event": "STOP", "timestamp": {}}).encode() + b"\n"
    ret = json.dumps({"return": {}}).encode() + b"\n"
    srv = _FakeQMPServer(sock_path, [event + ret])
    srv.start()

    (resp,) = qmp_command(sock_path, {"execute": "stop"})

    assert resp == {"return": {}}


def test_qmp_command_multiple_commands(tmp_path):
    sock_path = tmp_path / "qmp.sock"
    r1 = json.dumps({"return": {"status": "running"}}).encode() + b"\n"
    r2 = json.dumps({"return": {}}).encode() + b"\n"
    srv = _FakeQMPServer(sock_path, [r1, r2])
    srv.start()

    a, b = qmp_command(
        sock_path, {"execute": "query-status"}, {"execute": "cont"})

    assert a == {"return": {"status": "running"}}
    assert b == {"return": {}}
    assert srv.received[1] == {"execute": "query-status"}
    assert srv.received[2] == {"execute": "cont"}


def test_qmp_command_socket_absent_raises(tmp_path):
    with pytest.raises(OSError):
        qmp_command(tmp_path / "nope.sock", {"execute": "stop"})


# ---------------------------------------------------------------------------
# suspend / resume dispatch: mocked environment.
# ---------------------------------------------------------------------------

@contextmanager
def _env(container_dir, mode, *, running=True):
    with patch("kento.suspend.require_root"), \
         patch("kento.suspend.resolve_any", return_value=(container_dir, mode)), \
         patch("kento.suspend.is_running", return_value=running):
        yield


# -- plain vm --------------------------------------------------------------

def test_suspend_vm_sends_stop(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    (d / "qmp.sock").write_text("")  # presence check only
    with _env(d, "vm"), \
         patch("kento.suspend.qmp_command",
               return_value=[{"return": {}}]) as mock_qmp:
        rc = suspend("box")
    assert rc == 0
    args, kwargs = mock_qmp.call_args
    assert args[0] == d / "qmp.sock"
    assert args[1] == {"execute": "stop"}


def test_resume_vm_sends_cont(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    (d / "qmp.sock").write_text("")
    with _env(d, "vm"), \
         patch("kento.suspend.qmp_command",
               return_value=[{"return": {}}]) as mock_qmp:
        rc = resume("box")
    assert rc == 0
    args, _ = mock_qmp.call_args
    assert args[1] == {"execute": "cont"}


def test_suspend_vm_missing_socket_errors(tmp_path, capsys):
    d = tmp_path / "box"
    d.mkdir()
    with _env(d, "vm"), \
         patch("kento.suspend.qmp_command") as mock_qmp:
        rc = suspend("box")
    assert rc == 1
    mock_qmp.assert_not_called()
    assert "QMP socket not found" in capsys.readouterr().err


def test_suspend_vm_qmp_error_surfaced(tmp_path, capsys):
    d = tmp_path / "box"
    d.mkdir()
    (d / "qmp.sock").write_text("")
    err = {"error": {"class": "GenericError", "desc": "already paused"}}
    with _env(d, "vm"), \
         patch("kento.suspend.qmp_command", return_value=[err]):
        rc = suspend("box")
    assert rc == 1
    assert "already paused" in capsys.readouterr().err


def test_suspend_vm_not_running_errors(tmp_path, capsys):
    d = tmp_path / "box"
    d.mkdir()
    (d / "qmp.sock").write_text("")
    with _env(d, "vm", running=False), \
         patch("kento.suspend.qmp_command") as mock_qmp:
        rc = suspend("box")
    assert rc == 1
    mock_qmp.assert_not_called()
    assert "not running" in capsys.readouterr().err


# -- pve-vm ----------------------------------------------------------------

def _ok():
    class R:
        returncode = 0
        stdout = ""
        stderr = ""
    return R()


def _fail(stderr="boom"):
    class R:
        returncode = 1
        stdout = ""
    R.stderr = stderr
    return R()


def test_suspend_pve_vm_runs_qm_suspend(tmp_path):
    d = tmp_path / "100"
    d.mkdir()
    (d / "kento-vmid").write_text("100\n")
    with _env(d, "pve-vm"), \
         patch("kento.suspend.subprocess.run", return_value=_ok()) as mock_run:
        rc = suspend("box")
    assert rc == 0
    assert mock_run.call_args[0][0] == ["qm", "suspend", "100"]


def test_resume_pve_vm_runs_qm_resume(tmp_path):
    d = tmp_path / "100"
    d.mkdir()
    (d / "kento-vmid").write_text("100\n")
    with _env(d, "pve-vm"), \
         patch("kento.suspend.subprocess.run", return_value=_ok()) as mock_run:
        rc = resume("box")
    assert rc == 0
    assert mock_run.call_args[0][0] == ["qm", "resume", "100"]


def test_suspend_pve_vm_failure_surfaces(tmp_path, capsys):
    d = tmp_path / "100"
    d.mkdir()
    (d / "kento-vmid").write_text("100\n")
    with _env(d, "pve-vm"), \
         patch("kento.suspend.subprocess.run", return_value=_fail("nope")):
        rc = suspend("box")
    assert rc == 1
    assert "nope" in capsys.readouterr().err


# -- lxc / pve-lxc unsupported ---------------------------------------------

@pytest.mark.parametrize("mode", ["lxc", "pve"])
def test_suspend_lxc_unsupported(tmp_path, capsys, mode):
    d = tmp_path / "box"
    d.mkdir()
    with _env(d, mode), \
         patch("kento.suspend.qmp_command") as mock_qmp, \
         patch("kento.suspend.subprocess.run") as mock_run:
        rc = suspend("box")
    assert rc == 1
    mock_qmp.assert_not_called()
    mock_run.assert_not_called()
    assert "not supported for LXC" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["lxc", "pve"])
def test_resume_lxc_unsupported(tmp_path, capsys, mode):
    d = tmp_path / "box"
    d.mkdir()
    with _env(d, mode), \
         patch("kento.suspend.qmp_command") as mock_qmp, \
         patch("kento.suspend.subprocess.run") as mock_run:
        rc = resume("box")
    assert rc == 1
    mock_qmp.assert_not_called()
    mock_run.assert_not_called()
