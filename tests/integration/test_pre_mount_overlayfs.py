"""Tier 1 coverage for the ``pre-start|pre-mount|mount`` overlayfs branch.

The hook's overlay-mount path is the load-bearing piece of kento's runtime
model: it composes N layer directories into an overlayfs rooted at
``$LXC_ROOTFS_PATH`` (PVE) or ``$CONTAINER_DIR/rootfs`` (plain LXC). If
this branch mis-mounts, mounts the wrong path, or aborts silently, kento
is broken end-to-end even though every unit test passes.

Cases:

1. **Happy path** (``test_pre_mount_mounts_overlayfs``): root-only.
   Invoke the hook with realistic env, assert the rootfs becomes a
   mountpoint, and tear it down. Gated by a ``geteuid() != 0`` skipif.

2. **Negative path** (``test_pre_mount_missing_layer_exits_with_error``):
   unprivileged. Remove one of the fixture's layer dirs and assert the
   hook exits 1 with the actionable error message pointing at
   ``kento scrub``. No mount is attempted, so this runs as any user.

3. **mount hook type** (``test_mount_hook_type_reaches_overlay_case``):
   Verifies that ``LXC_HOOK_TYPE=mount`` triggers the overlay-mount case
   (same branch as ``pre-start|pre-mount``). Uses the missing-layer
   negative path so no root is needed.

4. **Unprivileged: fail-closed on missing LXC_CONFIG_FILE**
   (``test_unprivileged_no_config_file_exits_nonzero``): sentinel present
   but ``LXC_CONFIG_FILE`` not set → exits 1 with clear message.

5. **Unprivileged: fail-closed on missing idmap line**
   (``test_unprivileged_no_idmap_in_config_exits_nonzero``): sentinel
   present, config exists but has no ``lxc.idmap = u ...`` line → exits 1.

6. **Unprivileged: real idmapped overlay mount**
   (``test_unprivileged_mounts_idmapped_overlayfs``): root-only.
   Full end-to-end: sentinel + synthetic config → idmapped bind mounts +
   overlay with ``userxattr,index=off,metacopy=off``. Asserts upper/work
   chowned to BASE, idmap dirs present as mountpoints, overlay mounted.

7. **Unprivileged: post-stop cleans idmap binds**
   (``test_unprivileged_post_stop_cleans_idmap_binds``): root-only.
   After an unprivileged mount, post-stop must unmount the idmap binds
   and remove ``$STATE_DIR/idmap``.
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


# ---------------------------------------------------------------------------
# mount hook type — reaches the overlay case
# ---------------------------------------------------------------------------

def test_mount_hook_type_reaches_overlay_case(hook_fixture):
    """LXC_HOOK_TYPE=mount must trigger the overlay-mount branch.

    pve-lxc unprivileged containers use ``lxc.hook.mount`` (real root,
    post-rootfs-setup) rather than ``pre-mount`` (which fails in the
    mapped userns). Verify ``mount`` falls into the same overlay-mount
    case by exercising the missing-layer negative path with
    ``LXC_HOOK_TYPE=mount`` — no root needed.
    """
    # Remove the second layer to trigger the "layer path missing" error.
    # If ``mount`` doesn't reach the overlay case we'd get a different
    # (or zero) exit code.
    layer_second = hook_fixture.layers.split(":")[1]
    os.rmdir(layer_second)

    env = dict(hook_fixture.env)
    env["LXC_HOOK_TYPE"] = "mount"

    result = subprocess.run(
        ["sh", str(hook_fixture.hook_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 1, (
        f"hook with LXC_HOOK_TYPE=mount should exit 1 on missing layer, "
        f"got {result.returncode}.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "layer path missing" in result.stderr, (
        f"LXC_HOOK_TYPE=mount must reach the overlay case (layer-check) "
        f"and produce 'layer path missing'; got:\n{result.stderr}"
    )
    assert "kento scrub" in result.stderr, (
        f"error message should point at `kento scrub` as remediation; got:\n"
        f"{result.stderr}"
    )


# ---------------------------------------------------------------------------
# Unprivileged path — fail-closed assertions (no root required)
# ---------------------------------------------------------------------------

def test_unprivileged_no_config_file_exits_nonzero(hook_fixture):
    """Unprivileged: exit 1 when LXC_CONFIG_FILE is not set.

    The hook must fail closed when the sentinel exists but
    ``LXC_CONFIG_FILE`` is absent — without it there is no source for
    the idmap range.
    """
    # Create the sentinel
    (hook_fixture.container_dir / "kento-unprivileged").write_text("")

    env = dict(hook_fixture.env)
    env["LXC_HOOK_TYPE"] = "pre-start"
    # Deliberately do NOT set LXC_CONFIG_FILE

    result = subprocess.run(
        ["sh", str(hook_fixture.hook_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 1, (
        f"hook should exit 1 when LXC_CONFIG_FILE is absent, "
        f"got {result.returncode}.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "LXC_CONFIG_FILE" in result.stderr, (
        f"error should mention LXC_CONFIG_FILE; got:\n{result.stderr}"
    )


def test_unprivileged_no_idmap_in_config_exits_nonzero(hook_fixture, tmp_path):
    """Unprivileged: exit 1 when config has no lxc.idmap = u ... line.

    The hook must fail closed when the sentinel exists, LXC_CONFIG_FILE
    points to a readable file, but that file contains no
    ``lxc.idmap = u ...`` line.
    """
    # Synthetic config with only a gid idmap line (no uid line)
    config_file = tmp_path / "lxc-no-uid-idmap.conf"
    config_file.write_text(
        "lxc.uts.name = test-container\n"
        "lxc.idmap = g 0 100000 65536\n"
        "lxc.rootfs.path = /some/path\n"
    )

    # Create the sentinel
    (hook_fixture.container_dir / "kento-unprivileged").write_text("")

    env = dict(hook_fixture.env)
    env["LXC_HOOK_TYPE"] = "pre-start"
    env["LXC_CONFIG_FILE"] = str(config_file)

    result = subprocess.run(
        ["sh", str(hook_fixture.hook_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 1, (
        f"hook should exit 1 when no lxc.idmap u line in config, "
        f"got {result.returncode}.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "lxc.idmap" in result.stderr, (
        f"error should mention lxc.idmap; got:\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# Unprivileged path — real mount tests (root-gated)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    os.geteuid() != 0,
    reason="unprivileged idmap overlay requires root (mount --bind + idmap + overlayfs)",
)
def test_unprivileged_mounts_idmapped_overlayfs(hook_fixture, tmp_path):
    """Unprivileged hook must produce an overlayfs over idmapped lowers.

    Full end-to-end: writes the sentinel + a synthetic LXC config with
    ``lxc.idmap = u 0 100000 65536``, invokes the hook, then asserts:

    - rootfs is a mountpoint,
    - ``$STATE_DIR/idmap/0`` and ``/1`` are mountpoints (idmapped binds),
    - ``$STATE_DIR/upper`` and ``$STATE_DIR/work`` are chowned to
      100000:100000,
    - the overlay mount options include ``userxattr``, ``index=off``,
      and ``metacopy=off`` (verified via /proc/self/mountinfo or findmnt).
    """
    # Synthetic LXC config with idmap lines as produced by kento (plain-lxc)
    # or PVE (pve-lxc).
    config_file = tmp_path / "lxc-idmap.conf"
    config_file.write_text(
        "lxc.uts.name = test-container\n"
        "lxc.idmap = u 0 100000 65536\n"
        "lxc.idmap = g 0 100000 65536\n"
        "lxc.rootfs.path = /some/path\n"
    )

    # Create the sentinel
    (hook_fixture.container_dir / "kento-unprivileged").write_text("")

    env = dict(hook_fixture.env)
    env["LXC_HOOK_TYPE"] = "pre-start"
    env["LXC_CONFIG_FILE"] = str(config_file)
    env["LIBMOUNT_FORCE_MOUNT2"] = "always"

    rootfs = hook_fixture.rootfs
    state_dir = hook_fixture.state_dir
    idmap_dir = state_dir / "idmap"

    # Helper: layers count
    layer_count = len(hook_fixture.layers.split(":"))

    try:
        result = subprocess.run(
            ["sh", str(hook_fixture.hook_path)],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, (
            f"unprivileged hook exited {result.returncode}.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        # rootfs must be an overlayfs mountpoint
        assert _is_mountpoint(rootfs), (
            f"rootfs {rootfs} is not a mountpoint after unprivileged hook.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        # Each idmapped lower must be a mountpoint
        for i in range(layer_count):
            idmap_target = idmap_dir / str(i)
            assert _is_mountpoint(idmap_target), (
                f"idmap bind mount {idmap_target} is not a mountpoint; "
                f"idmapped lower {i} was not created.\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )

        # upper and work must be chowned to 100000
        upper_stat = (state_dir / "upper").stat()
        work_stat = (state_dir / "work").stat()
        assert upper_stat.st_uid == 100000, (
            f"upper dir uid should be 100000, got {upper_stat.st_uid}"
        )
        assert upper_stat.st_gid == 100000, (
            f"upper dir gid should be 100000, got {upper_stat.st_gid}"
        )
        assert work_stat.st_uid == 100000, (
            f"work dir uid should be 100000, got {work_stat.st_uid}"
        )
        assert work_stat.st_gid == 100000, (
            f"work dir gid should be 100000, got {work_stat.st_gid}"
        )

        # Overlay mount options must include userxattr,index=off,metacopy=off
        # Read from /proc/self/mountinfo: the options field (field index 5 for
        # per-mount options, or the superblock options after the dash separator).
        rootfs_str = str(rootfs)
        mount_opts = _get_overlay_mount_opts(rootfs_str)
        assert "userxattr" in mount_opts, (
            f"overlay options should contain 'userxattr'; found: {mount_opts!r}"
        )
        assert "index=off" in mount_opts, (
            f"overlay options should contain 'index=off'; found: {mount_opts!r}"
        )
        assert "metacopy=off" in mount_opts, (
            f"overlay options should contain 'metacopy=off'; found: {mount_opts!r}"
        )

    finally:
        # Tear down in reverse order: overlay first, then idmap binds.
        if _is_mountpoint(rootfs):
            subprocess.run(["umount", str(rootfs)], check=False, capture_output=True)
        if idmap_dir.exists():
            for i in range(layer_count - 1, -1, -1):
                idmap_target = idmap_dir / str(i)
                if _is_mountpoint(idmap_target):
                    subprocess.run(
                        ["umount", str(idmap_target)],
                        check=False,
                        capture_output=True,
                    )


@pytest.mark.skipif(
    os.geteuid() != 0,
    reason="unprivileged idmap overlay requires root (mount --bind + idmap + overlayfs)",
)
def test_unprivileged_post_stop_cleans_idmap_binds(hook_fixture, tmp_path):
    """post-stop must unmount idmap binds and remove $STATE_DIR/idmap.

    Sequence: run the hook (pre-start), verify the overlay + idmap mounts
    are up, then invoke the hook with post-stop and verify:

    - rootfs is no longer a mountpoint,
    - $STATE_DIR/idmap no longer exists.
    """
    config_file = tmp_path / "lxc-idmap.conf"
    config_file.write_text(
        "lxc.uts.name = test-container\n"
        "lxc.idmap = u 0 100000 65536\n"
        "lxc.idmap = g 0 100000 65536\n"
    )

    # Create the sentinel
    (hook_fixture.container_dir / "kento-unprivileged").write_text("")

    state_dir = hook_fixture.state_dir
    rootfs = hook_fixture.rootfs
    idmap_dir = state_dir / "idmap"
    layer_count = len(hook_fixture.layers.split(":"))

    env_mount = dict(hook_fixture.env)
    env_mount["LXC_HOOK_TYPE"] = "pre-start"
    env_mount["LXC_CONFIG_FILE"] = str(config_file)
    env_mount["LIBMOUNT_FORCE_MOUNT2"] = "always"

    try:
        result = subprocess.run(
            ["sh", str(hook_fixture.hook_path)],
            env=env_mount,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, (
            f"pre-start hook exited {result.returncode}.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert _is_mountpoint(rootfs), (
            f"rootfs not a mountpoint after pre-start; setup failed.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        # Now invoke post-stop
        env_stop = dict(hook_fixture.env)
        env_stop["LXC_HOOK_TYPE"] = "post-stop"

        result_stop = subprocess.run(
            ["sh", str(hook_fixture.hook_path)],
            env=env_stop,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result_stop.returncode == 0, (
            f"post-stop hook exited {result_stop.returncode}.\n"
            f"stdout:\n{result_stop.stdout}\nstderr:\n{result_stop.stderr}"
        )

        # Overlay must be unmounted
        assert not _is_mountpoint(rootfs), (
            f"rootfs {rootfs} is still a mountpoint after post-stop"
        )

        # idmap directory must be gone
        assert not idmap_dir.exists(), (
            f"$STATE_DIR/idmap still exists after post-stop: {idmap_dir}"
        )

    finally:
        # Safety net: tear down any leftover mounts if the test failed
        # partway through.
        if _is_mountpoint(rootfs):
            subprocess.run(["umount", str(rootfs)], check=False, capture_output=True)
        if idmap_dir.exists():
            for i in range(layer_count - 1, -1, -1):
                idmap_target = idmap_dir / str(i)
                if _is_mountpoint(idmap_target):
                    subprocess.run(
                        ["umount", str(idmap_target)],
                        check=False,
                        capture_output=True,
                    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_overlay_mount_opts(mountpoint: str) -> str:
    """Return the superblock options string for an overlay mount.

    Parses ``/proc/self/mountinfo`` to find the line for ``mountpoint``
    and returns everything after the optional ``-`` separator (the
    filesystem-specific options field). Returns an empty string if the
    mountpoint is not found or if parsing fails.

    mountinfo format (kernel docs):
      ID parent major:minor root mnt-point mnt-opts [opt-fields] - fs-type src super-opts
    """
    try:
        with open("/proc/self/mountinfo") as fp:
            for line in fp:
                parts = line.split()
                if len(parts) < 5:
                    continue
                if parts[4] != mountpoint:
                    continue
                # Find the dash separator that marks start of filesystem info
                try:
                    dash_idx = parts.index("-")
                except ValueError:
                    return ""
                # super-opts is the field after: - fstype source super-opts
                if dash_idx + 3 < len(parts):
                    return parts[dash_idx + 3]
                return ""
    except OSError:
        pass
    return ""
