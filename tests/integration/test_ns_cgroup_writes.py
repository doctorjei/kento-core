"""Tier 1 coverage for the pve-lxc-inner ``ns`` cgroup write branch.

PVE's LXC config nests the container cgroup one level below the
accounting cgroup (``lxc.cgroup.dir.container.inner = ns``), so the
memory/cpu limits kento emits as ``lxc.cgroup2.*`` land on the outer
(accounting) cgroup at ``/sys/fs/cgroup/lxc/<vmid>/`` while processes
actually run in ``/sys/fs/cgroup/lxc/<vmid>/ns/``. Without an explicit
propagation into the inner cgroup, the guest sees ``max`` at
``/sys/fs/cgroup/memory.max`` and won't enforce the limit internally.

hook.sh handles this by writing the requested memory/cpu values into
``$NS_CGROUP/memory.max`` and ``$NS_CGROUP/cpu.max`` during the
``start-host`` phase. On plain LXC the ns dir does not exist, so the
entire block is silently skipped.

This module exercises both sides:

- **Skip path** — ``kento-memory`` present, ns dir absent. The hook
  must exit 0 and not attempt any write. Mirrors plain-LXC topology.

- **Write path** — ``kento-memory`` and ``kento-cores`` present, with
  the ``NS_CGROUP`` path redirected to a tmpdir via the
  ``KENTO_TEST_NS_CGROUP`` env override (a small testability hook added
  to ``src/kento/hook.sh``: default is unchanged; when the env var is
  set the hook reads/writes to that path instead). The fake ns dir
  contains world-writable ``memory.max`` and ``cpu.max`` files we can
  inspect after the hook runs.

Invocation shape is v1 plain-LXC (env vars only) for the skip case;
the write case uses the same shape plus the ``KENTO_TEST_NS_CGROUP``
override. LXC_NAME is populated so ``CONTAINER_ID`` resolves via the
env branch of ``${LXC_NAME:-${1:-}}``.
"""

from __future__ import annotations

import subprocess


def test_start_host_ns_cgroup_absent_is_silent_skip(hook_fixture):
    """``kento-memory`` present + no ns dir on disk → hook exits 0, no writes.

    The hook guards its cgroup writes with ``[ -d "$NS_CGROUP" ]``. On
    plain LXC hosts the ns dir (``/sys/fs/cgroup/lxc/<id>/ns``) does not
    exist, so the entire block is a no-op. This test doesn't need the
    ``KENTO_TEST_NS_CGROUP`` override because the default path is
    already guaranteed to be missing (the fixture container name is a
    uuid-style ``test-container`` that no real LXC instance would have).
    """
    (hook_fixture.container_dir / "kento-memory").write_text("512\n")

    env = dict(hook_fixture.env)
    env["LXC_HOOK_TYPE"] = "start-host"

    result = subprocess.run(
        ["sh", str(hook_fixture.hook_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, (
        f"hook exited {result.returncode} when ns cgroup dir is absent; "
        f"the [ -d $NS_CGROUP ] guard should make this a silent no-op.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # No "warning: could not set memory.max" — the guard short-circuits
    # before the write attempt, so there's nothing to warn about.
    assert "could not set memory.max" not in result.stderr, (
        f"hook emitted a cgroup warning despite the guard short-circuiting.\n"
        f"stderr:\n{result.stderr}"
    )


def test_start_host_ns_cgroup_writes_memory_and_cpu_when_dir_exists(
    hook_fixture, tmp_path
):
    """Mock ns cgroup dir + KENTO_TEST_NS_CGROUP override → writes fire.

    Creates ``<tmp_path>/fake-ns-cgroup/`` with empty ``memory.max`` and
    ``cpu.max`` files (world-writable). Seeds ``kento-memory=512`` and
    ``kento-cores=2``. Invokes ``start-host`` with the override pointing
    at the fake dir. Asserts both cgroup files contain the byte-exact
    values hook.sh should compute:

    - ``memory.max`` = 512 * 1024 * 1024 = 536870912
    - ``cpu.max`` = "200000 100000" (cores=2 → quota=cores*100000, period=100000)
    """
    fake_ns = tmp_path / "fake-ns-cgroup"
    fake_ns.mkdir()
    memory_max = fake_ns / "memory.max"
    cpu_max = fake_ns / "cpu.max"
    # Start with placeholder content — hook.sh overwrites, does not append.
    memory_max.write_text("max\n")
    cpu_max.write_text("max 100000\n")
    memory_max.chmod(0o666)
    cpu_max.chmod(0o666)

    (hook_fixture.container_dir / "kento-memory").write_text("512\n")
    (hook_fixture.container_dir / "kento-cores").write_text("2\n")

    env = dict(hook_fixture.env)
    env["LXC_HOOK_TYPE"] = "start-host"
    env["KENTO_TEST_NS_CGROUP"] = str(fake_ns)

    result = subprocess.run(
        ["sh", str(hook_fixture.hook_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, (
        f"hook exited {result.returncode} with KENTO_TEST_NS_CGROUP set.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    expected_bytes = str(512 * 1024 * 1024)  # "536870912"
    assert memory_max.read_text().strip() == expected_bytes, (
        f"memory.max not written correctly.\n"
        f"expected: {expected_bytes!r}\n"
        f"actual:   {memory_max.read_text()!r}\n"
        f"hook stderr:\n{result.stderr}"
    )

    # cpu.max format is "<quota> <period>"; hook.sh uses quota=cores*100000
    # and period=100000 (the kernel default). cores=2 → "200000 100000".
    expected_cpu = "200000 100000"
    assert cpu_max.read_text().strip() == expected_cpu, (
        f"cpu.max not written correctly.\n"
        f"expected: {expected_cpu!r}\n"
        f"actual:   {cpu_max.read_text()!r}\n"
        f"hook stderr:\n{result.stderr}"
    )


def test_start_host_ns_cgroup_memory_only_no_cores(hook_fixture, tmp_path):
    """Writes are independent: ``kento-memory`` without ``kento-cores`` is fine.

    Confirms hook.sh's two inner ``[ -f ... ]`` checks are independent —
    one missing file must not block the other. Mirror of the combined
    test above but with ``kento-cores`` deliberately absent; asserts the
    initial placeholder content of ``cpu.max`` is untouched.
    """
    fake_ns = tmp_path / "fake-ns-cgroup"
    fake_ns.mkdir()
    memory_max = fake_ns / "memory.max"
    cpu_max = fake_ns / "cpu.max"
    memory_max.write_text("max\n")
    cpu_max.write_text("untouched\n")
    memory_max.chmod(0o666)
    cpu_max.chmod(0o666)

    (hook_fixture.container_dir / "kento-memory").write_text("256\n")
    # Deliberately no kento-cores.
    assert not (hook_fixture.container_dir / "kento-cores").exists()

    env = dict(hook_fixture.env)
    env["LXC_HOOK_TYPE"] = "start-host"
    env["KENTO_TEST_NS_CGROUP"] = str(fake_ns)

    result = subprocess.run(
        ["sh", str(hook_fixture.hook_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, (
        f"hook exited {result.returncode} with memory-only ns cgroup state.\n"
        f"stderr:\n{result.stderr}"
    )
    expected_bytes = str(256 * 1024 * 1024)
    assert memory_max.read_text().strip() == expected_bytes
    # cpu.max must be untouched — its [ -f kento-cores ] guard failed.
    assert cpu_max.read_text().strip() == "untouched"
