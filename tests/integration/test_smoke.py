"""Smoke test for the Tier 1 hook fixture scaffolding.

Validates that the fixture produces a generated, executable, non-empty
hook script and the expected on-disk state files. Deliberately avoids
semantic assertions about hook behavior — those belong to the targeted
tests added in A2+.
"""

from pathlib import Path


def test_hook_fixture_generates_nonempty_hook(hook_fixture):
    assert hook_fixture.hook_path.exists()
    assert hook_fixture.hook_path.is_file()
    assert hook_fixture.hook_path.stat().st_size > 0
    # Executable bit is set by write_hook (chmod 0o755).
    assert hook_fixture.hook_path.stat().st_mode & 0o111


def test_hook_fixture_lays_out_state_files(hook_fixture):
    cd = hook_fixture.container_dir
    for fname in ("kento-image", "kento-layers", "kento-state",
                  "kento-mode", "kento-name", "kento-inject.sh"):
        p = cd / fname
        assert p.exists(), f"missing state file: {fname}"
        assert p.read_text(), f"empty state file: {fname}"


def test_hook_fixture_factory_produces_distinct_dirs(hook_fixture_factory):
    a = hook_fixture_factory(name="one")
    b = hook_fixture_factory(name="two")
    assert a.container_dir != b.container_dir
    assert a.hook_path.exists()
    assert b.hook_path.exists()


def test_hook_fixture_env_has_lxc_vars(hook_fixture):
    # Defaults target a v1-style plain-LXC invocation.
    assert hook_fixture.env["LXC_NAME"] == hook_fixture.name
    assert hook_fixture.env["LXC_ROOTFS_PATH"] == str(hook_fixture.rootfs)
    assert "PATH" in hook_fixture.env
