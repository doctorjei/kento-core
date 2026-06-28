"""Host-side port-forward rule commands — the Python source of the live setter.

The bridged ``forwards`` setter (LXC / pve-lxc) installs the SAME DNAT triplet
the boot hook (``hook.sh``) installs — prerouting DNAT, output DNAT, and a
hairpin masquerade — against the live-resolved guest IP. The spec (§5.7C) chose
**option (i): the Python re-implements the rule commands** rather than factoring
a shared source — matching kento's "Python is the source, the shell hook is the
generated artifact" relationship. A **parity test** pins the two together
(``tests/integration/test_portfwd_parity.py``): for the same forward, the argv
this module builds must be byte-identical to what the generated hook emits.

This module is the SINGLE Python home for those rule commands so the live setter
(``_instances.py``) and the parity test import ONE builder — there is no second
copy to drift. It is pure: ``build_*`` functions return ``list[str]`` argv; the
``run_*`` thin wrappers do the subprocess I/O for the live setter. The comment
tag is ``kento:NAME:proto:hport`` (Block 14) so a single forward's rules are
locatable for live removal, and teardown-by-``kento:NAME:``-prefix still works.

Spec: ``~/workspace/kento-core-api-design.md`` §5.7C (the live setter); §5.7A
(the forward grammar / comment). Backend resolution mirrors ``hook.sh``
(``kento_nat_backend``: nft preferred, iptables fallback, neither => skip).
"""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

from kento.errors import SubprocessError

if TYPE_CHECKING:
    from kento._network import ForwardProtocol

__all__ = [
    "forward_comment",
    "resolve_backend",
    "build_nft_install",
    "build_iptables_install",
    "build_install",
    "run_install",
    "run_remove",
    "build_hostfwd_add",
    "build_hostfwd_remove",
    "vm_hostfwd_add",
    "vm_hostfwd_remove",
]

# nftables table the boot hook installs DNAT rules into (hook.sh: `ip kento`).
_NFT_TABLE = ("ip", "kento")
# The three chains, hook order: prerouting + output DNAT, postrouting hairpin.
_NFT_CHAINS = ("prerouting", "output", "postrouting")
_IPT_CHAINS = ("PREROUTING", "OUTPUT", "POSTROUTING")


def forward_comment(name: str, protocol: "ForwardProtocol", host_port: int) -> str:
    """The per-forward rule comment ``kento:NAME:proto:hport`` (Block 14, §5.7C).

    Identical to ``hook.sh:install_forward_rules`` so the hook's rules and the
    live setter's rules carry the SAME tag — a single forward is locatable for
    live removal, and ``post-stop`` teardown-by-``kento:NAME:``-prefix still
    matches all of an instance's rules (``kento:web`` never matches ``kento:web2``
    because the char after the name is always ``:``).
    """
    return f"kento:{name}:{protocol.value}:{host_port}"


def resolve_backend() -> str | None:
    """Resolve the NAT backend the way ``hook.sh:kento_nat_backend`` does.

    nft preferred (kento's historical backend, isolated ``ip kento`` table),
    else iptables fallback, else ``None`` (neither present -> cannot install
    rules; the caller skips live application with a clear message). ``shutil.which``
    is the Python equivalent of the hook's ``command -v`` probe.
    """
    if shutil.which("nft"):
        return "nft"
    if shutil.which("iptables"):
        return "iptables"
    return None


# --------------------------------------------------------------------------- #
# Rule-command builders — the parity-critical core (§5.7C option (i)).
#
# Each returns the THREE argv lists (one per chain) for ONE forward, in the SAME
# order and with the SAME token sequence the hook emits, so a parity test can
# compare them token-for-token. The nft comment carries LITERAL double quotes
# (the hook writes `comment "\"$comment\""`, so nft receives the token
# `"kento:..."`); iptables takes the bare comment. host_addr/guest_addr are None
# in 1.0 -> bridged binds all interfaces and DNATs to the resolved guest IP.
# --------------------------------------------------------------------------- #


def build_nft_install(
    guest_ip: str,
    protocol: "ForwardProtocol",
    host_port: int,
    guest_port: int,
    name: str,
) -> list[list[str]]:
    """Build the three ``nft add rule`` argv lists for one forward (hook parity).

    Mirrors ``hook.sh:install_forward_rules`` nft branch exactly:
    prerouting/output DNAT to ``guest_ip:guest_port`` on ``proto dport
    host_port``, plus the hairpin masquerade for ``127.0.0.0/8 -> guest_ip``.
    The comment token includes literal quotes to match the hook's
    ``comment "\\"$comment\\""`` rendering.
    """
    proto = protocol.value
    comment = f'"{forward_comment(name, protocol, host_port)}"'
    tbl = list(_NFT_TABLE)
    return [
        ["add", "rule", *tbl, "prerouting", proto, "dport", str(host_port),
         "dnat", "to", f"{guest_ip}:{guest_port}", "comment", comment],
        ["add", "rule", *tbl, "output", proto, "dport", str(host_port),
         "dnat", "to", f"{guest_ip}:{guest_port}", "comment", comment],
        ["add", "rule", *tbl, "postrouting", "ip", "saddr", "127.0.0.0/8",
         "ip", "daddr", guest_ip, proto, "dport", str(guest_port),
         "masquerade", "comment", comment],
    ]


def build_iptables_install(
    guest_ip: str,
    protocol: "ForwardProtocol",
    host_port: int,
    guest_port: int,
    name: str,
) -> list[list[str]]:
    """Build the three ``iptables -A`` argv lists for one forward (hook parity).

    Mirrors ``hook.sh:install_forward_rules`` iptables branch exactly: append to
    the nat-table PREROUTING/OUTPUT (DNAT) and POSTROUTING (MASQUERADE) chains,
    each tagged with the bare ``kento:NAME:proto:hport`` comment.
    """
    proto = protocol.value
    comment = forward_comment(name, protocol, host_port)
    return [
        ["-t", "nat", "-A", "PREROUTING", "-p", proto, "--dport", str(host_port),
         "-j", "DNAT", "--to-destination", f"{guest_ip}:{guest_port}",
         "-m", "comment", "--comment", comment],
        ["-t", "nat", "-A", "OUTPUT", "-p", proto, "--dport", str(host_port),
         "-j", "DNAT", "--to-destination", f"{guest_ip}:{guest_port}",
         "-m", "comment", "--comment", comment],
        ["-t", "nat", "-A", "POSTROUTING", "-s", "127.0.0.0/8", "-d", guest_ip,
         "-p", proto, "--dport", str(guest_port), "-j", "MASQUERADE",
         "-m", "comment", "--comment", comment],
    ]


def build_install(
    backend: str,
    guest_ip: str,
    protocol: "ForwardProtocol",
    host_port: int,
    guest_port: int,
    name: str,
) -> list[list[str]]:
    """Dispatch to the backend-specific install builder (without the binary).

    Returns the THREE argv lists (arguments only — the ``nft``/``iptables``
    executable is prepended by ``run_install``). Used by both the live setter and
    the parity test so there is exactly one rule-command source.
    """
    if backend == "nft":
        return build_nft_install(guest_ip, protocol, host_port, guest_port, name)
    if backend == "iptables":
        return build_iptables_install(
            guest_ip, protocol, host_port, guest_port, name)
    raise ValueError(f"unknown NAT backend: {backend!r}")


# --------------------------------------------------------------------------- #
# Live I/O — thin subprocess wrappers used by the running-instance setter.
# Pure builders above stay test-friendly; these do the actual firewall mutation.
# --------------------------------------------------------------------------- #


def _run(argv: list[str]) -> None:
    """Run one firewall command; raise ``SubprocessError`` on non-zero rc."""
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise SubprocessError(
            f"{argv[0]} failed (rc {result.returncode})"
            + (f": {stderr}" if stderr else ""),
            cmd=argv,
            returncode=result.returncode,
        )


def run_install(
    backend: str,
    guest_ip: str,
    protocol: "ForwardProtocol",
    host_port: int,
    guest_port: int,
    name: str,
) -> None:
    """Install one forward LIVE: run the three DNAT/masquerade rule commands.

    For nft, ensures the ``ip kento`` table + base chains exist first (idempotent,
    mirroring the hook's table/chain bootstrap) so a first live add into a fresh
    table succeeds. Any rule that fails raises ``SubprocessError`` — the caller's
    catch-reverse unwinds the partial set.
    """
    if backend == "nft":
        _ensure_nft_table()
    for argv in build_install(
        backend, guest_ip, protocol, host_port, guest_port, name):
        _run([backend, *argv])


def run_remove(
    backend: str,
    protocol: "ForwardProtocol",
    host_port: int,
    name: str,
) -> None:
    """Remove one forward's rules LIVE, matched by the ``kento:NAME:proto:hport``
    comment (the per-forward tag from Block 14).

    nft has no delete-by-content, so we list each chain, find the handle(s)
    carrying this forward's exact comment, and delete by handle — the same
    mechanism ``hook.sh`` post-stop teardown uses, but matched to the FULL
    per-forward comment (not the ``kento:NAME:`` prefix) so only THIS forward is
    removed. iptables deletes by re-stating the rule with ``-D`` (it has no
    comment-only delete), looping until none remain.
    """
    comment = forward_comment(name, protocol, host_port)
    if backend == "nft":
        _nft_remove_by_comment(comment)
    else:
        _iptables_remove_by_comment(comment)


# --------------------------------------------------------------------------- #
# VM-usermode (slirp) live forwards — QMP human-monitor `hostfwd_add/remove`.
#
# QEMU slirp accepts live forward changes over the monitor. We reach them via
# QMP's human-monitor-command bridge (suspend.qmp_hmp). The HMP grammar mirrors
# QEMU's own hostfwd= option: `<proto>:<haddr>:<hport>-<gaddr>:<gport>`. In 1.0
# host_addr/guest_addr are None, so we use 127.0.0.1 for the host bind and an
# empty guest addr — IDENTICAL to the boot-time fragment vm.py:_read_hostfwds
# emits (`<proto>:127.0.0.1:<hport>-:<gport>`), so live and boot agree.
# netdev id = `net0` (the slirp -netdev user id vm.py assigns).
# --------------------------------------------------------------------------- #

_QMP_NETDEV = "net0"


def build_hostfwd_add(
    protocol: "ForwardProtocol", host_port: int, guest_port: int) -> str:
    """The HMP ``hostfwd_add`` command line for one forward (VM-usermode, §5.7C).

    ``hostfwd_add net0 <proto>:127.0.0.1:<hport>-:<gport>`` — the rule spec part
    matches ``vm.py:_read_hostfwds`` boot fragment exactly (host 127.0.0.1, guest
    addr empty), so a live-added forward is identical to a boot-installed one.
    """
    spec = f"{protocol.value}:127.0.0.1:{host_port}-:{guest_port}"
    return f"hostfwd_add {_QMP_NETDEV} {spec}"


def build_hostfwd_remove(protocol: "ForwardProtocol", host_port: int) -> str:
    """The HMP ``hostfwd_remove`` command line for one forward (VM-usermode).

    ``hostfwd_remove net0 <proto>:127.0.0.1:<hport>`` — the listening-socket
    identity (proto + host addr + host port), matching the add's host side.
    """
    spec = f"{protocol.value}:127.0.0.1:{host_port}"
    return f"hostfwd_remove {_QMP_NETDEV} {spec}"


def vm_hostfwd_add(
    sock_path,
    protocol: "ForwardProtocol",
    host_port: int,
    guest_port: int,
) -> None:
    """Add one forward LIVE on a usermode VM via QMP HMP ``hostfwd_add``.

    A non-empty HMP return (e.g. the host port is already in use) is surfaced as
    ``SubprocessError`` so the caller's catch-reverse unwinds; a socket/protocol
    failure propagates as ``SubprocessError`` too (the caller treats any failure
    uniformly).
    """
    from kento.suspend import qmp_hmp

    cmd = build_hostfwd_add(protocol, host_port, guest_port)
    try:
        out = qmp_hmp(sock_path, cmd)
    except (OSError, ValueError) as exc:
        raise SubprocessError(f"QMP {cmd!r} failed: {exc}") from exc
    if out:
        raise SubprocessError(f"QMP hostfwd_add rejected: {out}")


def vm_hostfwd_remove(
    sock_path, protocol: "ForwardProtocol", host_port: int) -> None:
    """Remove one forward LIVE on a usermode VM via QMP HMP ``hostfwd_remove``.

    Surfaces a non-empty HMP return / transport failure as ``SubprocessError``
    (same uniform treatment as add).
    """
    from kento.suspend import qmp_hmp

    cmd = build_hostfwd_remove(protocol, host_port)
    try:
        out = qmp_hmp(sock_path, cmd)
    except (OSError, ValueError) as exc:
        raise SubprocessError(f"QMP {cmd!r} failed: {exc}") from exc
    if out:
        raise SubprocessError(f"QMP hostfwd_remove rejected: {out}")


def _ensure_nft_table() -> None:
    """Create the ``ip kento`` table + base chains (idempotent), as the hook does.

    Each is allowed to already exist (the hook uses ``|| true``); we swallow a
    non-zero rc the same way so a re-add of an existing table/chain is a no-op
    rather than a spurious failure.
    """
    cmds = [
        ["add", "table", "ip", "kento"],
        ["add", "chain", "ip", "kento", "prerouting",
         "{ type nat hook prerouting priority dstnat; policy accept; }"],
        ["add", "chain", "ip", "kento", "output",
         "{ type nat hook output priority dstnat; policy accept; }"],
        ["add", "chain", "ip", "kento", "postrouting",
         "{ type nat hook postrouting priority srcnat; policy accept; }"],
    ]
    for argv in cmds:
        subprocess.run(
            ["nft", *argv], capture_output=True, text=True)  # idempotent: ignore rc


def _nft_remove_by_comment(comment: str) -> None:
    """Delete every nft rule in the kento chains carrying ``comment``, by handle.

    Mirrors ``hook.sh`` post-stop, but anchored to the FULL comment token
    (``comment "kento:NAME:proto:hport"``) so a single forward is removed without
    touching its siblings. Best-effort per handle (a concurrent delete is fine).
    """
    needle = f'comment "{comment}"'
    for chain in _NFT_CHAINS:
        listing = subprocess.run(
            ["nft", "-a", "list", "chain", "ip", "kento", chain],
            capture_output=True, text=True)
        if listing.returncode != 0:
            continue
        for line in listing.stdout.splitlines():
            if needle not in line:
                continue
            # Handle is the last token: `... # handle 5`.
            tokens = line.split()
            if len(tokens) >= 2 and tokens[-2] == "handle":
                handle = tokens[-1]
                subprocess.run(
                    ["nft", "delete", "rule", "ip", "kento", chain,
                     "handle", handle],
                    capture_output=True, text=True)


def _iptables_remove_by_comment(comment: str) -> None:
    """Delete every iptables nat rule carrying ``comment`` by re-stating it ``-D``.

    iptables cannot delete by comment alone, so we list each chain with
    line-numbers, find rows whose comment matches, and delete the first match by
    number, looping until none remain (line numbers shift after each delete —
    the same delete-first-until-gone loop the hook teardown uses).
    """
    for chain in _IPT_CHAINS:
        while True:
            listing = subprocess.run(
                ["iptables", "-t", "nat", "-L", chain, "--line-numbers", "-n"],
                capture_output=True, text=True)
            if listing.returncode != 0:
                break
            num = None
            for line in listing.stdout.splitlines():
                if f"/* {comment} */" in line or f" {comment} " in f" {line} ":
                    first = line.split(None, 1)[0]
                    if first.isdigit():
                        num = first
                        break
            if num is None:
                break
            deleted = subprocess.run(
                ["iptables", "-t", "nat", "-D", chain, num],
                capture_output=True, text=True)
            if deleted.returncode != 0:
                break
