"""Network value types — ``NetworkConnection`` + the port-forward grammar.

These are **pure, inert, frozen value types** (spec §2 principle 2): no I/O,
ever; their methods are pure transforms (``parse`` / ``render``). The live
``forwards`` setter — nft/iptables DNAT, QMP ``hostfwd_add`` — is runtime I/O on
a handle (§5.7C) and lives nowhere in this module; here we model only the data
and the boundary parsers that turn user/disk strings into structured values and
back.

The public surface (``NetworkMode``, ``ForwardProtocol``, ``NetworkConnection``,
``HostBinding``, ``GuestTarget``) is re-exported flat from ``kento`` — refer to
``kento.NetworkConnection``, not ``kento._network.NetworkConnection``.

Spec: ``~/workspace/kento-core-api-design.md`` §5 (NetworkConnection) — §5.1
(type), §5.2/§5.3 (the L2/L3 config maps + decisions), §5.5 (state mapping), and
§5.7 (multi-forward persistence + the spec grammar, esp. §5.7A). We model the
domain to the relevant standards (ssh ``-L``, docker/podman ``-p``, QEMU
``hostfwd``) completely and faithfully — including parts kento itself never
exercises today, e.g. UDP and the address forms (§2 principle 1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from kento.errors import KentoError, ValidationError

__all__ = [
    "NetworkMode",
    "ForwardProtocol",
    "NetworkConnection",
    "HostBinding",
    "GuestTarget",
    "ForwardAddressNotImplemented",
    "parse_forward_spec",
    "render_forward_spec",
    "parse_forwards",
    "parse_cidr",
]


# --------------------------------------------------------------------------- #
# Enums — the value IS the wire string (§5.1, matching NetworkMode/Status).
# --------------------------------------------------------------------------- #


class NetworkMode(str, Enum):
    """How an instance's NIC attaches to a network (§5.1).

    The value is the literal string used in the ``kento-net-type`` state file
    and at every render site — a flat mode enum, not a sum-type tree (§5.3).
    Which keys of ``link_config``/``ip_config`` apply to each mode, and which
    backends support each mode, are enforced at the create boundary, not by the
    type (§5.6).
    """

    DHCP = "dhcp"          # bridged; DHCP supplies address/gateway/dns/search
    STATIC = "static"      # bridged; address etc. specified by hand (ip_config)
    USER = "user"          # user-mode NAT — QEMU slirp / slirp4netns / pasta
    HOST = "host"          # share the host network namespace
    DISABLED = "disabled"  # no NIC


class ForwardProtocol(str, Enum):
    """Transport protocol of a port forward (§5.1/§5.3).

    The value IS the rendered wire string used at every site — nft
    ``tcp dport``, iptables ``-p tcp``, QEMU ``hostfwd=tcp:``, the
    ``kento-port`` state file, the CLI ``/udp`` suffix. Scope for 1.0 is
    ``tcp``/``udp`` (the universally supportable set — QEMU slirp ``hostfwd``
    accepts only these). SCTP etc. would be additive future enum values, no
    structural change (§5.3).
    """

    TCP = "tcp"
    UDP = "udp"


# --------------------------------------------------------------------------- #
# Type aliases — name the tuple positions so the positional Nones self-document
# (§5.7: "no value class — a plain dict; aliases name the tuple positions").
# --------------------------------------------------------------------------- #

# (protocol, host_addr, host_port). host_addr is None in 1.0 (unspecified bind,
# a forward-compat placeholder — §5.3); it is part of the *key* because the
# listening-socket identity is what makes two forwards distinct.
HostBinding = tuple["ForwardProtocol", "str | None", int]

# (guest_addr, guest_port). guest_addr is None in 1.0 (kento-resolved guest IP);
# it is part of the *value* because it is the forward's target (§5.3).
GuestTarget = tuple["str | None", int]


# --------------------------------------------------------------------------- #
# NetworkConnection — flat: mode + two friendly string->string maps (§5.1).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class NetworkConnection:
    """An instance's network attachment — a flat, inert value (§5.1).

    A ``mode`` enum plus two dev-facing ``str -> str`` maps, deliberately split
    by layer (§5.2):

    * ``link_config`` — L2 (the NIC itself): ``bridge``, ``mac``.
    * ``ip_config`` — L3 (addressing/routing): ``address``, ``subnet``,
      ``gateway``, ``dns1``, ``dns2``. Populated **only for STATIC** — for
      DHCP/USER/HOST/DISABLED it is empty (the lease / slirp / host supplies L3).

    Port-forwarding lives OUTSIDE this type (run 29 M9: a host firewall rule,
    not guest/NIC config) — it is the ``forwards`` field on the base
    ``Instance``, modeled here only as the ``HostBinding``/``GuestTarget``
    aliases and the boundary parsers below.

    The maps default to empty (not shared) so a bare ``NetworkConnection(mode)``
    is a valid, isolated value. Which keys apply to which mode, and backend ×
    mode validity, are enforced at the create boundary, not by this type (§5.3,
    §5.6) — the value stays inert.
    """

    mode: NetworkMode
    link_config: dict[str, str] = field(default_factory=dict)
    ip_config: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Port-forward spec grammar (§5.7A).
#
#   [hostIP:]hostPort:[guestIP:]guestPort[/protocol]
#
# Disambiguated by the count of colons OUTSIDE any [IPv6] brackets:
#   2 elements -> hostPort:guestPort                 (legacy; docker/podman)
#   3 elements -> hostIP:hostPort:guestPort          (docker/podman)
#   4 elements -> hostIP:hostPort:guestIP:guestPort  (ssh -L)
# No "/protocol" suffix => tcp; "/udp" => udp.
#
# The SAME grammar serializes the kento-port state file (one spec per line), so
# today's bare "8080:80" is a valid 2-element tcp spec — no migration branch.
# Address forms (3/4-element) PARSE but RAISE: 1.0 has no per-address bind, and
# silently dropping a user-typed IP would be dishonest (§5.7A, §2 principle 5).
# --------------------------------------------------------------------------- #


class ForwardAddressNotImplemented(KentoError):
    """A port-forward spec carries a host/guest address, unsupported in 1.0.

    A 3- or 4-element spec (``hostIP:hostPort:guestPort`` /
    ``hostIP:hostPort:guestIP:guestPort``) is well-formed and parses, but
    per-address bind is not yet implemented (§5.7A). We RAISE rather than
    silently drop the typed IP so the CLI stays honest — the model slots
    (``host_addr``/``guest_addr``) exist for when this lands post-1.0.
    """


_PORT_MIN = 1
_PORT_MAX = 65535

# ASCII digits only — the §5.7A grammar's ports are /[0-9]+/ and §5.2's prefix
# is a plain number. str.isdigit() is WRONG here: it admits non-ASCII numerals,
# so fullwidth "８０" would silently fold to ASCII 80 (an unfaithful round-trip,
# §2.1) and "8²" passes isdigit() but throws a BARE ValueError at int() (parse
# is then non-total despite §2.5). Same ASCII-anchored approach as Block 01's
# _references._RE_PORT (§07.5 build-on-what-exists). Anchored so a partial
# digit run can't slip through.
_RE_ASCII_DIGITS = re.compile(r"^[0-9]+$")


def _split_outside_brackets(s: str) -> list[str]:
    """Split ``s`` on ``:`` while ignoring colons inside ``[...]`` (§5.7A).

    IPv6 host/guest literals are written bracketed (``[::1]:8080:80``); their
    inner colons must not be counted as element separators. A plain ``host:port``
    or ``8080:80`` has no brackets and splits normally.
    """
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in s:
        if ch == "[":
            depth += 1
            current.append(ch)
        elif ch == "]":
            depth -= 1
            if depth < 0:
                raise ValidationError(
                    f"unbalanced ']' in port-forward spec: {s!r}"
                )
            current.append(ch)
        elif ch == ":" and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if depth != 0:
        raise ValidationError(
            f"unterminated '[' in port-forward spec: {s!r}"
        )
    parts.append("".join(current))
    return parts


def _parse_port(s: str, *, role: str, spec: str) -> int:
    """Parse and range-check a port (1..65535). Total; raises on violation."""
    if not s:
        raise ValidationError(
            f"empty {role} in port-forward spec: {spec!r}"
        )
    if not _RE_ASCII_DIGITS.match(s):
        raise ValidationError(
            f"{role} must be an ASCII number, got {s!r} in port-forward spec: "
            f"{spec!r}"
        )
    port = int(s)
    if not (_PORT_MIN <= port <= _PORT_MAX):
        raise ValidationError(
            f"{role} {port} out of range {_PORT_MIN}-{_PORT_MAX} "
            f"in port-forward spec: {spec!r}"
        )
    return port


def _validate_address(addr: str, *, role: str, spec: str) -> str:
    """Reject an empty address element. Returns the (faithful) address string.

    The address itself is not validated beyond non-emptiness here, because any
    well-formed 3/4-element spec is rejected by the caller anyway
    (:class:`ForwardAddressNotImplemented`). This guard exists so a malformed
    *shape* (e.g. ``:8080:80`` — empty host IP) is a clear ``ValidationError``
    rather than masquerading as the not-yet-implemented path.
    """
    if not addr:
        raise ValidationError(
            f"empty {role} in port-forward spec: {spec!r}"
        )
    return addr


def parse_forward_spec(spec: str) -> tuple[HostBinding, GuestTarget]:
    """Parse one port-forward spec string into ``(HostBinding, GuestTarget)``.

    Grammar (§5.7A)::

        [hostIP:]hostPort:[guestIP:]guestPort[/protocol]

    Element count (colons outside ``[...]``) disambiguates:

    * 2 — ``hostPort:guestPort`` (legacy/docker; addresses ``None``).
    * 3 — ``hostIP:hostPort:guestPort`` — parses, then raises
      :class:`ForwardAddressNotImplemented` (no per-address bind in 1.0).
    * 4 — ``hostIP:hostPort:guestIP:guestPort`` (ssh ``-L``) — same: parses,
      then raises.

    A trailing ``/protocol`` selects ``tcp`` (default) or ``udp``; any other
    protocol is a :class:`ValidationError`. Malformed shape (wrong element
    count, empty/non-numeric port, out-of-range port, unbalanced brackets) is a
    :class:`ValidationError`. Total — never returns a sentinel (§2 principle 5).

    Returns ``((protocol, host_addr, host_port), (guest_addr, guest_port))``;
    in 1.0 the returned binding always has ``host_addr is None`` and
    ``guest_addr is None`` because the only non-raising form is the 2-element one.
    """
    if not isinstance(spec, str) or not spec:
        raise ValidationError(
            f"empty port-forward spec: {spec!r}"
        )

    # Peel the optional "/protocol" suffix first (a '/' never appears inside the
    # address/port grammar, so a single rpartition is unambiguous).
    body = spec
    protocol = ForwardProtocol.TCP
    if "/" in spec:
        body, _, proto_str = spec.rpartition("/")
        if not body:
            raise ValidationError(
                f"missing host/guest ports before '/' in port-forward spec: "
                f"{spec!r}"
            )
        if not proto_str:
            raise ValidationError(
                f"empty protocol after '/' in port-forward spec: {spec!r}"
            )
        try:
            protocol = ForwardProtocol(proto_str.lower())
        except ValueError:
            supported = ", ".join(p.value for p in ForwardProtocol)
            raise ValidationError(
                f"unsupported protocol {proto_str!r} in port-forward spec: "
                f"{spec!r} (supported: {supported})"
            )

    elements = _split_outside_brackets(body)
    n = len(elements)

    if n == 2:
        host_port_s, guest_port_s = elements
        host_port = _parse_port(host_port_s, role="host port", spec=spec)
        guest_port = _parse_port(guest_port_s, role="guest port", spec=spec)
        return (protocol, None, host_port), (None, guest_port)

    if n == 3:
        host_addr_s, host_port_s, guest_port_s = elements
        _validate_address(host_addr_s, role="host address", spec=spec)
        _parse_port(host_port_s, role="host port", spec=spec)
        _parse_port(guest_port_s, role="guest port", spec=spec)
        raise ForwardAddressNotImplemented(
            f"host address binding not yet implemented: {spec!r}. "
            f"Use the 'hostPort:guestPort' form (1.0 binds all interfaces)."
        )

    if n == 4:
        host_addr_s, host_port_s, guest_addr_s, guest_port_s = elements
        _validate_address(host_addr_s, role="host address", spec=spec)
        _parse_port(host_port_s, role="host port", spec=spec)
        _validate_address(guest_addr_s, role="guest address", spec=spec)
        _parse_port(guest_port_s, role="guest port", spec=spec)
        raise ForwardAddressNotImplemented(
            f"host/guest address binding not yet implemented: {spec!r}. "
            f"Use the 'hostPort:guestPort' form (1.0 resolves the guest IP)."
        )

    raise ValidationError(
        f"port-forward spec must have 2-4 colon-separated elements "
        f"([hostIP:]hostPort:[guestIP:]guestPort), got {n} in: {spec!r}"
    )


def render_forward_spec(binding: HostBinding, target: GuestTarget) -> str:
    """Render a ``(HostBinding, GuestTarget)`` back to a spec string (§5.7A).

    In 1.0 only the 2-element form is emitted — addresses are the ``None``
    placeholder, so the output is ``hostPort:guestPort`` for tcp and
    ``hostPort:guestPort/udp`` for udp (no ``/tcp`` suffix, matching the
    default). This is what the ``kento-port`` state file stores, one per line.

    Rendering a binding that carries a non-``None`` address raises
    :class:`ForwardAddressNotImplemented` — the address forms do not round-trip
    in 1.0 (they cannot be parsed back without raising either). ``parse`` and
    ``render`` thus agree exactly on the address-less surface (gate C).
    """
    protocol, host_addr, host_port = binding
    guest_addr, guest_port = target
    if host_addr is not None or guest_addr is not None:
        raise ForwardAddressNotImplemented(
            "rendering host/guest address binding is not yet implemented "
            "(1.0 emits only the address-less 'hostPort:guestPort' form)"
        )
    rendered = f"{host_port}:{guest_port}"
    if protocol is not ForwardProtocol.TCP:
        rendered += f"/{protocol.value}"
    return rendered


def parse_forwards(specs: list[str]) -> dict[HostBinding, GuestTarget]:
    """Parse a list of spec strings into the ``forwards`` map, deduped strictly.

    Each entry goes through :func:`parse_forward_spec`; the result is keyed by
    ``HostBinding`` so the dict structurally enforces uniqueness on
    ``(protocol, host_addr, host_port)`` (§5.3). A *duplicate* binding in the
    input — which would silently clobber an earlier target — is rejected with a
    clear :class:`ValidationError` rather than allowed to win-last (§5.7B
    "uniqueness enforced at the boundary; duplicate -> clear error").

    In 1.0 every accepted binding has ``host_addr is None``, so uniqueness
    reduces to ``(protocol, host_port)`` — consistent with the legacy single
    ``kento-port`` line and today's TCP-only behavior (§5.3).
    """
    forwards: dict[HostBinding, GuestTarget] = {}
    for spec in specs:
        binding, target = parse_forward_spec(spec)
        if binding in forwards:
            protocol, host_addr, host_port = binding
            where = host_port if host_addr is None else f"{host_addr}:{host_port}"
            raise ValidationError(
                f"duplicate port forward for {protocol.value} {where}: "
                f"{spec!r} collides with an earlier entry "
                f"(each (protocol, host address, host port) must be unique)"
            )
        forwards[binding] = target
    return forwards


# --------------------------------------------------------------------------- #
# CIDR -> ip_config decomposition (§5.2).
#
# A "10.0.0.5/24" static-address input is parsed and decomposed at the boundary
# into address + subnet — the same parse-at-the-boundary discipline as
# OciReference. A bare "10.0.0.5" (no prefix) yields no subnet.
# --------------------------------------------------------------------------- #


def parse_cidr(value: str) -> tuple[str, str | None]:
    """Split a static-address input into ``(address, subnet)`` (§5.2).

    ``"10.0.0.5/24"`` -> ``("10.0.0.5", "24")``; a bare ``"10.0.0.5"`` ->
    ``("10.0.0.5", None)``. ``subnet`` is the prefix length as a string (the
    ``ip_config[subnet]`` form). The address part is required and non-empty; the
    prefix, when present, must be a number in the valid IPv4 range (0..32).

    This validates the *shape* (one optional ``/prefix``, numeric prefix in
    range), not that the address is a routable IPv4 — that, like which keys
    apply to which mode, is a create-boundary concern (§5.6). Total; raises
    :class:`ValidationError` on a malformed shape (§2 principle 5).
    """
    if not isinstance(value, str) or not value:
        raise ValidationError(f"empty address input: {value!r}")
    if value.count("/") > 1:
        raise ValidationError(
            f"address input has more than one '/': {value!r}"
        )
    address, sep, prefix = value.partition("/")
    if not address:
        raise ValidationError(
            f"missing address before '/' in: {value!r}"
        )
    if not sep:
        return address, None
    if not prefix:
        raise ValidationError(
            f"empty prefix length after '/' in: {value!r}"
        )
    if not _RE_ASCII_DIGITS.match(prefix):
        raise ValidationError(
            f"prefix length must be an ASCII number, got {prefix!r} in: "
            f"{value!r}"
        )
    if int(prefix) > 32:
        raise ValidationError(
            f"IPv4 prefix length {prefix} out of range 0-32 in: {value!r}"
        )
    return address, prefix
