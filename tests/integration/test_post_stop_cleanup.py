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
from pathlib import Path


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


def test_post_stop_without_portfwd_active_attempts_teardown(
    hook_fixture, tmp_path
):
    """No ``kento-portfwd-active`` on disk: post-stop STILL runs teardown.

    Regression for the detached-worker orphan-rule race: a DHCP worker can
    install rules (and be about to write the marker) after the container
    stops. Post-stop therefore must NOT gate teardown on the
    ``kento-portfwd-active`` marker -- it attempts removal of this instance's
    tagged rules unconditionally. The teardown is idempotent and quiet via
    the anchored ``kento:${NAME}`` match, so a no-op pass is harmless.

    Asserts the hook exits 0 AND that nft was invoked (the teardown list
    pass ran) even with no marker present.
    """
    # No kento-portfwd-active to start with.
    assert not (hook_fixture.container_dir / "kento-portfwd-active").exists()

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
    # nft MUST have been invoked: teardown is now unconditional so a late
    # worker's orphan rules get cleaned even without the marker. The stub's
    # log records each `nft -a list chain ...` pass (one per chain).
    nft_log = bin_dir / "nft.log"
    assert nft_log.exists(), (
        "nft was not called despite the unconditional-teardown contract; "
        "post-stop must attempt rule removal even with no marker present."
    )
    calls = [ln for ln in nft_log.read_text().splitlines() if ln]
    assert len(calls) >= 3, (
        f"expected >=3 nft list passes (one per chain), got {len(calls)}:\n"
        + "\n".join(calls)
    )


# --- #1: prefix-collision teardown safety -----------------------------------


def _write_nft_collision_stub(bin_dir, victim_name: str) -> None:
    """Drop an ``nft`` stub that models the SHARED ``ip kento`` table holding
    rules for two instances whose names collide on a prefix (``web`` and
    ``web2``).

    Behaviour:
      * ``nft -a list chain ip kento <chain>`` prints a comment-tagged rule
        for BOTH ``web`` (handle 5) and ``web2`` (handle 9) in each chain,
        exactly as real ``nft -a`` renders them: ``comment "kento:<name>"``
        followed by `` # handle <N>``. The post-stop teardown greps this for
        the stopping instance and feeds ``$NF`` (the handle number) to
        ``nft delete``.
      * ``nft delete rule ip kento <chain> handle <N>`` is logged so the test
        can assert WHICH handles were deleted.

    ``victim_name`` is the OTHER instance's name whose handle (9) must never
    be deleted when the colliding instance (``web``) is torn down.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    nft = bin_dir / "nft"
    # Rendered like real `nft -a list chain` output (leading tab, trailing
    # `# handle N`). Both the colliding name and the victim appear in every
    # chain so an unanchored substring match would scoop up the victim too.
    nft.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{bin_dir}/nft.log"\n'
        'case "$1" in\n'
        "  -a)\n"
        "    # `nft -a list chain ip kento <chain>` — emit both instances.\n"
        '    printf "\\ttcp dport 10270 dnat to 10.0.3.1:22 '
        'comment \\"kento:web\\" # handle 5\\n"\n'
        '    printf "\\ttcp dport 10280 dnat to 10.0.3.2:22 '
        f'comment \\"kento:{victim_name}\\" # handle 9\\n"\n'
        "    ;;\n"
        "esac\n"
        "exit 0\n"
    )
    nft.chmod(nft.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_post_stop_prefix_collision_spares_other_instance(
    hook_fixture_factory, tmp_path
):
    """Stopping ``web`` must NOT delete ``web2``'s live port-forward rules.

    Regression for the unanchored substring match in the post-stop nft
    teardown: ``grep "kento:web"`` matches ``kento:web2`` too, so stopping
    ``web`` would silently delete the still-running ``web2``'s DNAT/masquerade
    handles from the SHARED ``ip kento`` table. The fix anchors the grep to
    the full quoted comment token (``comment "kento:web"``).

    The nft stub models both instances co-resident in the shared table. We
    run ``web``'s post-stop and assert the only handle deleted is ``web``'s
    (5), never ``web2``'s (9).
    """
    fx = hook_fixture_factory(name="web")
    (fx.container_dir / "kento-portfwd-active").write_text("10270:22:10.0.3.1\n")
    # nft is the default backend (no backend marker -> nft).

    bin_dir = tmp_path / "bin"
    _write_nft_collision_stub(bin_dir, victim_name="web2")

    env = dict(fx.env)
    env["LXC_HOOK_TYPE"] = "post-stop"
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"

    result = subprocess.run(
        ["sh", str(fx.hook_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, (
        f"post-stop hook exited {result.returncode}.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    nft_log = bin_dir / "nft.log"
    assert nft_log.exists(), "nft was never invoked during teardown"
    log = nft_log.read_text()
    delete_lines = [ln for ln in log.splitlines() if "delete rule" in ln]

    # web2's handle (9) must NEVER be deleted — that's the live instance.
    assert not any("handle 9" in ln for ln in delete_lines), (
        "post-stop teardown for 'web' deleted web2's handle (9); the comment "
        "match is not anchored to the full token.\nnft delete calls:\n"
        + "\n".join(delete_lines)
    )
    # web's own handle (5) must be deleted in each of the three chains.
    web_deletes = [ln for ln in delete_lines if "handle 5" in ln]
    assert len(web_deletes) == 3, (
        "expected web's handle (5) deleted once per chain (3 total), got "
        f"{len(web_deletes)}:\n" + "\n".join(delete_lines)
    )

    assert not (fx.container_dir / "kento-portfwd-active").exists(), (
        "active marker must be removed after teardown"
    )


def test_post_stop_nft_teardown_anchors_comment_match():
    """Static guard: the generated nft teardown anchors the comment token.

    Complements the behavioural test above — asserts the generated hook
    content matches ``comment "kento:${NAME}"`` with a trailing boundary
    rather than an unanchored substring, so a future refactor can't silently
    reintroduce the prefix-collision bug.
    """
    from kento.hook import generate_hook

    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "web")
    post_body = script[script.index("post-stop)"):]
    # Anchored nft match: full quoted token + trailing boundary. The `$` in
    # the alternation is backslash-escaped in the source heredoc-free string.
    assert r'comment \"kento:${NAME}\"( |\$)' in post_body, (
        "nft teardown must anchor the comment match to the full quoted token"
    )
    # Must NOT use the old unanchored bare-substring grep.
    assert 'grep "kento:${NAME}"' not in post_body
    # iptables teardown likewise anchored with a trailing boundary (not -F).
    assert r'kento:${NAME}( |\$)' in post_body
    assert 'grep -F "kento:${NAME}"' not in post_body


# --- detached-worker orphan-rule race: stop-path defenses --------------------


def test_post_stop_removes_worker_and_sentinels(hook_fixture, tmp_path):
    """post-stop must remove the worker script + all kento-portfwd-* sentinels.

    A DHCP-mode container leaves a worker script and several sentinel files
    on disk (active/backend/cancel/pid/worker.sh). After the container stops,
    post-stop must clean ALL of them so a subsequent boot starts clean and no
    stale cancel sentinel survives to abort a fresh worker.
    """
    cdir = hook_fixture.container_dir
    # Seed the full set of portfwd state files a DHCP run can leave behind.
    (cdir / "kento-portfwd-active").write_text("10270:22:10.0.3.42\n")
    (cdir / "kento-portfwd-backend").write_text("nft\n")
    (cdir / "kento-portfwd-cancel").write_text("")
    (cdir / "kento-portfwd-pid").write_text("999999\n")  # almost-certainly dead
    (cdir / "kento-portfwd-worker.sh").write_text("#!/bin/sh\nexit 0\n")

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
        f"post-stop hook exited {result.returncode}.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    for leftover in (
        "kento-portfwd-active",
        "kento-portfwd-backend",
        "kento-portfwd-cancel",
        "kento-portfwd-pid",
        "kento-portfwd-worker.sh",
    ):
        assert not (cdir / leftover).exists(), (
            f"post-stop must remove {leftover}; still present after the hook "
            f"ran.\nstderr:\n{result.stderr}"
        )


def test_post_stop_static_cancel_and_pgid_kill():
    """Static guard: post-stop writes the cancel sentinel + signals the PGID.

    Complements the behavioural tests — asserts the generated hook content
    contains the cancel-sentinel write (defense #1 trigger), the
    process-group TERM (defense #2), and that teardown is no longer gated on
    the active marker (defense #3). A future refactor that drops any of these
    reintroduces the orphan-rule race.
    """
    from kento.hook import generate_hook

    script = generate_hook(Path("/var/lib/lxc/test"), "/a:/b", "web")
    post_body = script[script.index("post-stop)"):]

    # #1: cancel sentinel written at the start of post-stop.
    assert "kento-portfwd-cancel" in post_body
    # #2: process-group TERM (negative PID) to reap a still-polling worker.
    assert 'kill -TERM -- "-$WORKER_PID"' in post_body
    assert "kento-portfwd-pid" in post_body
    # #3: teardown no longer gated solely on the active marker. The old
    # guard wrapped the whole nft/iptables dance in
    # `if [ -f .../kento-portfwd-active ]; then`. Assert the nft list pass
    # is NOT nested under such a guard by checking the backend resolution
    # runs unconditionally (BACKEND=nft default sits before the chains loop
    # with no preceding active-marker `if`).
    idx_backend = post_body.index("BACKEND=nft")
    preamble = post_body[:idx_backend]
    assert 'if [ -f "$CONTAINER_DIR/kento-portfwd-active" ]; then' not in preamble, (
        "teardown must not be gated on the kento-portfwd-active marker"
    )
