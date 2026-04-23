"""Regression test for commit dca8a55.

Plain LXC with ``lxc.hook.version = 1`` invokes hooks with NO positional
arguments — all context arrives via ``LXC_*`` env vars. Before dca8a55
the ``start-host`` branch referenced ``$1`` directly to derive the
container identifier (for the ``ns`` cgroup path and the portfwd helper
call). Under the script's ``set -u``, that aborted with
``1: unbound variable`` before any real work happened, so kento start
never installed port-forward rules or memory/cpu cgroup writes on plain
LXC.

The fix replaced ``$1`` with ``CONTAINER_ID="${LXC_NAME:-${1:-}}"`` and
threaded ``$CONTAINER_ID`` through the branch. This test invokes the
generated hook exactly the way plain LXC does — argv empty, env vars
only — and asserts it exits 0.
"""

from __future__ import annotations

import subprocess


def test_start_host_with_no_positional_args_and_env_only(hook_fixture):
    """Plain LXC hook.version=1 invocation must not abort on `$1` unbound.

    Mirrors the real invocation shape: ``sh <hook>`` with empty argv and
    LXC_* env vars populated. No ``kento-port`` file is laid down by the
    fixture, so the portfwd helper returns early and the test exercises
    just the CONTAINER_ID derivation + ns-cgroup existence check (which
    is a no-op outside PVE since ``/sys/fs/cgroup/lxc/<name>/ns`` does
    not exist on plain LXC hosts either).
    """
    env = dict(hook_fixture.env)
    env["LXC_HOOK_TYPE"] = "start-host"

    result = subprocess.run(
        ["sh", str(hook_fixture.hook_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    combined = (result.stderr or "") + (result.stdout or "")
    # bash says "unbound variable"; dash says "parameter not set". Both
    # indicate the dca8a55 regression (`$1` dereferenced with no argv).
    for needle in ("unbound variable", "parameter not set"):
        assert needle not in combined, (
            f"hook aborted on '{needle}' (dca8a55 regression); stderr:\n"
            f"{result.stderr}"
        )
    assert result.returncode == 0, (
        f"hook exited {result.returncode} under plain-LXC v1 invocation "
        f"(empty argv, env vars only).\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
