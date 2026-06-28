"""Tier 1 PARITY: the Python live rule-builder == the generated hook (§5.7C).

The live ``forwards`` setter re-implements the DNAT rule commands in Python
(``kento.portfwd.build_install``) rather than sourcing a shared file — matching
kento's "Python is source, shell hook is the generated artifact" relationship
(§5.7C option (i)). The RISK of two copies is drift: a change to one and not the
other silently breaks live add/remove or the boot install.

This test is the anti-drift guard. For the SAME forward (name, guest IP, proto,
host_port, guest_port), it asserts that the commands the GENERATED HOOK emits at
``start-host`` are byte-identical to what ``kento.portfwd.build_install`` builds,
for BOTH backends (nft, iptables) AND BOTH protocols (tcp, udp). Mechanism
mirrors the existing Tier-1 portfwd tests: a stub ``nft``/``iptables`` on PATH
records its argv; the hook runs against a static IP (synchronous path, no DHCP
worker); the recorded rule lines are compared to the Python builder's argv.

Treat this as load-bearing: if it reddens, the hook and the live setter have
diverged and one of them is wrong.
"""

from __future__ import annotations

import os
import stat
import subprocess

from kento._network import ForwardProtocol
from kento.portfwd import build_install


def _write_executable(path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _logging_stub(bin_dir, name: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    _write_executable(
        bin_dir / name,
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{bin_dir}/{name}.log"\n'
        "exit 0\n",
    )


def _run_hook(hook_fixture, bin_dir):
    env = dict(hook_fixture.env)
    env["LXC_HOOK_TYPE"] = "start-host"
    env["PATH"] = f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"
    result = subprocess.run(
        ["sh", str(hook_fixture.hook_path)],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, (
        f"start-host exited {result.returncode}.\nstderr:\n{result.stderr}"
    )


# The forward set under test: one tcp + one udp, exercised on both backends. The
# guest IP matches the static kento-net the fixture writes.
_GUEST_IP = "10.0.3.42"
_FORWARDS = [
    (ForwardProtocol.TCP, 8080, 80),
    (ForwardProtocol.UDP, 5353, 53),
]


def _python_lines(backend: str, name: str) -> list[str]:
    """The Python builder's rule commands as space-joined argv strings.

    One §5.7C triplet per forward; flattened and ordered as the hook installs
    them (each forward's prerouting/output/postrouting in sequence) so the two
    sequences compare directly.
    """
    lines: list[str] = []
    for proto, hport, gport in _FORWARDS:
        for argv in build_install(backend, _GUEST_IP, proto, hport, gport, name):
            lines.append(" ".join(argv))
    return lines


def _setup_static_forwards(hook_fixture) -> None:
    (hook_fixture.container_dir / "kento-port").write_text(
        "8080:80\n5353:53/udp\n"
    )
    (hook_fixture.container_dir / "kento-net").write_text(
        f"ip={_GUEST_IP}/24\n"
    )


def test_nft_python_matches_hook(hook_fixture, tmp_path):
    """nft: the hook's ``add rule`` lines == build_install(nft) argv, in order."""
    _setup_static_forwards(hook_fixture)
    bin_dir = tmp_path / "bin"
    _logging_stub(bin_dir, "nft")
    _run_hook(hook_fixture, bin_dir)

    log = (bin_dir / "nft.log").read_text().splitlines()
    # The hook also emits table/chain bootstrap ("add table", "add chain ...");
    # the rule commands are the "add rule" lines — exactly what build_install
    # produces (the live setter bootstraps the table separately, _ensure_nft_table).
    hook_rules = [ln for ln in log if ln.startswith("add rule")]
    python_rules = _python_lines("nft", hook_fixture.name)

    assert hook_rules == python_rules, (
        "nft rule drift between hook and Python builder:\n"
        f"hook:\n  " + "\n  ".join(hook_rules) + "\n"
        f"python:\n  " + "\n  ".join(python_rules)
    )


def test_iptables_python_matches_hook(hook_fixture, tmp_path):
    """iptables: the hook's ``-A`` lines == build_install(iptables) argv, in order."""
    _setup_static_forwards(hook_fixture)
    bin_dir = tmp_path / "bin"
    _logging_stub(bin_dir, "iptables")  # nft NOT stubbed -> iptables fallback
    _run_hook(hook_fixture, bin_dir)

    log = (bin_dir / "iptables.log").read_text().splitlines()
    # Every iptables invocation in the install path is a rule append (-A ...);
    # there is no table/chain bootstrap for the iptables backend.
    hook_rules = [ln for ln in log if " -A " in f" {ln} "]
    python_rules = _python_lines("iptables", hook_fixture.name)

    assert hook_rules == python_rules, (
        "iptables rule drift between hook and Python builder:\n"
        f"hook:\n  " + "\n  ".join(hook_rules) + "\n"
        f"python:\n  " + "\n  ".join(python_rules)
    )
