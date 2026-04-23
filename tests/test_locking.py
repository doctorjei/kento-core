"""Tests for kento.locking — cross-process exclusive lock."""

import multiprocessing
import time
from pathlib import Path

import pytest

from kento import locking
from kento.locking import _open_lock_fd, kento_lock


def _point_locks_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect both lock paths into tmp_path."""
    monkeypatch.setattr(locking, "_PRIMARY_LOCK", tmp_path / "kento.lock")
    monkeypatch.setattr(locking, "_FALLBACK_LOCK", tmp_path / "fallback.lock")


def test_basic_acquire_release(tmp_path, monkeypatch):
    """Plain `with kento_lock(): pass` succeeds quickly."""
    _point_locks_at(tmp_path, monkeypatch)
    start = time.monotonic()
    with kento_lock():
        pass
    elapsed = time.monotonic() - start
    # Acquisition of an uncontended lock should be near-instantaneous.
    assert elapsed < 1.0
    # Primary lock file should now exist.
    assert (tmp_path / "kento.lock").exists()


def _child_hold_lock(lock_path: str, timeline_path: str, hold_seconds: float) -> None:
    """Child process: acquire the lock, record timestamps, sleep, release."""
    # Re-point the module globals inside the child (monkeypatch doesn't cross
    # the fork/spawn boundary cleanly, so set them explicitly).
    from kento import locking as child_locking

    child_locking._PRIMARY_LOCK = Path(lock_path)
    child_locking._FALLBACK_LOCK = Path(lock_path + ".fallback")

    with child_locking.kento_lock():
        with open(timeline_path, "a") as f:
            f.write(f"child_acquired {time.monotonic()}\n")
        time.sleep(hold_seconds)
        with open(timeline_path, "a") as f:
            f.write(f"child_releasing {time.monotonic()}\n")


def test_mutual_exclusion(tmp_path, monkeypatch):
    """Parent blocks until child releases the lock."""
    _point_locks_at(tmp_path, monkeypatch)
    lock_path = str(tmp_path / "kento.lock")
    timeline_path = str(tmp_path / "timeline.txt")

    hold_seconds = 0.2
    # Use 'fork' so the child shares parent state; falls back to default if
    # not available. We set module globals inside the child anyway.
    ctx = multiprocessing.get_context("fork")
    proc = ctx.Process(
        target=_child_hold_lock,
        args=(lock_path, timeline_path, hold_seconds),
    )
    proc.start()

    # Give the child a moment to acquire the lock before we try.
    time.sleep(0.05)

    parent_attempt = time.monotonic()
    with kento_lock():
        parent_acquired = time.monotonic()
        with open(timeline_path, "a") as f:
            f.write(f"parent_acquired {parent_acquired}\n")

    proc.join(timeout=5.0)
    assert proc.exitcode == 0, "child process failed"

    # Parent waited at least ~0.1s (child held for 0.2s, parent started
    # ~0.05s after spawn).
    waited = parent_acquired - parent_attempt
    assert waited >= 0.1, f"parent did not wait for child (waited {waited:.3f}s)"

    # Verify ordering in the timeline file.
    lines = Path(timeline_path).read_text().splitlines()
    events = {}
    for line in lines:
        name, ts = line.rsplit(" ", 1)
        events[name] = float(ts)

    assert "child_acquired" in events
    assert "child_releasing" in events
    assert "parent_acquired" in events
    assert events["child_releasing"] <= events["parent_acquired"], (
        f"parent acquired before child released: {events}"
    )


def test_reentrancy_sequential(tmp_path, monkeypatch):
    """Same process can acquire the lock twice in a row."""
    _point_locks_at(tmp_path, monkeypatch)
    with kento_lock():
        pass
    with kento_lock():
        pass
    # No hang, no error — test passes if we reach here.


def test_exception_inside_with_block_releases_lock(tmp_path, monkeypatch):
    """Exception inside the `with` block still releases the lock."""
    _point_locks_at(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match="boom"):
        with kento_lock():
            raise RuntimeError("boom")

    # Must be able to re-acquire in the same process without hanging.
    start = time.monotonic()
    with kento_lock():
        pass
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, "lock was not released after exception"


def test_fallback_path_used_when_primary_unavailable(tmp_path, monkeypatch):
    """_open_lock_fd falls back when primary parent cannot be created."""
    # Primary points at a path whose parent cannot be created.
    monkeypatch.setattr(
        locking, "_PRIMARY_LOCK", Path("/proc/nonexistent/kento.lock")
    )
    fallback = tmp_path / "fallback.lock"
    monkeypatch.setattr(locking, "_FALLBACK_LOCK", fallback)

    fd = _open_lock_fd()
    try:
        assert fallback.exists(), "fallback lock file should have been created"
    finally:
        import os
        os.close(fd)


def test_all_paths_fail_exits(tmp_path, monkeypatch, capsys):
    """SystemExit(1) when both primary and fallback cannot be created."""
    monkeypatch.setattr(
        locking, "_PRIMARY_LOCK", Path("/proc/nonexistent/kento.lock")
    )
    monkeypatch.setattr(
        locking, "_FALLBACK_LOCK", Path("/proc/also-nonexistent/fallback.lock")
    )

    with pytest.raises(SystemExit) as exc_info:
        _open_lock_fd()
    assert exc_info.value.code == 1

    captured = capsys.readouterr()
    assert "could not open a kento lock file" in captured.err
