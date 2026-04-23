"""Tier 1 coverage for the ``post-stop`` cleanup branch.

When a container stops, LXC invokes the hook with ``post-stop`` so kento
can tear down the port-forwarding rules it installed at ``start-host``
time. The hook's responsibilities in that phase are:

1. If ``kento-portfwd-active`` exists, walk each kento nftables chain,
   delete the rules tagged with the container's ``kento:<NAME>`` comment,
   then remove the ``kento-portfwd-active`` state file.
2. Unmount the overlayfs at ``$CONTAINER_DIR/rootfs`` if it's still a
   mountpoint.

This module focuses on (1): a populated ``kento-portfwd-active`` file is
gone after a post-stop invocation, and the hook exits 0. ``nft`` is
stubbed on PATH with a log-and-exit-0 wrapper so the test doesn't depend
on the host's nftables state; the overlayfs umount in (2) is a no-op
here because the fixture's ``rootfs`` dir is not a mountpoint.

See ``src/kento/hook.sh`` for the post-stop branch. Invocation shape
matches v1 plain-LXC: env vars only, no positional args, with
``LXC_HOOK_TYPE=post-stop``.
"""

from __future__ import annotations

import os
import stat
import subprocess


def _write_nft_stub(bin_dir) -> None:
    """Drop a log-and-exit-0 ``nft`` wrapper into ``bin_dir``.

    The post-stop branch shells out to ``nft`` to list and delete rules;
    we don't want to actually touch the host nftables tree, so intercept
    it with a no-op script that also writes its argv to a log file for
    potential inspection by the test.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    nft = bin_dir / "nft"
    nft.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{bin_dir}/nft.log"\n'
        "exit 0\n"
    )
    nft.chmod(nft.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_post_stop_removes_portfwd_active_file(hook_fixture, tmp_path):
    """``kento-portfwd-active`` must be gone after a post-stop invocation.

    Seeds the fixture with a realistic ``kento-portfwd-active`` (the
    start-host branch writes ``HOST:GUEST:IP``; we mirror that shape).
    Stubs ``nft`` so the rule-delete loop in hook.sh is exercised without
    touching real nftables state. Asserts the file is removed and the
    hook exits cleanly.
    """
    # Seed a portfwd-active marker with the exact format start-host writes:
    # "<HOST_PORT>:<GUEST_PORT>:<IP>\n" (see setup_port_forwarding in
    # src/kento/hook.sh, the sync-path `echo ... > kento-portfwd-active`).
    portfwd_active = hook_fixture.container_dir / "kento-portfwd-active"
    portfwd_active.write_text("10270:22:10.0.3.42\n")
    assert portfwd_active.exists()

    # Stub nft on PATH so the rule-delete loop is a no-op the test can
    # observe via the log file if needed.
    bin_dir = tmp_path / "bin"
    _write_nft_stub(bin_dir)

    env = dict(hook_fixture.env)
    env["LXC_HOOK_TYPE"] = "post-stop"
    # Put our stub ahead of the system nft.
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"

    result = subprocess.run(
        ["sh", str(hook_fixture.hook_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, (
        f"post-stop hook exited {result.returncode}.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert not portfwd_active.exists(), (
        f"post-stop must remove {portfwd_active}; still present after the "
        f"hook ran.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_post_stop_without_portfwd_active_is_noop(hook_fixture, tmp_path):
    """No ``kento-portfwd-active`` on disk → post-stop still exits 0.

    Covers the common case where a container was created/started but had
    no port forwarding configured. The hook's cleanup branch is guarded
    by ``[ -f "$CONTAINER_DIR/kento-portfwd-active" ]`` so the nft dance
    is skipped entirely; only the overlayfs umount runs (a no-op here).
    """
    # No kento-portfwd-active to start with.
    assert not (hook_fixture.container_dir / "kento-portfwd-active").exists()

    # Still stub nft so an unexpected invocation would be visible via
    # the log file (it shouldn't be called at all in this path).
    bin_dir = tmp_path / "bin"
    _write_nft_stub(bin_dir)

    env = dict(hook_fixture.env)
    env["LXC_HOOK_TYPE"] = "post-stop"
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"

    result = subprocess.run(
        ["sh", str(hook_fixture.hook_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, (
        f"post-stop hook exited {result.returncode} with no portfwd state.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # nft must not have been invoked — the branch is guarded by the
    # file-existence check. If the stub's log shows entries, the guard
    # is broken.
    nft_log = bin_dir / "nft.log"
    assert not nft_log.exists(), (
        f"nft was called despite no kento-portfwd-active on disk; the "
        f"post-stop guard is broken. Log contents:\n{nft_log.read_text()}"
    )
