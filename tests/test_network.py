"""Spec-vector suite for the network value types (§5).

Exercises the flat ``NetworkConnection`` (§5.1/§5.2), the enums' value-is-wire-
string contract (§5.1/§5.3), the §5.7A port-forward spec grammar (2/3/4-element
disambiguation, ``/proto`` suffix, legacy 2-element=tcp, address-forms-raise,
IPv6 bracket counting, uniqueness collision), parse->render round-trip on the
address-less surface, and the §5.2 CIDR->ip_config decomposition.

These are PURE value types — every test here is inert (no I/O); the live setter
(§5.7C) is out of scope for this block by design.

Spec: ~/workspace/kento-core-api-design.md §5.
"""

import dataclasses

import pytest

from kento import (
    ForwardAddressNotImplemented,
    ForwardProtocol,
    NetworkConnection,
    NetworkMode,
    parse_cidr,
    parse_forward_spec,
    parse_forwards,
    render_forward_spec,
)
from kento.errors import KentoError, ValidationError


# --------------------------------------------------------------------------- #
# Enums — the value IS the rendered wire string (§5.1/§5.3).
# --------------------------------------------------------------------------- #


def test_network_mode_values_are_wire_strings():
    assert NetworkMode.DHCP.value == "dhcp"
    assert NetworkMode.STATIC.value == "static"
    assert NetworkMode.USER.value == "user"
    assert NetworkMode.HOST.value == "host"
    assert NetworkMode.DISABLED.value == "disabled"
    # str, Enum mixin: the member compares/hashes as its wire string, and
    # `.value` is what every render site interpolates. (str(member) is the
    # enum repr "NetworkMode.STATIC" pre-3.12, so the wire string is taken
    # from `.value`, never str(member) — assert the contract that holds.)
    assert NetworkMode.DHCP == "dhcp"
    assert NetworkMode.STATIC.value == "static"
    assert {NetworkMode.HOST: 1}["host"] == 1  # hashes as the wire string


def test_network_mode_membership_is_exactly_the_five():
    assert {m.value for m in NetworkMode} == {
        "dhcp", "static", "user", "host", "disabled",
    }


def test_forward_protocol_values_are_wire_strings():
    assert ForwardProtocol.TCP.value == "tcp"
    assert ForwardProtocol.UDP.value == "udp"
    assert ForwardProtocol.TCP == "tcp"
    # The value is what every render site interpolates (nft/iptables/QEMU/CLI).
    assert f"{ForwardProtocol.UDP.value}" == "udp"


def test_forward_protocol_scope_is_tcp_udp_only():
    # 1.0 floor — QEMU slirp hostfwd supports only tcp/udp (§5.3). SCTP etc.
    # would be additive future values.
    assert {p.value for p in ForwardProtocol} == {"tcp", "udp"}


def test_forward_protocol_not_named_protocol():
    # Named ForwardProtocol, NOT Protocol, to avoid shadowing typing.Protocol
    # (§5.1 comment).
    import kento
    assert not hasattr(kento, "Protocol")
    assert hasattr(kento, "ForwardProtocol")


# --------------------------------------------------------------------------- #
# NetworkConnection — flat, frozen, inert (§5.1/§5.2).
# --------------------------------------------------------------------------- #


def test_network_connection_minimal_construction():
    conn = NetworkConnection(mode=NetworkMode.DHCP)
    assert conn.mode is NetworkMode.DHCP
    assert conn.link_config == {}
    assert conn.ip_config == {}


def test_network_connection_is_frozen():
    conn = NetworkConnection(mode=NetworkMode.STATIC)
    with pytest.raises(dataclasses.FrozenInstanceError):
        conn.mode = NetworkMode.DHCP  # type: ignore[misc]


def test_network_connection_static_carries_ip_config():
    conn = NetworkConnection(
        mode=NetworkMode.STATIC,
        link_config={"bridge": "lxcbr0", "mac": "02:00:00:00:00:01"},
        ip_config={
            "address": "10.0.0.5", "subnet": "24", "gateway": "10.0.0.1",
            "dns1": "1.1.1.1", "dns2": "8.8.8.8",
        },
    )
    assert conn.link_config["bridge"] == "lxcbr0"
    assert conn.ip_config["address"] == "10.0.0.5"
    assert conn.ip_config["subnet"] == "24"


def test_network_connection_default_maps_are_not_shared():
    # field(default_factory=dict): each instance gets its own map, not a shared
    # mutable default — a classic Python footgun (gate C).
    a = NetworkConnection(mode=NetworkMode.DHCP)
    b = NetworkConnection(mode=NetworkMode.DHCP)
    a.link_config["bridge"] = "lxcbr0"
    assert b.link_config == {}


# --------------------------------------------------------------------------- #
# §5.7A port-forward grammar — 2-element (legacy/docker; the only 1.0 form).
# --------------------------------------------------------------------------- #


def test_two_element_legacy_tcp():
    # Today's "8080:80" is a valid 2-element spec reading as tcp -> no migration.
    binding, target = parse_forward_spec("8080:80")
    assert binding == (ForwardProtocol.TCP, None, 8080)
    assert target == (None, 80)


def test_two_element_explicit_tcp_suffix():
    binding, target = parse_forward_spec("8080:80/tcp")
    assert binding == (ForwardProtocol.TCP, None, 8080)
    assert target == (None, 80)


def test_two_element_udp_suffix():
    binding, target = parse_forward_spec("53:53/udp")
    assert binding == (ForwardProtocol.UDP, None, 53)
    assert target == (None, 53)


def test_proto_suffix_is_case_insensitive():
    binding, _ = parse_forward_spec("8080:80/UDP")
    assert binding[0] is ForwardProtocol.UDP


def test_same_port_tcp_and_udp_are_distinct_forwards():
    # Protocol joins the uniqueness key: tcp:8080 and udp:8080 coexist (§5.3).
    fwds = parse_forwards(["8080:80", "8080:80/udp"])
    assert len(fwds) == 2
    assert fwds[(ForwardProtocol.TCP, None, 8080)] == (None, 80)
    assert fwds[(ForwardProtocol.UDP, None, 8080)] == (None, 80)


# --------------------------------------------------------------------------- #
# §5.7A — 3/4-element address forms PARSE but RAISE (honest, not silent-drop).
# --------------------------------------------------------------------------- #


def test_three_element_address_form_raises_not_implemented():
    with pytest.raises(ForwardAddressNotImplemented):
        parse_forward_spec("127.0.0.1:8080:80")


def test_four_element_ssh_form_raises_not_implemented():
    with pytest.raises(ForwardAddressNotImplemented):
        parse_forward_spec("127.0.0.1:8080:10.0.0.5:80")


def test_address_form_with_udp_still_raises():
    with pytest.raises(ForwardAddressNotImplemented):
        parse_forward_spec("127.0.0.1:8080:80/udp")


def test_address_not_implemented_is_kento_error():
    # Subclasses KentoError so a catch-all `except KentoError` in the CLI works.
    assert issubclass(ForwardAddressNotImplemented, KentoError)


def test_address_form_validates_ports_before_raising():
    # A malformed *shape* (non-numeric port) is a ValidationError, not the
    # not-implemented path — the spec parses far enough to know it's well-formed
    # before deciding addresses aren't supported.
    with pytest.raises(ValidationError):
        parse_forward_spec("127.0.0.1:notaport:80")


def test_address_form_empty_host_addr_is_validation_error():
    # ":8080:80" has 3 elements but an empty host address -> shape error, NOT
    # the not-implemented path.
    with pytest.raises(ValidationError):
        parse_forward_spec(":8080:80")


# --------------------------------------------------------------------------- #
# §5.7A — IPv6 bracket form recognized (colons counted outside brackets).
# --------------------------------------------------------------------------- #


def test_ipv6_bracketed_host_counted_as_one_element():
    # "[::1]:8080:80" is the 3-element address form (host=[::1]); its inner
    # colons must NOT inflate the element count to make it look 2-element.
    # It is an address form, so it raises not-implemented (never bites in 1.0
    # since addresses aren't accepted).
    with pytest.raises(ForwardAddressNotImplemented):
        parse_forward_spec("[::1]:8080:80")


def test_ipv6_bracketed_guest_four_element():
    # "[::1]:8080:[fd00::5]:80" — 4 elements, both addresses bracketed IPv6.
    with pytest.raises(ForwardAddressNotImplemented):
        parse_forward_spec("[::1]:8080:[fd00::5]:80")


def test_unbalanced_bracket_is_validation_error():
    with pytest.raises(ValidationError):
        parse_forward_spec("[::1:8080:80")


# --------------------------------------------------------------------------- #
# §5.7A — malformed shapes -> ValidationError (total functions, no sentinel).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("spec", [
    "",                 # empty
    "8080",             # 1 element
    "1:2:3:4:5",        # 5 elements (over the grammar)
    "8080:",            # empty guest port
    ":80",              # empty host port
    "abc:80",           # non-numeric host port
    "8080:xyz",         # non-numeric guest port
    "0:80",             # host port out of range (< 1)
    "70000:80",         # host port out of range (> 65535)
    "8080:0",           # guest port out of range
    "8080:80/",         # empty protocol
    "8080:80/sctp",     # unsupported protocol (1.0 floor)
    "/tcp",             # protocol only, no ports
])
def test_malformed_specs_raise_validation_error(spec):
    with pytest.raises(ValidationError):
        parse_forward_spec(spec)


@pytest.mark.parametrize("spec", [
    "8²:80",       # superscript-2 host: isdigit()==True, int() -> bare ValueError
    "80:8²",       # ...guest side too
    "８０:80",  # fullwidth "80" host: isdigit()==True, would fold to ASCII 80
    "80:８０",  # ...guest side — silent unfaithful round-trip if admitted
])
def test_non_ascii_digit_ports_raise_validation_error(spec):
    # The grammar is ASCII /[0-9]+/ (§5.7A). Non-ASCII numerals must be a typed
    # ValidationError, never a bare ValueError (parse stays total, §2.5) and
    # never a silent fold to ASCII (faithful round-trip, §2.1). Guards against
    # str.isdigit()'s Unicode-numeral admission.
    with pytest.raises(ValidationError):
        parse_forward_spec(spec)


def test_port_boundaries_accepted():
    lo, _ = parse_forward_spec("1:1")
    hi, _ = parse_forward_spec("65535:65535")
    assert lo[2] == 1
    assert hi[2] == 65535


# --------------------------------------------------------------------------- #
# §5.7B — uniqueness enforced at the boundary; duplicate -> clear error.
# --------------------------------------------------------------------------- #


def test_parse_forwards_rejects_duplicate_binding():
    with pytest.raises(ValidationError):
        parse_forwards(["8080:80", "8080:9090"])  # same (tcp, None, 8080)


def test_parse_forwards_duplicate_even_with_identical_target():
    # An exact repeat is still a collision -> explicit error, not silent merge.
    with pytest.raises(ValidationError):
        parse_forwards(["8080:80", "8080:80"])


def test_parse_forwards_empty_list():
    assert parse_forwards([]) == {}


def test_parse_forwards_propagates_address_not_implemented():
    with pytest.raises(ForwardAddressNotImplemented):
        parse_forwards(["8080:80", "127.0.0.1:9090:90"])


# --------------------------------------------------------------------------- #
# render_forward_spec + parse<->render round-trip (the address-less surface).
# --------------------------------------------------------------------------- #


def test_render_tcp_omits_suffix():
    assert render_forward_spec((ForwardProtocol.TCP, None, 8080), (None, 80)) \
        == "8080:80"


def test_render_udp_adds_suffix():
    assert render_forward_spec((ForwardProtocol.UDP, None, 53), (None, 53)) \
        == "53:53/udp"


@pytest.mark.parametrize("spec", ["8080:80", "53:53/udp", "1:65535", "443:443"])
def test_parse_render_round_trip(spec):
    # render(parse(spec)) == canonical spec. "8080:80/tcp" canonicalizes to
    # "8080:80" (the default), so it is tested separately below.
    binding, target = parse_forward_spec(spec)
    assert render_forward_spec(binding, target) == spec


def test_round_trip_canonicalizes_explicit_tcp():
    binding, target = parse_forward_spec("8080:80/tcp")
    assert render_forward_spec(binding, target) == "8080:80"


def test_render_raises_on_address_binding():
    # Address forms don't round-trip in 1.0 (parse raises too) — render is
    # symmetric: it refuses a non-None address rather than emitting a form
    # parse can't read back (gate C).
    with pytest.raises(ForwardAddressNotImplemented):
        render_forward_spec((ForwardProtocol.TCP, "127.0.0.1", 8080), (None, 80))
    with pytest.raises(ForwardAddressNotImplemented):
        render_forward_spec((ForwardProtocol.TCP, None, 8080), ("10.0.0.5", 80))


def test_forwards_map_round_trips_through_render():
    fwds = parse_forwards(["8080:80", "53:53/udp"])
    lines = sorted(render_forward_spec(b, t) for b, t in fwds.items())
    # The kento-port state file is exactly these lines (§5.5/§5.7A).
    assert lines == ["53:53/udp", "8080:80"]


# --------------------------------------------------------------------------- #
# §5.2 CIDR -> ip_config decomposition.
# --------------------------------------------------------------------------- #


def test_parse_cidr_with_prefix():
    assert parse_cidr("10.0.0.5/24") == ("10.0.0.5", "24")


def test_parse_cidr_without_prefix():
    assert parse_cidr("10.0.0.5") == ("10.0.0.5", None)


def test_parse_cidr_prefix_zero_and_max():
    assert parse_cidr("0.0.0.0/0") == ("0.0.0.0", "0")
    assert parse_cidr("10.0.0.5/32") == ("10.0.0.5", "32")


@pytest.mark.parametrize("value", [
    "",                 # empty
    "/24",              # missing address
    "10.0.0.5/",        # empty prefix
    "10.0.0.5/abc",     # non-numeric prefix
    "10.0.0.5/2⁴",      # superscript: isdigit()==True, int() -> bare ValueError
    "10.0.0.5/２４",   # fullwidth "24": isdigit()==True, would fold to ASCII 24
    "10.0.0.5/33",      # prefix out of IPv4 range
    "10.0.0.5/24/8",    # more than one '/'
])
def test_parse_cidr_malformed_raises(value):
    with pytest.raises(ValidationError):
        parse_cidr(value)


# --------------------------------------------------------------------------- #
# Flat public re-export — kento.X, no module stutter.
# --------------------------------------------------------------------------- #


def test_public_surface_is_flat():
    import kento
    for name in (
        "NetworkMode", "ForwardProtocol", "NetworkConnection",
        "HostBinding", "GuestTarget", "ForwardAddressNotImplemented",
        "parse_forward_spec", "render_forward_spec", "parse_forwards",
        "parse_cidr",
    ):
        assert name in kento.__all__, f"{name} missing from kento.__all__"
        assert hasattr(kento, name), f"kento.{name} not importable"
