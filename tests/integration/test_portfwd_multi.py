"""Tier 1 coverage for multi-forward install + teardown (Block 14, Phase 5a).

``kento-port`` now holds N forward specs (one §5.7A line each), protocol-aware
(tcp/udp). At ``start-host`` with a static ``kento-net`` IP the hook installs
the DNAT triplet (prerouting + output + hairpin masquerade) for EACH forward,
tagged ``kento:NAME:proto:hport`` so a single forward's rules are locatable.
At ``post-stop`` the teardown removes ALL of an instance's rules by the
``kento:NAME:`` prefix (while ``kento:web`` still never matches ``kento:web2``).

These tests drive the synchronous static-IP path (no DHCP worker needed) with
stubbed ``nft`` / ``iptables`` on PATH, asserting the hook's *output* (the rule
argv it emitted + the active marker) rather than real firewall state.
"""

from __future__ import annotations

import os
import stat
import subprocess


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


def _run(hook_fixture, bin_dir, hook_type):
    env = dict(hook_fixture.env)
    env["LXC_HOOK_TYPE"] = hook_type
    # Isolate PATH to the stub dir + coreutils so the real nft/iptables in sbin
    # are not picked up.
    env["PATH"] = f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"
    return subprocess.run(
        ["sh", str(hook_fixture.hook_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


# --- nft: install N forwards, protocol-aware, per-forward comment ------------


def test_nft_installs_n_forwards_protocol_aware(hook_fixture, tmp_path):
    """Static IP -> 3 nft rules PER forward, correct proto + enriched comment."""
    static_ip = "10.0.3.42"
    (hook_fixture.container_dir / "kento-port").write_text(
        "8080:80\n5353:53/udp\n"
    )
    (hook_fixture.container_dir / "kento-net").write_text(f"ip={static_ip}/24\n")

    bin_dir = tmp_path / "bin"
    _logging_stub(bin_dir, "nft")

    result = _run(hook_fixture, bin_dir, "start-host")
    assert result.returncode == 0, (
        f"start-host exited {result.returncode}.\nstderr:\n{result.stderr}"
    )

    log = (bin_dir / "nft.log").read_text()
    rule_lines = [ln for ln in log.splitlines() if ln.startswith("add rule")]
    name = hook_fixture.name

    # tcp 8080 -> 80: prerouting + output dnat, postrouting masquerade.
    tcp_comment = f'kento:{name}:tcp:8080'
    assert sum(1 for ln in rule_lines if tcp_comment in ln) == 3, log
    assert any(
        f"prerouting tcp dport 8080 dnat to {static_ip}:80" in ln
        and tcp_comment in ln
        for ln in rule_lines
    ), log
    assert any(
        f"postrouting ip saddr 127.0.0.0/8 ip daddr {static_ip} tcp dport 80 "
        "masquerade" in ln and tcp_comment in ln
        for ln in rule_lines
    ), log

    # udp 5353 -> 53: same triplet, udp matchers + udp:5353 comment. Assert the
    # proto on EACH of the three chains (prerouting / output / postrouting) so a
    # single-chain proto regression (e.g. hook.sh output DNAT hardcoded to tcp)
    # reddens the suite — the OUTPUT chain is the gap the Editor flagged.
    udp_comment = f'kento:{name}:udp:5353'
    assert sum(1 for ln in rule_lines if udp_comment in ln) == 3, log
    assert any(
        f"prerouting udp dport 5353 dnat to {static_ip}:53" in ln
        and udp_comment in ln
        for ln in rule_lines
    ), log
    assert any(
        f"output udp dport 5353 dnat to {static_ip}:53" in ln
        and udp_comment in ln
        for ln in rule_lines
    ), log
    assert any(
        f"postrouting ip saddr 127.0.0.0/8 ip daddr {static_ip} udp dport 53 "
        "masquerade" in ln and udp_comment in ln
        for ln in rule_lines
    ), log

    # Active marker records one "hport:gport:ip:proto" line per forward.
    active = hook_fixture.container_dir / "kento-portfwd-active"
    assert active.read_text().splitlines() == [
        f"8080:80:{static_ip}:tcp",
        f"5353:53:{static_ip}:udp",
    ]


def test_iptables_installs_n_forwards_protocol_aware(hook_fixture, tmp_path):
    """Same as above but via the iptables fallback (-p tcp/-p udp + --comment)."""
    static_ip = "10.0.3.50"
    (hook_fixture.container_dir / "kento-port").write_text(
        "8080:80\n5353:53/udp\n"
    )
    (hook_fixture.container_dir / "kento-net").write_text(f"ip={static_ip}/24\n")

    bin_dir = tmp_path / "bin"
    _logging_stub(bin_dir, "iptables")  # nft NOT stubbed -> iptables fallback

    result = _run(hook_fixture, bin_dir, "start-host")
    assert result.returncode == 0, (
        f"start-host exited {result.returncode}.\nstderr:\n{result.stderr}"
    )

    log = (bin_dir / "iptables.log").read_text()
    add_lines = [ln for ln in log.splitlines() if " -A " in f" {ln} "]
    name = hook_fixture.name
    assert sum(1 for ln in add_lines if f"kento:{name}:tcp:8080" in ln) == 3
    assert sum(1 for ln in add_lines if f"kento:{name}:udp:5353" in ln) == 3
    assert any("-p tcp --dport 8080" in ln for ln in add_lines), log
    # Assert udp proto on EACH chain (PREROUTING / OUTPUT / POSTROUTING) so a
    # single-chain proto regression (e.g. the OUTPUT DNAT hardcoded to tcp)
    # reddens the suite — the OUTPUT chain is the gap the Editor flagged.
    assert any("PREROUTING -p udp --dport 5353" in ln for ln in add_lines), log
    assert any("OUTPUT -p udp --dport 5353" in ln for ln in add_lines), log
    assert any(
        "POSTROUTING" in ln and "-p udp --dport 53 " in f"{ln} "
        for ln in add_lines
    ), log

    backend = hook_fixture.container_dir / "kento-portfwd-backend"
    assert backend.read_text().strip() == "iptables"


# --- teardown: remove ALL of an instance's rules by the kento:NAME: prefix ---


def test_post_stop_removes_all_forwards_by_prefix(hook_fixture, tmp_path):
    """post-stop deletes every forward's rules (all share the kento:NAME: prefix)
    while sparing a prefix-colliding sibling (kento:NAME2)."""
    name = hook_fixture.name  # "test-container"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    # nft stub: -a list emits two of THIS instance's forwards (tcp 8080 handle 5,
    # udp 5353 handle 6) plus a prefix-colliding sibling's forward (handle 9).
    nft = bin_dir / "nft"
    nft.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{bin_dir}/nft.log"\n'
        'case "$1" in\n'
        "  -a)\n"
        '    printf "\\ttcp dport 8080 dnat to 10.0.3.1:80 '
        f'comment \\"kento:{name}:tcp:8080\\" # handle 5\\n"\n'
        '    printf "\\tudp dport 5353 dnat to 10.0.3.1:53 '
        f'comment \\"kento:{name}:udp:5353\\" # handle 6\\n"\n'
        '    printf "\\ttcp dport 9090 dnat to 10.0.3.2:90 '
        f'comment \\"kento:{name}2:tcp:9090\\" # handle 9\\n"\n'
        "    ;;\n"
        "esac\n"
        "exit 0\n"
    )
    nft.chmod(nft.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    (hook_fixture.container_dir / "kento-portfwd-active").write_text(
        "8080:80:10.0.3.1:tcp\n5353:53:10.0.3.1:udp\n"
    )

    result = _run(hook_fixture, bin_dir, "post-stop")
    assert result.returncode == 0, (
        f"post-stop exited {result.returncode}.\nstderr:\n{result.stderr}"
    )

    log = (bin_dir / "nft.log").read_text()
    deletes = [ln for ln in log.splitlines() if "delete rule" in ln]
    # This instance's two forwards: handle 5 and 6, each deleted once per chain
    # (3 chains) -> the stub emits all rows for every chain, so the teardown
    # deletes handle 5 and 6 in each of the 3 chains.
    assert any("handle 5" in ln for ln in deletes), deletes
    assert any("handle 6" in ln for ln in deletes), deletes
    # The prefix-colliding sibling (kento:NAME2, handle 9) must NEVER be deleted.
    assert not any("handle 9" in ln for ln in deletes), (
        "teardown deleted a prefix-colliding sibling's rule (handle 9); the "
        "kento:NAME: match is not boundary-anchored.\n" + "\n".join(deletes)
    )

    assert not (hook_fixture.container_dir / "kento-portfwd-active").exists()
