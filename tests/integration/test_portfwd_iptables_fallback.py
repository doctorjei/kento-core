"""Tier 1 coverage for the port-forwarding iptables fallback.

``setup_port_forwarding`` resolves a NAT backend at install time: ``nft``
if present on PATH, else ``iptables``, else neither (skip-with-warning).
These tests drive the iptables fallback and the neither-present skip by
controlling which stub binaries appear on ``PATH``.

Stub pattern mirrors ``test_portfwd_worker.py``: each backend binary is a
shell wrapper on a temp PATH that logs its argv to a sibling ``.log`` file
and exits 0. We assert the hook's *behaviour* (which binary it invoked,
which marker files it wrote), not real firewall state.

Covered:
  1. iptables fallback, static IP  — nft absent, iptables present, kento-net
     seeded with a static IP -> 3 iptables rule calls + active marker +
     backend marker == ``iptables``.
  2. iptables fallback, DHCP worker — nft absent, iptables + lxc-info present,
     no kento-net -> worker installs via iptables; markers reflect iptables.
  3. neither backend — PATH with neither nft nor iptables -> start-host exits
     0 (does NOT abort under ``set -eu``), writes kento-portfwd-error, no
     kento-portfwd-active.
  4. post-stop iptables teardown — backend marker == iptables + iptables stub
     recording ``-D`` calls -> post-stop issues deletes + removes markers.
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


def _iptables_stub(bin_dir, logname: str = "iptables.log") -> None:
    """Install an ``iptables`` stub that logs argv and exits 0.

    For the teardown test the stub must answer ``-L ... --line-numbers``
    queries so the post-stop delete loop terminates: first list returns one
    numbered matching line, subsequent lists return nothing (rule "deleted").
    Keep it simple here — the install stub just logs and exits.
    """
    _write_executable(
        bin_dir / "iptables",
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{bin_dir}/{logname}"\n'
        "exit 0\n",
    )


def _lxc_info_stub(bin_dir, canned_ip: str) -> None:
    _write_executable(
        bin_dir / "lxc-info",
        "#!/bin/sh\n"
        f"echo {canned_ip}\n"
        "exit 0\n",
    )


def _wait_for_file(path, timeout: float = WORKER_POLL_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(WORKER_POLL_INTERVAL)
    return False


def _run_hook(hook_fixture, bin_dir, hook_type="start-host", isolate_path=True):
    """Run the hook with ``bin_dir`` as the (optionally sole) PATH.

    ``isolate_path=True`` restricts PATH to ``bin_dir`` plus the coreutils
    dirs ``/usr/bin:/bin`` only — deliberately EXCLUDING ``/usr/sbin:/sbin``
    where the real ``nft``/``iptables`` live, so the hook sees only the stub
    binaries we placed (or none). The coreutils the hook needs (sh, grep,
    awk, cat, sed, cut, head, tr) all live in ``/usr/bin``/``/bin``.
    """
    env = dict(hook_fixture.env)
    env["LXC_HOOK_TYPE"] = hook_type
    if isolate_path:
        env["PATH"] = f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"
    else:
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        ["sh", str(hook_fixture.hook_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


# --- 1. iptables fallback, static IP -----------------------------------------


def test_iptables_fallback_static_ip(hook_fixture, tmp_path):
    """nft absent, iptables present, static kento-net -> iptables install."""
    host_port = "10270"
    guest_port = "22"
    static_ip = "10.0.3.42"

    (hook_fixture.container_dir / "kento-port").write_text(
        f"{host_port}:{guest_port}\n"
    )
    (hook_fixture.container_dir / "kento-net").write_text(
        f"ip={static_ip}/24\n"
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    _iptables_stub(bin_dir)  # nft deliberately NOT installed

    result = _run_hook(hook_fixture, bin_dir)
    assert result.returncode == 0, (
        f"start-host exited {result.returncode}.\nstderr:\n{result.stderr}"
    )

    active = hook_fixture.container_dir / "kento-portfwd-active"
    assert active.exists(), (
        f"missing active marker.\nstderr:\n{result.stderr}"
    )
    # Block 14: marker is one "HOST:GUEST:IP:PROTO" line per forward (tcp default).
    assert active.read_text().strip() == (
        f"{host_port}:{guest_port}:{static_ip}:tcp"
    )

    backend = hook_fixture.container_dir / "kento-portfwd-backend"
    assert backend.exists(), "missing backend marker"
    assert backend.read_text().strip() == "iptables"

    # nft must NOT have been written (we never stubbed it; assert it really
    # took the iptables branch by counting iptables rule calls).
    log = bin_dir / "iptables.log"
    assert log.exists(), "iptables stub was never invoked"
    calls = [ln for ln in log.read_text().splitlines() if ln]
    rule_calls = [ln for ln in calls if " -A " in f" {ln} "]
    assert len(rule_calls) == 3, (
        f"expected 3 iptables -A rule calls, got {len(rule_calls)}:\n"
        + "\n".join(calls)
    )


# --- 2. iptables fallback, DHCP worker ---------------------------------------


def test_iptables_fallback_dhcp_worker(hook_fixture, tmp_path):
    """nft absent, iptables + lxc-info present, no kento-net -> worker path."""
    host_port = "10271"
    guest_port = "22"
    canned_ip = "10.0.3.99"

    (hook_fixture.container_dir / "kento-port").write_text(
        f"{host_port}:{guest_port}\n"
    )
    assert not (hook_fixture.container_dir / "kento-net").exists()

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    _iptables_stub(bin_dir)
    _lxc_info_stub(bin_dir, canned_ip)  # nft deliberately NOT installed

    result = _run_hook(hook_fixture, bin_dir)
    assert result.returncode == 0, (
        f"start-host exited {result.returncode}.\nstderr:\n{result.stderr}"
    )

    worker_path = hook_fixture.container_dir / "kento-portfwd-worker.sh"
    assert worker_path.exists(), (
        f"worker not generated.\nstderr:\n{result.stderr}"
    )
    # Backend resolved at install time must be baked into the worker.
    assert 'BACKEND="iptables"' in worker_path.read_text()

    active = hook_fixture.container_dir / "kento-portfwd-active"
    assert _wait_for_file(active), (
        f"worker never wrote {active}.\nworker:\n{worker_path.read_text()}"
    )
    # Block 14: marker is one "HOST:GUEST:IP:PROTO" line per forward (tcp default).
    assert active.read_text().strip() == (
        f"{host_port}:{guest_port}:{canned_ip}:tcp"
    )

    backend = hook_fixture.container_dir / "kento-portfwd-backend"
    assert backend.exists() and backend.read_text().strip() == "iptables"

    log = bin_dir / "iptables.log"
    assert log.exists(), "iptables stub never invoked by worker"
    rule_calls = [
        ln for ln in log.read_text().splitlines() if ln and " -A " in f" {ln} "
    ]
    assert len(rule_calls) == 3, (
        f"expected 3 iptables -A rule calls in worker, got {len(rule_calls)}"
    )


# --- 3. neither backend present ----------------------------------------------


def test_neither_backend_does_not_abort(hook_fixture, tmp_path):
    """Neither nft nor iptables -> start-host exits 0, writes error marker."""
    (hook_fixture.container_dir / "kento-port").write_text("10272:22\n")
    (hook_fixture.container_dir / "kento-net").write_text("ip=10.0.3.7/24\n")

    # Empty stub dir: PATH has only coreutils, no nft, no iptables.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    # Sanity: ensure neither backend leaks in from the coreutils PATH
    # entries the isolated hook run uses (sbin is excluded by isolation).
    for tool in ("nft", "iptables"):
        found = subprocess.run(
            ["sh", "-c", f"command -v {tool}"],
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
        )
        if found.returncode == 0:
            import pytest

            pytest.skip(f"{tool} present in base PATH; cannot test absence")

    result = _run_hook(hook_fixture, bin_dir)
    assert result.returncode == 0, (
        "start-host must NOT abort when no NAT backend is present; "
        f"exited {result.returncode}.\nstderr:\n{result.stderr}"
    )

    assert not (hook_fixture.container_dir / "kento-portfwd-active").exists(), (
        "active marker must not be written when no backend installed rules"
    )
    err = hook_fixture.container_dir / "kento-portfwd-error"
    assert err.exists(), "expected kento-portfwd-error marker"
    assert "nft" in err.read_text() and "iptables" in err.read_text()


# --- 4. post-stop iptables teardown ------------------------------------------


def test_post_stop_iptables_teardown(hook_fixture, tmp_path):
    """backend marker == iptables -> post-stop deletes rules + removes markers."""
    cdir = hook_fixture.container_dir
    (cdir / "kento-portfwd-active").write_text("10273:22:10.0.3.50\n")
    (cdir / "kento-portfwd-backend").write_text("iptables\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    # iptables stub that:
    #  - on `-L <chain> --line-numbers ...` returns one numbered matching
    #    line the FIRST time per chain, then nothing (rule "deleted").
    #  - on `-D ...` logs the delete and exits 0.
    # State is tracked per-chain via marker files so the post-stop while-loop
    # terminates after exactly one delete per chain.
    _write_executable(
        bin_dir / "iptables",
        "#!/bin/sh\n"
        f'LOG="{bin_dir}/iptables.log"\n'
        f'STATE="{bin_dir}"\n'
        'printf "%s\\n" "$*" >> "$LOG"\n'
        "# Parse out the chain name following -L or -D.\n"
        "mode=\"\"\n"
        "chain=\"\"\n"
        "prev=\"\"\n"
        'for a in "$@"; do\n'
        '    case "$prev" in\n'
        '        -L) mode=list; chain="$a" ;;\n'
        '        -D) mode=del;  chain="$a" ;;\n'
        '    esac\n'
        '    prev="$a"\n'
        "done\n"
        'if [ "$mode" = list ]; then\n'
        '    flag="$STATE/listed-$chain"\n'
        '    if [ ! -f "$flag" ]; then\n'
        '        : > "$flag"\n'
        '        echo "num  target  prot"\n'
        '        echo "1    DNAT     tcp  /* kento:' + hook_fixture.name + ' */"\n'
        "    fi\n"
        "    exit 0\n"
        "fi\n"
        "exit 0\n",
    )

    result = _run_hook(hook_fixture, bin_dir, hook_type="post-stop")
    assert result.returncode == 0, (
        f"post-stop exited {result.returncode}.\nstderr:\n{result.stderr}"
    )

    log = bin_dir / "iptables.log"
    assert log.exists(), "iptables stub never invoked during teardown"
    calls = log.read_text().splitlines()
    del_calls = [ln for ln in calls if " -D " in f" {ln} "]
    assert len(del_calls) == 3, (
        f"expected 3 iptables -D deletes (one per chain), got "
        f"{len(del_calls)}:\n" + "\n".join(calls)
    )

    assert not (cdir / "kento-portfwd-active").exists(), "active marker not removed"
    assert not (cdir / "kento-portfwd-backend").exists(), "backend marker not removed"
