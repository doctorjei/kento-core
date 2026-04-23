"""Fixtures for Tier 1 integration tests.

Tier 1 = direct subprocess invocation of the generated kento-hook with
LXC-realistic env vars. No real LXC required. Catches hook-script syntax
and runtime bugs (e.g. the dca8a55 regression) that unit-level string
assertions miss.

See ``~/playbook/plans/lxc-in-lxc-tests.md`` "Step 1 — Tier 1".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from kento.hook import write_hook
from kento.inject import write_inject


@dataclass
class HookFixture:
    """Handle to a generated kento-hook and its on-disk state.

    Fields:
        hook_path: path to the generated ``kento-hook`` script (executable).
        container_dir: the per-container state directory (``<base>/<name>``).
        state_dir: overlayfs upper/work base (defaults to ``container_dir``).
        name: container name baked into the hook.
        image: image reference recorded in ``kento-image``.
        layers: colon-joined overlay layer paths baked into the hook.
        rootfs: path the hook would mount the overlayfs at
            (``container_dir/rootfs``, created empty for tests that pass
            it as ``$LXC_ROOTFS_PATH``).
        env: default env vars a test can pass to ``subprocess.run``. Tests
            can mutate / supplement this copy.
    """

    hook_path: Path
    container_dir: Path
    state_dir: Path
    name: str
    image: str
    layers: str
    rootfs: Path
    env: dict = field(default_factory=dict)


def _make_hook_fixture(
    tmp_path: Path,
    *,
    name: str = "test-container",
    image: str = "docker.io/library/alpine:latest",
    hook_version: str = "v1",
) -> HookFixture:
    """Build a realistic LXC-style container directory + generated hook.

    Lays out ``<tmp_path>/lxc/<name>/`` with the state files the hook
    reads at runtime (``kento-image``, ``kento-layers``, ``kento-state``,
    ``kento-mode``, ``kento-name``) plus a companion ``kento-inject.sh``
    so the pre-mount branch doesn't fail on a missing delegate.

    ``hook_version`` is informational — the hook template is identical
    regardless; it just documents which invocation shape the caller
    expects to test. The default env dict is populated for v1 (plain
    LXC: env-var-only, no positional args); v0 callers should pass
    positional args through ``subprocess.run(..., args=[...])``.

    ``layers`` uses two real directories under tmp_path so the hook's
    layer-existence check passes. Tests that want the missing-layer
    branch can delete one of them before invoking.
    """

    base = tmp_path / "lxc"
    container_dir = base / name
    container_dir.mkdir(parents=True)

    # Two real, empty layer dirs so the hook's missing-layer check passes
    # by default. Tests that want the failure branch can rm one.
    layer_a = tmp_path / "layers" / "a"
    layer_b = tmp_path / "layers" / "b"
    layer_a.mkdir(parents=True)
    layer_b.mkdir(parents=True)
    layers = f"{layer_a}:{layer_b}"

    # State dir (overlayfs upper/work live here). For tests we co-locate
    # it with the container dir — matches the default hook behavior.
    state_dir = container_dir

    # Rootfs is the overlayfs mount target. Create it empty; mount tests
    # may need root and live in a separate skipif branch.
    rootfs = container_dir / "rootfs"
    rootfs.mkdir()

    # Kento state files the hook + inject.sh read at runtime.
    (container_dir / "kento-image").write_text(image + "\n")
    (container_dir / "kento-layers").write_text(layers + "\n")
    (container_dir / "kento-state").write_text(str(state_dir) + "\n")
    (container_dir / "kento-mode").write_text("lxc\n")
    (container_dir / "kento-name").write_text(name + "\n")

    # Real hook generation + real inject.sh copy. Use the production
    # write_hook() so we exercise the actual template-substitution path.
    hook_path = write_hook(container_dir, layers, name, state_dir=state_dir)
    write_inject(container_dir)

    # Default env for a v1-style plain-LXC invocation: env vars only, no
    # positional args. Tests for v0 (positional args) or for snippets
    # wrapper shapes can start from this and override.
    env = {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "LXC_NAME": name,
        "LXC_ROOTFS_PATH": str(rootfs),
    }

    return HookFixture(
        hook_path=hook_path,
        container_dir=container_dir,
        state_dir=state_dir,
        name=name,
        image=image,
        layers=layers,
        rootfs=rootfs,
        env=env,
    )


@pytest.fixture
def hook_fixture(tmp_path):
    """Default hook_fixture: alpine image, v1 hook-version shape.

    Tests that need different parameters should use
    ``hook_fixture_factory`` instead.
    """
    return _make_hook_fixture(tmp_path)


@pytest.fixture
def hook_fixture_factory(tmp_path):
    """Factory variant. Call with keyword args to customize.

    Example:
        def test_something(hook_fixture_factory):
            fx = hook_fixture_factory(name="other", hook_version="v0")
    """
    # Each call gets its own subdir so multiple invocations in one test
    # don't collide on the same <tmp_path>/lxc/<name>/ path.
    counter = {"n": 0}

    def _factory(**kwargs):
        counter["n"] += 1
        subdir = tmp_path / f"fx{counter['n']}"
        subdir.mkdir()
        return _make_hook_fixture(subdir, **kwargs)

    return _factory
