"""Tests for cross-process locking in create() — F7 + F11.

create() holds kento_lock() around:
  1. name / VMID / container-dir allocation + mkdir (the outer block)
  2. each allocate_port() call (narrower blocks)

These tests assert the lock is entered for each of those operations.
"""

from unittest.mock import MagicMock, patch

import pytest


@patch("kento.create.kento_lock")
@patch("kento.create.subprocess.run")
@patch("kento.create.resolve_layers", return_value="/a:/b")
@patch("kento.create.require_root")
def test_create_enters_kento_lock_for_name_and_mkdir(
    mock_root, mock_layers, mock_run, mock_lock, tmp_path
):
    """The outer allocate/mkdir block must enter kento_lock()."""
    # Configure kento_lock() as a context manager that records entry.
    from kento.create import create

    cm = MagicMock()
    mock_lock.return_value = cm

    with patch("kento.create.LXC_BASE", tmp_path), \
         patch("kento.create.upper_base", return_value=tmp_path / "lock-test"):
        create("myimage:latest", name="lock-test", mode="lxc",
               )

    # At least one with-kento_lock() block must have been entered. For an LXC
    # create without --port, only the outer block runs — so exactly one call.
    assert mock_lock.call_count >= 1
    assert cm.__enter__.call_count >= 1
    assert cm.__enter__.call_count == cm.__exit__.call_count


@patch("kento.create.kento_lock")
@patch("kento.create.subprocess.run")
@patch("kento.create.resolve_layers", return_value="/a:/b")
@patch("kento.create.require_root")
def test_create_enters_lock_twice_when_port_auto(
    mock_root, mock_layers, mock_run, mock_lock, tmp_path
):
    """Auto-port creates should enter kento_lock twice: outer + port."""
    from kento.create import create

    cm = MagicMock()
    mock_lock.return_value = cm

    with patch("kento.create.LXC_BASE", tmp_path), \
         patch("kento._bridge_exists", return_value=True), \
         patch("kento.create.upper_base",
               return_value=tmp_path / "lock-port-test"), \
         patch("kento.vm.allocate_port", return_value=10099):
        create("myimage:latest", name="lock-port-test", mode="lxc",
               bridge="lxcbr0", port="auto",
               net_type="bridge")

    # Outer allocate/mkdir block + port allocation block → at least 2 enters.
    assert cm.__enter__.call_count >= 2
    assert cm.__enter__.call_count == cm.__exit__.call_count


@patch("kento.create.kento_lock")
@patch("kento.create.resolve_layers", return_value="/a:/b")
@patch("kento.create.require_root")
def test_vm_auto_port_enters_lock_for_port_allocation(
    mock_root, mock_layers, mock_lock, tmp_path
):
    """VM usermode (default) uses allocate_port → must be locked."""
    from kento.create import create

    cm = MagicMock()
    mock_lock.return_value = cm

    with patch("kento.create.LXC_BASE", tmp_path), \
         patch("kento.create.VM_BASE", tmp_path), \
         patch("kento.create.upper_base",
               return_value=tmp_path / "vm-lock-test"), \
         patch("kento.vm.allocate_port", return_value=10055):
        create("myimage:latest", name="vm-lock-test", mode="vm")

    # Outer block + port allocation block.
    assert cm.__enter__.call_count >= 2
    assert cm.__enter__.call_count == cm.__exit__.call_count


@patch("kento.create.subprocess.run")
@patch("kento.create.resolve_layers", return_value="/a:/b")
@patch("kento.create.require_root")
def test_concurrent_create_does_not_share_auto_name(
    mock_root, mock_layers, mock_run, tmp_path
):
    """Two sequential create() calls with auto-name must produce distinct
    instance directories (regression: was already true, just confirming the
    lock didn't break it)."""
    from kento.create import create

    def fake_upper(cid, base=None):
        return tmp_path / cid

    with patch("kento.create.LXC_BASE", tmp_path), \
         patch("kento.create.upper_base", side_effect=fake_upper):
        create("myimage:latest", mode="lxc")
        create("myimage:latest", mode="lxc")

    # next_instance_name picks myimage_latest-0 then -1.
    assert (tmp_path / "myimage_latest-0").is_dir()
    assert (tmp_path / "myimage_latest-1").is_dir()


def test_create_port_written_atomically_with_allocation(tmp_path):
    """When port=='auto', the kento-port file must be written while still
    holding the lock — otherwise two concurrent creates could both call
    allocate_port(), see the same free port, release, then write.

    We verify this structurally by asserting the write happens before the
    lock exits: patch allocate_port to record the sequence of lock state
    and file-write events.
    """
    from kento import locking
    from kento.create import create

    events: list[str] = []

    # Wrap kento_lock so we record enter/exit.
    original_kento_lock = locking.kento_lock

    class _RecordingLock:
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            events.append("lock-enter")
            return self.inner.__enter__()

        def __exit__(self, *a):
            events.append("lock-exit")
            return self.inner.__exit__(*a)

    def fake_lock():
        return _RecordingLock(original_kento_lock())

    def fake_allocate_port():
        events.append("allocate_port")
        return 10066

    real_write_text = type(tmp_path).write_text

    def recording_write_text(self, data, *args, **kwargs):
        if self.name == "kento-port":
            events.append(f"write-port={data.strip()}")
        return real_write_text(self, data, *args, **kwargs)

    with patch("kento.create.kento_lock", side_effect=fake_lock), \
         patch("kento.create.LXC_BASE", tmp_path), \
         patch("kento._bridge_exists", return_value=True), \
         patch("kento.create.upper_base",
               return_value=tmp_path / "atomic-test"), \
         patch("kento.create.subprocess.run"), \
         patch("kento.create.resolve_layers", return_value="/a:/b"), \
         patch("kento.create.require_root"), \
         patch("kento.vm.allocate_port", side_effect=fake_allocate_port), \
         patch.object(type(tmp_path), "write_text", recording_write_text):
        create("myimage:latest", name="atomic-test", mode="lxc",
               bridge="lxcbr0", port="auto",
               net_type="bridge")

    # Find the port-alloc block: look for a sequence where allocate_port
    # and write-port=... both occur between the same lock-enter/lock-exit.
    # Scan events and find the pair.
    port_block_found = False
    for i, ev in enumerate(events):
        if ev == "allocate_port":
            # Find the enclosing lock-enter/lock-exit.
            lock_enter_idx = max(
                j for j, e in enumerate(events[:i]) if e == "lock-enter"
            )
            lock_exit_idx = next(
                (j for j, e in enumerate(events[i:], start=i)
                 if e == "lock-exit"),
                None,
            )
            assert lock_exit_idx is not None
            # Between allocate_port and lock-exit there should be a
            # write-port entry.
            between = events[i:lock_exit_idx]
            assert any(e.startswith("write-port=") for e in between), (
                f"kento-port was not written inside the same lock block as "
                f"allocate_port. Events: {events}"
            )
            port_block_found = True
            break

    assert port_block_found, f"allocate_port was never called: {events}"
