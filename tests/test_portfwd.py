"""Unit tests for ``kento.portfwd`` — the live port-forward rule commands.

Covers the pure rule-command builders (the parity-critical core — argv shape per
backend/proto), the QMP HMP grammar, backend resolution, and the thin live I/O
wrappers (mocked subprocess). The hook-vs-Python byte-parity is asserted
separately in ``tests/integration/test_portfwd_parity.py`` (Tier-1, runs the
real hook); here we pin the argv shape and the error/rollback surfaces.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kento import portfwd
from kento._network import ForwardProtocol
from kento.errors import SubprocessError


# --- comment + backend resolution -------------------------------------------


def test_forward_comment_format():
    assert portfwd.forward_comment("web", ForwardProtocol.TCP, 8080) == \
        "kento:web:tcp:8080"
    assert portfwd.forward_comment("db", ForwardProtocol.UDP, 5353) == \
        "kento:db:udp:5353"


def test_resolve_backend_prefers_nft():
    with patch("kento.portfwd.shutil.which",
               side_effect=lambda b: "/usr/sbin/nft" if b == "nft" else None):
        assert portfwd.resolve_backend() == "nft"


def test_resolve_backend_iptables_fallback():
    def which(b):
        return "/sbin/iptables" if b == "iptables" else None
    with patch("kento.portfwd.shutil.which", side_effect=which):
        assert portfwd.resolve_backend() == "iptables"


def test_resolve_backend_none():
    with patch("kento.portfwd.shutil.which", return_value=None):
        assert portfwd.resolve_backend() is None


# --- nft / iptables install builders (argv shape) ---------------------------


def test_build_nft_install_tcp():
    rules = portfwd.build_nft_install(
        "10.0.3.42", ForwardProtocol.TCP, 8080, 80, "web")
    assert len(rules) == 3
    pre, out, post = (" ".join(r) for r in rules)
    assert pre == (
        'add rule ip kento prerouting tcp dport 8080 dnat to '
        '10.0.3.42:80 comment "kento:web:tcp:8080"')
    assert out == (
        'add rule ip kento output tcp dport 8080 dnat to '
        '10.0.3.42:80 comment "kento:web:tcp:8080"')
    assert post == (
        'add rule ip kento postrouting ip saddr 127.0.0.0/8 ip daddr '
        '10.0.3.42 tcp dport 80 masquerade comment "kento:web:tcp:8080"')


def test_build_nft_install_udp_proto_on_every_chain():
    rules = portfwd.build_nft_install(
        "10.0.3.42", ForwardProtocol.UDP, 5353, 53, "dns")
    lines = [" ".join(r) for r in rules]
    # udp on the DNAT chains AND the masquerade chain (no tcp hardcode anywhere).
    assert any("prerouting udp dport 5353" in ln for ln in lines)
    assert any("output udp dport 5353" in ln for ln in lines)
    assert any("udp dport 53 masquerade" in ln for ln in lines)
    assert not any("tcp" in ln for ln in lines)


def test_build_iptables_install_tcp():
    rules = portfwd.build_iptables_install(
        "10.0.3.50", ForwardProtocol.TCP, 8080, 80, "web")
    pre, out, post = (" ".join(r) for r in rules)
    assert pre == (
        "-t nat -A PREROUTING -p tcp --dport 8080 -j DNAT "
        "--to-destination 10.0.3.50:80 -m comment --comment kento:web:tcp:8080")
    assert post == (
        "-t nat -A POSTROUTING -s 127.0.0.0/8 -d 10.0.3.50 -p tcp "
        "--dport 80 -j MASQUERADE -m comment --comment kento:web:tcp:8080")


def test_build_install_dispatch_and_unknown():
    assert portfwd.build_install(
        "nft", "1.2.3.4", ForwardProtocol.TCP, 1, 2, "n") == \
        portfwd.build_nft_install("1.2.3.4", ForwardProtocol.TCP, 1, 2, "n")
    assert portfwd.build_install(
        "iptables", "1.2.3.4", ForwardProtocol.UDP, 1, 2, "n") == \
        portfwd.build_iptables_install("1.2.3.4", ForwardProtocol.UDP, 1, 2, "n")
    with pytest.raises(ValueError):
        portfwd.build_install("ipchains", "1.2.3.4", ForwardProtocol.TCP, 1, 2, "n")


# --- QMP hostfwd grammar (mirrors vm.py boot fragment) ----------------------


def test_build_hostfwd_add_matches_boot_fragment():
    # Boot uses `<proto>:127.0.0.1:<hport>-:<gport>`; the live add must match so a
    # live-added forward is identical to a boot-installed one.
    assert portfwd.build_hostfwd_add(ForwardProtocol.TCP, 8080, 80) == \
        "hostfwd_add net0 tcp:127.0.0.1:8080-:80"
    assert portfwd.build_hostfwd_add(ForwardProtocol.UDP, 5353, 53) == \
        "hostfwd_add net0 udp:127.0.0.1:5353-:53"


def test_build_hostfwd_remove():
    assert portfwd.build_hostfwd_remove(ForwardProtocol.TCP, 8080) == \
        "hostfwd_remove net0 tcp:127.0.0.1:8080"


# --- live I/O wrappers (mocked subprocess) ----------------------------------


def test_run_install_nft_bootstraps_table_then_rules():
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        class R:
            returncode = 0
            stderr = ""
        return R()

    with patch("kento.portfwd.subprocess.run", side_effect=fake_run):
        portfwd.run_install(
            "nft", "10.0.3.42", ForwardProtocol.TCP, 8080, 80, "web")
    # First the 4 table/chain bootstrap commands, then 3 rule installs.
    assert calls[0] == ["nft", "add", "table", "ip", "kento"]
    rule_calls = [c for c in calls if c[:3] == ["nft", "add", "rule"]]
    assert len(rule_calls) == 3


def test_run_install_raises_on_failure():
    class R:
        returncode = 1
        stderr = "boom"

    with patch("kento.portfwd.subprocess.run", return_value=R()):
        with pytest.raises(SubprocessError):
            portfwd.run_install(
                "iptables", "10.0.3.42", ForwardProtocol.TCP, 8080, 80, "web")


def test_run_remove_nft_deletes_matching_handle():
    name = "web"
    listing = (
        '\ttcp dport 8080 dnat to 10.0.3.42:80 '
        f'comment "kento:{name}:tcp:8080" # handle 5\n'
        '\tudp dport 5353 dnat to 10.0.3.42:53 '
        f'comment "kento:{name}:udp:5353" # handle 6\n'
    )
    deleted = []

    def fake_run(argv, **kw):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        r = R()
        if argv[:3] == ["nft", "-a", "list"]:
            r.stdout = listing
        elif "delete" in argv:
            deleted.append(argv)
        return r

    with patch("kento.portfwd.subprocess.run", side_effect=fake_run):
        portfwd.run_remove("nft", ForwardProtocol.TCP, 8080, name)
    # Only the tcp:8080 forward's handle (5) is deleted, never udp:5353 (6).
    assert all("5" in c for c in (a[-1] for a in deleted))
    assert all("6" not in a[-1] for a in deleted)
    assert deleted, "expected at least one delete by handle"


def test_vm_hostfwd_add_surfaces_failure(tmp_path):
    sock = tmp_path / "qmp.sock"
    with patch("kento.suspend.qmp_hmp", return_value="port in use"):
        with pytest.raises(SubprocessError):
            portfwd.vm_hostfwd_add(sock, ForwardProtocol.TCP, 8080, 80)


def test_vm_hostfwd_add_success(tmp_path):
    sock = tmp_path / "qmp.sock"
    with patch("kento.suspend.qmp_hmp", return_value="") as q:
        portfwd.vm_hostfwd_add(sock, ForwardProtocol.UDP, 5353, 53)
    q.assert_called_once_with(sock, "hostfwd_add net0 udp:127.0.0.1:5353-:53")
