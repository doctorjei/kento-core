"""Tier 1 coverage for LXC ``hook.version = 0`` invocations.

PVE uses hook.version=0, where LXC passes three positional args to every
hook: ``$1`` = container identifier, ``$2`` = section (``lxc``),
``$3`` = hook type (``pre-mount``, ``start-host``, ``post-stop``, ...).
Env vars (``LXC_NAME``, ``LXC_ROOTFS_PATH``, ...) are still set, but
``LXC_HOOK_TYPE`` is **not** — the type comes from ``$3``. The hook
template derives ``HOOK_TYPE`` with ``${LXC_HOOK_TYPE:-$3}`` precisely
to straddle both regimes (v1 plain LXC: env-only; v0 PVE: positional +
env; snippets-wrapper: positional synthesized).

The ``dca8a55`` fix introduced ``CONTAINER_ID="${LXC_NAME:-${1:-}}"`` so
the start-host branch survives ``set -u`` whether the identifier arrives
via env (``LXC_NAME``) or argv (``$1``). This module exercises both
halves of that ``:-`` expression under the v0 invocation shape.
"""

from __future__ import annotations

import subprocess


def test_start_host_v0_positional_plus_env(hook_fixture):
    """v0 invocation with both LXC_NAME env and $1 positional set.

    Matches the shape LXC itself uses for hook.version=0: argv is
    ``[container_id, "lxc", "start-host"]`` and the LXC_* env vars are
    populated as usual. ``LXC_HOOK_TYPE`` is deliberately absent — the
    hook must fall back to ``$3`` to pick the branch.
    """
    env = dict(hook_fixture.env)
    # v0 does NOT set LXC_HOOK_TYPE. If the hook fails to honor $3,
    # HOOK_TYPE expands to empty and none of the case branches match,
    # which is a silent no-op but not an error. Explicit exercise by
    # also confirming returncode==0 and no set-u abort.
    env.pop("LXC_HOOK_TYPE", None)

    result = subprocess.run(
        ["sh", str(hook_fixture.hook_path),
         hook_fixture.name, "lxc", "start-host"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    combined = (result.stderr or "") + (result.stdout or "")
    for needle in ("unbound variable", "parameter not set"):
        assert needle not in combined, (
            f"hook aborted on '{needle}' under v0 invocation; stderr:\n"
            f"{result.stderr}"
        )
    assert result.returncode == 0, (
        f"hook exited {result.returncode} under v0 positional invocation.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_start_host_v0_positional_only_no_lxc_name(hook_fixture):
    """v0 invocation with LXC_NAME unset — CONTAINER_ID must fall back to $1.

    The dca8a55 fix wrote ``CONTAINER_ID="${LXC_NAME:-${1:-}}"``. If
    LXC_NAME is present, the inner ``${1:-}`` is never evaluated — so
    test_start_host_v0_positional_plus_env only proves the outer branch.
    This test forces the inner branch by dropping LXC_NAME from the env,
    leaving ``$1`` as the sole source for the container identifier. This
    is the shape kento's own PVE snippets wrapper uses (see
    ``src/kento/lxc_hook.py``): it execs the hook with ``$1=VMID`` and
    no LXC_NAME set.
    """
    env = dict(hook_fixture.env)
    env.pop("LXC_NAME", None)
    env.pop("LXC_HOOK_TYPE", None)

    result = subprocess.run(
        ["sh", str(hook_fixture.hook_path),
         hook_fixture.name, "lxc", "start-host"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    combined = (result.stderr or "") + (result.stdout or "")
    for needle in ("unbound variable", "parameter not set"):
        assert needle not in combined, (
            f"hook aborted on '{needle}' with LXC_NAME unset; the $1 "
            f"fallback of CONTAINER_ID=\"${{LXC_NAME:-${{1:-}}}}\" did not "
            f"take effect. stderr:\n{result.stderr}"
        )
    assert result.returncode == 0, (
        f"hook exited {result.returncode} under v0 snippets-wrapper shape "
        f"(LXC_NAME unset, $1={hook_fixture.name!r}).\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
