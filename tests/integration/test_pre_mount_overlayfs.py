"""Tier 1 coverage for the ``pre-start|pre-mount`` overlayfs branch.

The hook's pre-mount path is the load-bearing piece of kento's runtime
model: it composes N layer directories into an overlayfs rooted at
``$LXC_ROOTFS_PATH`` (PVE) or ``$CONTAINER_DIR/rootfs`` (plain LXC). If
this branch mis-mounts, mounts the wrong path, or aborts silently, kento
is broken end-to-end even though every unit test passes.

Two cases here:

1. **Happy path** (``test_pre_mount_mounts_overlayfs``): root-only.
   Invoke the hook with realistic env, assert the rootfs becomes a
   mountpoint, and tear it down. Gated by a ``geteuid() != 0`` skipif.

2. **Negative path** (``test_pre_mount_missing_layer_exits_with_error``):
   unprivileged. Remove one of the fixture's layer dirs and assert the
   hook exits 1 with the actionable error message pointing at
   ``kento scrub``. No mount is attempted, so this runs as any user.
"""

from __future__ import annotations

import os
import subprocess

import pytest


def _is_mountpoint(path) -> bool:
    """True if ``path`` is a mountpoint according to the kernel.

    ``os.path.ismount`` works fine for this (compares st_dev with the
    parent), but we also consult ``/proc/self/mountinfo`` as a sanity
    check in case the filesystem layout does something unusual.
    """
    if os.path.ismount(str(path)):
        return True
    target = str(path)
    try:
        with open("/proc/self/mountinfo") as fp:
            for line in fp:
                # Format: ID parent major:minor root mountpoint options ...
                parts = line.split()
                if len(parts) >= 5 and parts[4] == target:
                    return True
    except OSError:
        pass
    return False


@pytest.mark.skipif(
    os.geteuid() != 0,
    reason="pre-mount overlayfs branch requires root (mount syscall)",
)
def test_pre_mount_mounts_overlayfs(hook_fixture):
    """Hook's pre-mount branch must produce an actual overlayfs mount.

    Invokes the hook in the v1 plain-LXC shape: empty argv + env vars
    including ``LXC_HOOK_TYPE=pre-mount``. hook.sh treats ``pre-start``
    and ``pre-mount`` as the same branch (``pre-start|pre-mount)``), so
    this also covers the plain-LXC pre-start path — we pick ``pre-mount``
    to align with the PVE-LXC runtime shape where ``pre-mount`` is the
    actually-scheduled phase (``lxc.hook.pre-mount:`` in pve.py).

    ``LIBMOUNT_FORCE_MOUNT2=always`` is set per CONVENTIONS.md to work
    around the kernel-6.x ``fsconfig``/overlayfs regression that causes
    the new-mount-API path to fail with ESTALE on some hosts. The hook
    itself also exports this internally, but we set it in the caller's
    env too so the subshell inherits a consistent value.
    """
    env = dict(hook_fixture.env)
    env["LXC_HOOK_TYPE"] = "pre-mount"
    env["LIBMOUNT_FORCE_MOUNT2"] = "always"

    rootfs = hook_fixture.rootfs
    try:
        result = subprocess.run(
            ["sh", str(hook_fixture.hook_path)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"pre-mount hook exited {result.returncode}.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert _is_mountpoint(rootfs), (
            f"pre-mount hook returned 0 but {rootfs} is not a mountpoint; "
            f"overlayfs never got composed.\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    finally:
        # Always tear down, even on assertion failure, so subsequent
        # tests don't trip over a stale mount at the same tmp_path.
        if _is_mountpoint(rootfs):
            subprocess.run(
                ["umount", str(rootfs)],
                check=False,
                capture_output=True,
            )


def test_pre_mount_missing_layer_exits_with_error(hook_fixture):
    """Missing layer dir must abort the hook with an actionable message.

    The layer-existence check sits at the top of the pre-start|pre-mount
    branch and runs before the mount syscall, so this case doesn't need
    root. We delete one of the two fixture-provided layer dirs, then
    invoke the hook and confirm:

    - exit code is 1 (hook aborted before mounting),
    - stderr names the missing dir,
    - stderr points at ``kento scrub`` as the remediation.
    """
    # Remove the second layer to trigger the "layer path missing" branch.
    layer_second = hook_fixture.layers.split(":")[1]
    # rmdir requires the dir to be empty — our fixture makes them empty,
    # so this works without rmtree.
    os.rmdir(layer_second)

    env = dict(hook_fixture.env)
    env["LXC_HOOK_TYPE"] = "pre-mount"

    result = subprocess.run(
        ["sh", str(hook_fixture.hook_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 1, (
        f"hook should exit 1 on missing layer, got {result.returncode}.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "layer path missing" in result.stderr, (
        f"error message should mention 'layer path missing'; got:\n"
        f"{result.stderr}"
    )
    assert layer_second in result.stderr, (
        f"error message should name the missing dir {layer_second!r}; got:\n"
        f"{result.stderr}"
    )
    assert "kento scrub" in result.stderr, (
        f"error message should point at `kento scrub` as remediation; got:\n"
        f"{result.stderr}"
    )
