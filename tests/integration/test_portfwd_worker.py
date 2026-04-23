"""Tier 1 coverage for the port-forwarding DHCP worker.

When ``start-host`` fires with a ``kento-port`` state file present AND no
static ``kento-net`` IP to install rules synchronously, the hook's
``setup_port_forwarding`` helper generates a worker script at
``$CONTAINER_DIR/kento-portfwd-worker.sh`` and launches it detached via
``setsid``. The worker polls ``lxc-info -n <cid> -iH`` until an IPv4
address appears, installs three nft rules (prerouting DNAT, output DNAT,
postrouting masquerade), and finally writes the
``$CONTAINER_DIR/kento-portfwd-active`` marker in the shape
``HOST_PORT:GUEST_PORT:IP``.

This test exercises the full worker pipeline without requiring a live
LXC container:

- ``lxc-info`` is replaced by a shell-script stub on PATH that echoes a
  canned IPv4 address matching the real ``lxc-info -iH`` output format
  (see ``src/kento/hook.sh`` line ~112 for the regex: one address per
  line, bare IPv4 dotted-quad).
- ``nft`` is stubbed with a log-and-exit-0 wrapper — we're asserting
  the worker's *output* (the marker file), not the firewall state.
- The hook itself is invoked under the v1 plain-LXC shape (env-only,
  no positional args) with ``LXC_HOOK_TYPE=start-host``.

Because the worker is backgrounded via ``setsid ... &``, subprocess.run
returns immediately. The test polls for the marker file with a generous
timeout.
"""

from __future__ import annotations

import os
import stat
import subprocess
import time


WORKER_POLL_TIMEOUT = 15.0  # seconds; worker retries lxc-info for up to ~30s
WORKER_POLL_INTERVAL = 0.1  # seconds


def _write_executable(path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_stubs(bin_dir, canned_ip: str) -> None:
    """Populate ``bin_dir`` with fake ``lxc-info`` and ``nft`` tools.

    ``lxc-info`` prints ``canned_ip`` on stdout ignoring its argv (the
    worker invokes it with ``-n <cid> -iH``). Output must be a bare IPv4
    dotted-quad — the worker's grep is ``^[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+$``.

    ``nft`` is a no-op that writes its argv to ``nft.log`` so the test
    can assert the worker actually reached the rule-install stage.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    _write_executable(
        bin_dir / "lxc-info",
        "#!/bin/sh\n"
        f"echo {canned_ip}\n"
        "exit 0\n",
    )
    _write_executable(
        bin_dir / "nft",
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{bin_dir}/nft.log"\n'
        "exit 0\n",
    )


def _wait_for_file(path, timeout: float = WORKER_POLL_TIMEOUT):
    """Spin until ``path`` exists or ``timeout`` elapses. Returns bool."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(WORKER_POLL_INTERVAL)
    return False


def test_portfwd_worker_writes_active_marker_with_canned_ip(
    hook_fixture, tmp_path
):
    """DHCP worker must install rules + write marker using canned IP.

    Seeds ``kento-port`` with a realistic spec, stubs ``lxc-info`` to
    return a fixed IPv4, stubs ``nft`` so rule installs are no-ops, then
    invokes ``start-host``. The hook launches the worker detached; we
    poll for the ``kento-portfwd-active`` marker and assert its contents
    match the expected ``HOST:GUEST:IP`` shape.
    """
    host_port = "10270"
    guest_port = "22"
    canned_ip = "10.0.3.42"

    # Seed kento-port (HOST:GUEST). hook.sh validates these are integers
    # in [1,65535]; no other state files are needed for the DHCP branch
    # (kento-net is deliberately absent so setup_port_forwarding falls
    # through to the worker).
    (hook_fixture.container_dir / "kento-port").write_text(
        f"{host_port}:{guest_port}\n"
    )
    assert not (hook_fixture.container_dir / "kento-net").exists()

    bin_dir = tmp_path / "bin"
    _write_stubs(bin_dir, canned_ip)

    env = dict(hook_fixture.env)
    env["LXC_HOOK_TYPE"] = "start-host"
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"

    result = subprocess.run(
        ["sh", str(hook_fixture.hook_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    # start-host returns quickly regardless of the worker's progress.
    assert result.returncode == 0, (
        f"start-host hook exited {result.returncode}.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    # The worker script should have been generated on disk.
    worker_path = hook_fixture.container_dir / "kento-portfwd-worker.sh"
    assert worker_path.exists(), (
        f"hook did not generate {worker_path}; DHCP branch may not have "
        f"been taken.\nhook stderr:\n{result.stderr}"
    )

    # Poll for the marker file — the worker runs asynchronously.
    active = hook_fixture.container_dir / "kento-portfwd-active"
    assert _wait_for_file(active), (
        f"worker never wrote {active} within {WORKER_POLL_TIMEOUT}s.\n"
        f"hook stderr:\n{result.stderr}\n"
        f"worker script:\n{worker_path.read_text()}\n"
        f"bin_dir contents: {sorted(p.name for p in bin_dir.iterdir())}"
    )

    contents = active.read_text().strip()
    expected = f"{host_port}:{guest_port}:{canned_ip}"
    assert contents == expected, (
        f"marker content mismatch. expected={expected!r} actual={contents!r}"
    )

    # Sanity: nft was actually called at least three times (prerouting,
    # output, postrouting). If the worker short-circuited before rule
    # install, the log would be empty or missing.
    nft_log = bin_dir / "nft.log"
    assert nft_log.exists(), (
        "nft stub was never invoked; worker didn't reach the rule-install "
        "stage."
    )
    nft_calls = [ln for ln in nft_log.read_text().splitlines() if ln]
    # Each nft invocation the worker makes is a single argv to the stub.
    # prerouting + output + postrouting = at least 3 rule calls; the
    # table/chain-create nft calls from the parent start-host run also
    # count (they precede the detachment). Assert >=3.
    assert len(nft_calls) >= 3, (
        f"expected >=3 nft calls (prerouting+output+postrouting rules), "
        f"got {len(nft_calls)}:\n" + "\n".join(nft_calls)
    )
