"""Spec-vector suite for the SourceReference family (§3.7).

Exercises each grammar production (valid + violated), the §3.3 endpoint
disambiguation heuristic, tag+digest co-occurrence, the 255-char cap, the
bare-identifier rejection, password masking, normalize (§3.4), and
parse->render round-trip identity. Worked fixtures from §3.6. Cross-checked
against `distribution/reference`'s own conventions.

Spec: ~/workspace/kento-core-api-design.md §3.
"""

import pytest

from kento import (
    Digest,
    Endpoint,
    MalformedReference,
    OciReference,
    SourceReference,
)
from kento.errors import KentoError


# --------------------------------------------------------------------------- #
# §3.6 worked examples — the canonical fixtures.
# --------------------------------------------------------------------------- #

# (input, endpoint_host, port, path, name, version, digest_str)
WORKED = [
    ("droste-hair:latest", None, None, "", "droste-hair", "latest", None),
    ("docker.io/library/debian:12", "docker.io", None, "library", "debian",
     "12", None),
    ("quay.io/podman/hello@sha256:" + "9" * 64, "quay.io", None, "podman",
     "hello", None, "sha256:" + "9" * 64),
    ("reg.example.com:5000/team/proj/app:1.2@sha256:" + "a" * 64,
     "reg.example.com", 5000, "team/proj", "app", "1.2",
     "sha256:" + "a" * 64),
    ("localhost/foo", "localhost", None, "", "foo", None, None),
    ("ubuntu", None, None, "", "ubuntu", None, None),
]


@pytest.mark.parametrize(
    "s,host,port,path,name,version,digest_str", WORKED,
    ids=[w[0] for w in WORKED],
)
def test_worked_examples(s, host, port, path, name, version, digest_str):
    ref = OciReference.parse(s)
    if host is None:
        assert ref.endpoint is None
    else:
        assert ref.endpoint is not None
        assert ref.endpoint.host == host
        assert ref.endpoint.port == port
    assert ref.path == path
    assert ref.name == name
    assert ref.version == version
    if digest_str is None:
        assert ref.digest is None
    else:
        assert ref.digest is not None
        assert ref.digest.render() == digest_str


def test_normalize_worked_example():
    # `ubuntu` after normalize => docker.io / library / ubuntu / latest.
    ref = OciReference.parse("ubuntu").normalize()
    assert ref.endpoint is not None
    assert ref.endpoint.host == "docker.io"
    assert ref.path == "library"
    assert ref.name == "ubuntu"
    assert ref.version == "latest"
    assert ref.digest is None


# --------------------------------------------------------------------------- #
# §3.3 endpoint disambiguation heuristic.
# --------------------------------------------------------------------------- #

def test_disambiguation_foo_bar_is_path_not_endpoint():
    # `foo/bar`: `foo` has no '.'/':'/uppercase and isn't localhost => path.
    ref = OciReference.parse("foo/bar")
    assert ref.endpoint is None
    assert ref.path == "foo"
    assert ref.name == "bar"


def test_disambiguation_dotted_is_endpoint():
    ref = OciReference.parse("foo.com/bar")
    assert ref.endpoint is not None
    assert ref.endpoint.host == "foo.com"
    assert ref.path == ""
    assert ref.name == "bar"


def test_disambiguation_localhost_is_endpoint():
    ref = OciReference.parse("localhost/bar")
    assert ref.endpoint is not None
    assert ref.endpoint.host == "localhost"
    assert ref.path == ""
    assert ref.name == "bar"


def test_disambiguation_uppercase_is_endpoint():
    # `Foo` has an uppercase letter => treated as a domain (endpoint).
    ref = OciReference.parse("Foo/bar")
    assert ref.endpoint is not None
    assert ref.endpoint.host == "Foo"
    assert ref.path == ""
    assert ref.name == "bar"


def test_disambiguation_port_colon_is_endpoint():
    ref = OciReference.parse("myhost:5000/bar")
    assert ref.endpoint is not None
    assert ref.endpoint.host == "myhost"
    assert ref.endpoint.port == 5000
    assert ref.name == "bar"


def test_single_component_no_slash_is_name():
    # No '/', so the lone component is always the name (never an endpoint).
    ref = OciReference.parse("ubuntu")
    assert ref.endpoint is None
    assert ref.path == ""
    assert ref.name == "ubuntu"


# --------------------------------------------------------------------------- #
# §3.2 — faithful parse applies NO defaults.
# --------------------------------------------------------------------------- #

def test_parse_applies_no_defaults():
    ref = OciReference.parse("ubuntu")
    assert ref.endpoint is None          # not docker.io
    assert ref.path == ""                # not library
    assert ref.version is None           # not latest


def test_parse_then_normalize_is_separate_and_pure():
    ref = OciReference.parse("ubuntu")
    norm = ref.normalize()
    # original is unchanged (frozen / pure)
    assert ref.endpoint is None
    assert ref.version is None
    # normalized is a distinct object with defaults applied
    assert norm is not ref
    assert norm.endpoint.host == "docker.io"
    assert norm.version == "latest"


# --------------------------------------------------------------------------- #
# tag + digest co-occurrence.
# --------------------------------------------------------------------------- #

def test_tag_and_digest_co_occur():
    s = "debian:12@sha256:" + "b" * 64
    ref = OciReference.parse(s)
    assert ref.name == "debian"
    assert ref.version == "12"
    assert ref.digest is not None
    assert ref.digest.algorithm == "sha256"
    assert ref.digest.encoded == "b" * 64


def test_digest_only_no_tag():
    s = "debian@sha256:" + "c" * 64
    ref = OciReference.parse(s)
    assert ref.version is None
    assert ref.digest is not None


# --------------------------------------------------------------------------- #
# Digest decomposition (§3.1).
# --------------------------------------------------------------------------- #

def test_digest_decomposition():
    d = Digest.parse("sha256:" + "d" * 64)
    assert d.algorithm == "sha256"
    assert d.encoded == "d" * 64


def test_digest_multi_component_algorithm():
    # grammar permits a multi-component algorithm, e.g. multihash+base58.
    d = Digest.parse("multihash+base58:" + "e" * 40)
    assert d.algorithm == "multihash+base58"
    assert d.encoded == "e" * 40


def test_digest_sha256_must_be_64_hex():
    with pytest.raises(MalformedReference):
        Digest.parse("sha256:" + "f" * 63)
    with pytest.raises(MalformedReference):
        Digest.parse("sha256:" + "f" * 65)


def test_digest_hex_minimum_32():
    # >=32 hex is the floor for an arbitrary algorithm.
    Digest.parse("custom:" + "0" * 32)        # ok
    with pytest.raises(MalformedReference):
        Digest.parse("custom:" + "0" * 31)


def test_digest_rejects_non_hex():
    with pytest.raises(MalformedReference):
        Digest.parse("sha256:" + "g" * 64)


def test_digest_requires_colon():
    with pytest.raises(MalformedReference):
        Digest.parse("sha256" + "a" * 64)


def test_digest_rejects_bad_algorithm():
    with pytest.raises(MalformedReference):
        # algorithm component must start with a letter
        Digest.parse("1bad:" + "a" * 64)


# --------------------------------------------------------------------------- #
# 255-char total cap (§3.1).
# --------------------------------------------------------------------------- #

def test_repo_name_at_cap_ok():
    # exactly 255 chars of remote-name is allowed.
    name = "a" * 255
    ref = OciReference.parse(name)
    assert ref.name == name


def test_repo_name_over_cap_rejected():
    with pytest.raises(MalformedReference):
        OciReference.parse("a" * 256)


def test_repo_name_cap_counts_domain():
    # domain + '/' + remote-name together must be <= 255.
    long_repo = "reg.example.com/" + "a" * 250
    assert len(long_repo) > 255
    with pytest.raises(MalformedReference):
        OciReference.parse(long_repo)


# --------------------------------------------------------------------------- #
# bare-identifier rejection (§3.1 / §3.6).
# --------------------------------------------------------------------------- #

def test_bare_identifier_rejected_as_name():
    # a bare 64-hex [a-f0-9] string is an image ID, NOT a name.
    with pytest.raises(MalformedReference):
        OciReference.parse("a" * 64)


def test_identifier_qualified_by_registry_is_ok():
    # qualified (has an endpoint), the 64-hex string is a legitimate name.
    ref = OciReference.parse("localhost/" + "a" * 64)
    assert ref.name == "a" * 64
    assert ref.endpoint.host == "localhost"


def test_63_hex_is_not_an_identifier():
    # only exactly 64 [a-f0-9] is the identifier short form.
    ref = OciReference.parse("a" * 63)
    assert ref.name == "a" * 63


def test_64_hex_with_uppercase_is_not_identifier():
    # identifier is [a-f0-9] only, so an uppercase variant is not the
    # short-ID form. But a single no-slash component is the NAME, and a
    # path-component is strictly [a-z0-9]+ (§3.1) — so an uppercase letter
    # makes it an invalid name, not a valid one.
    with pytest.raises(MalformedReference):
        OciReference.parse("A" + "a" * 63)


# --------------------------------------------------------------------------- #
# Invalid references — each production violated.
# --------------------------------------------------------------------------- #

INVALID = [
    "",                       # empty reference
    "ubuntu:",               # empty tag
    "@sha256:" + "a" * 64,   # missing name before digest
    "ubuntu@",               # empty digest
    "foo/Path:tag",          # path component must be lowercase [a-z0-9]
    "foo//bar",              # empty path component
    "/leading-slash",        # empty leading component
    "trailing/",             # empty trailing component
    "ubuntu:" + "x" * 129,   # tag too long (>128)
    "ubuntu:ta g",           # space in tag
    "repo:tag@notadigest",   # malformed digest
]


@pytest.mark.parametrize("s", INVALID)
def test_invalid_references_raise(s):
    with pytest.raises(MalformedReference):
        OciReference.parse(s)


def test_parse_non_string_raises():
    with pytest.raises(MalformedReference):
        OciReference.parse(None)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Component validators are total (raise, never None).
# --------------------------------------------------------------------------- #

def test_endpoint_rejects_negative_port():
    with pytest.raises(MalformedReference):
        Endpoint(host="reg.example.com", port=-1)


def test_endpoint_rejects_bad_host():
    with pytest.raises(MalformedReference):
        Endpoint(host="bad_host_under!score", port=None)


def test_endpoint_rejects_ipv4_octet_out_of_range():
    with pytest.raises(MalformedReference):
        Endpoint(host="999.1.1.1", port=None)


def test_endpoint_accepts_ipv4():
    ep = Endpoint(host="10.0.0.1", port=5000)
    assert ep.host == "10.0.0.1"


# --------------------------------------------------------------------------- #
# Password masking (§3.8) — Endpoint userinfo.
# --------------------------------------------------------------------------- #

def test_password_masked_in_render():
    ep = Endpoint(host="reg.example.com", port=None, username="alice",
                  password="s3cr3t")
    rendered = ep.render()
    assert "s3cr3t" not in rendered
    assert "****" in rendered
    assert "alice" in rendered


def test_password_masked_in_str():
    ep = Endpoint(host="reg.example.com", port=None, username="alice",
                  password="s3cr3t")
    assert "s3cr3t" not in str(ep)


def test_password_unmasked_only_when_explicitly_requested():
    ep = Endpoint(host="reg.example.com", port=None, username="alice",
                  password="s3cr3t")
    assert ep.render(mask_password=False) == "alice:s3cr3t@reg.example.com"


def test_username_without_password():
    ep = Endpoint(host="reg.example.com", port=None, username="alice")
    assert ep.render() == "alice@reg.example.com"


# --------------------------------------------------------------------------- #
# render + parse->render round-trip identity.
# --------------------------------------------------------------------------- #

ROUND_TRIP = [
    "droste-hair:latest",
    "docker.io/library/debian:12",
    "quay.io/podman/hello@sha256:" + "9" * 64,
    "reg.example.com:5000/team/proj/app:1.2@sha256:" + "a" * 64,
    "localhost/foo",
    "ubuntu",
    "debian:12@sha256:" + "b" * 64,
    "foo/bar",
    "foo.com/bar",
    "[::1]:5000/team/app:1.0",
]


@pytest.mark.parametrize("s", ROUND_TRIP, ids=ROUND_TRIP)
def test_round_trip_identity(s):
    ref = OciReference.parse(s)
    assert ref.render() == s
    # render is also __str__
    assert str(ref) == s
    # re-parsing the rendered form is stable
    assert OciReference.parse(ref.render()) == ref


# --------------------------------------------------------------------------- #
# IPv6 host.
# --------------------------------------------------------------------------- #

def test_ipv6_host_with_port():
    ref = OciReference.parse("[2001:db8::1]:5000/app")
    assert ref.endpoint is not None
    assert ref.endpoint.host == "[2001:db8::1]"
    assert ref.endpoint.port == 5000
    assert ref.name == "app"


def test_ipv6_host_no_port():
    ref = OciReference.parse("[::1]/app")
    assert ref.endpoint is not None
    assert ref.endpoint.host == "[::1]"
    assert ref.endpoint.port is None


# --------------------------------------------------------------------------- #
# normalize (§3.4) — each Docker-Hub default.
# --------------------------------------------------------------------------- #

def test_normalize_default_domain():
    assert OciReference.parse("foo/bar").normalize().endpoint.host == "docker.io"


def test_normalize_library_prefix():
    norm = OciReference.parse("ubuntu").normalize()
    assert norm.path == "library"


def test_normalize_no_library_prefix_when_namespaced():
    norm = OciReference.parse("myorg/app").normalize()
    assert norm.path == "myorg"          # already namespaced, no library prefix


def test_normalize_default_tag_latest():
    assert OciReference.parse("foo/bar").normalize().version == "latest"


def test_normalize_legacy_index_docker_io():
    norm = OciReference.parse("index.docker.io/library/ubuntu").normalize()
    assert norm.endpoint.host == "docker.io"


def test_parse_rejects_uppercase_path_component():
    # §3.1 path-component is [a-z0-9]+ — strict parse rejects uppercase, so
    # the Docker "lowercase repo" normalize step is unreachable (no-op) and
    # is documented as omitted in normalize().
    with pytest.raises(MalformedReference):
        OciReference.parse("docker.io/Library/Ubuntu")


def test_normalize_preserves_digest():
    norm = OciReference.parse("ubuntu@sha256:" + "a" * 64).normalize()
    assert norm.digest is not None
    assert norm.version == "latest"      # still gets default tag


def test_normalize_idempotent():
    once = OciReference.parse("ubuntu").normalize()
    twice = once.normalize()
    assert once == twice


# --------------------------------------------------------------------------- #
# SourceReference base — scheme dispatch (§3.8).
# --------------------------------------------------------------------------- #

def test_dispatch_schemeless_routes_to_oci():
    ref = SourceReference.parse("ubuntu:latest")
    assert isinstance(ref, OciReference)
    assert ref.scheme == "oci"
    assert ref.name == "ubuntu"


def test_dispatch_oci_scheme_routes_to_oci():
    ref = SourceReference.parse("oci://docker.io/library/debian:12")
    assert isinstance(ref, OciReference)
    assert ref.endpoint.host == "docker.io"
    assert ref.name == "debian"


def test_dispatch_http_routes_to_urlreference():
    # Block B1 flipped this branch: http(s) now parses, no longer raises.
    from kento import UrlReference
    assert isinstance(SourceReference.parse("http://example.com/x"),
                      UrlReference)
    assert isinstance(SourceReference.parse("https://example.com/x"),
                      UrlReference)


def test_dispatch_file_not_yet_implemented():
    with pytest.raises(NotImplementedError):
        SourceReference.parse("file:///path/to/thing")


def test_oci_colon_is_tag_not_scheme():
    # `foo:bar` is a name:tag, NOT a scheme — only `<scheme>://` is a scheme.
    ref = SourceReference.parse("foo:bar")
    assert isinstance(ref, OciReference)
    assert ref.name == "foo"
    assert ref.version == "bar"


# --------------------------------------------------------------------------- #
# pathname (concrete base property, run 30).
# --------------------------------------------------------------------------- #

def test_pathname_with_namespace():
    ref = OciReference.parse("docker.io/library/debian:12")
    assert ref.pathname == "library/debian"


def test_pathname_no_namespace():
    ref = OciReference.parse("localhost/foo")
    assert ref.pathname == "foo"


def test_pathname_bare_name():
    ref = OciReference.parse("ubuntu")
    assert ref.pathname == "ubuntu"


# --------------------------------------------------------------------------- #
# Error hierarchy & inertness.
# --------------------------------------------------------------------------- #

def test_malformed_reference_is_kento_error():
    assert issubclass(MalformedReference, KentoError)


def test_malformed_reference_carries_value_and_production():
    with pytest.raises(MalformedReference) as exc:
        OciReference.parse("ubuntu:")
    assert exc.value.value == "ubuntu:"
    assert exc.value.production is not None


def test_reference_is_frozen():
    ref = OciReference.parse("ubuntu")
    with pytest.raises(Exception):
        ref.name = "changed"  # type: ignore[misc]


def test_references_value_equality():
    assert OciReference.parse("ubuntu:1") == OciReference.parse("ubuntu:1")
    assert OciReference.parse("ubuntu:1") != OciReference.parse("ubuntu:2")


# --------------------------------------------------------------------------- #
# Construction is CLOSED under the grammar (gate C — Finding 1).
# Direct construction / replace() must not yield a ref whose render() parse()
# would reject. __post_init__ enforces the SAME `name`-production checks parse
# does: components, the 255 cap, AND the bare-identifier rejection.
# --------------------------------------------------------------------------- #

def test_construct_rejects_bare_identifier_leaf():
    with pytest.raises(MalformedReference):
        OciReference(endpoint=None, path="", name="a" * 64, version=None,
                     digest=None)


def test_construct_rejects_over_255_cap():
    with pytest.raises(MalformedReference):
        OciReference(endpoint=None, path="", name="x" * 256, version=None,
                     digest=None)


def test_construct_rejects_over_255_cap_counting_domain():
    ep = Endpoint(host="reg.example.com", port=None)
    # domain + '/' + name exceeds 255
    with pytest.raises(MalformedReference):
        OciReference(endpoint=ep, path="", name="a" * 250, version=None,
                     digest=None)


def test_construct_rejects_invalid_path_component():
    with pytest.raises(MalformedReference):
        OciReference(endpoint=None, path="Bad", name="leaf", version=None,
                     digest=None)


def test_construct_rejects_invalid_tag():
    with pytest.raises(MalformedReference):
        OciReference(endpoint=None, path="", name="leaf", version="bad tag",
                     digest=None)


def test_construct_rejects_empty_name():
    with pytest.raises(MalformedReference):
        OciReference(endpoint=None, path="", name="", version=None,
                     digest=None)


def test_construct_allows_qualified_64hex_leaf():
    ep = Endpoint(host="localhost", port=None)
    ref = OciReference(endpoint=ep, path="", name="a" * 64, version=None,
                       digest=None)
    assert ref.name == "a" * 64


def test_replace_cannot_smuggle_invalid_ref():
    from dataclasses import replace
    ref = OciReference.parse("localhost/foo")
    # turning it into an unqualified bare-identifier must raise
    with pytest.raises(MalformedReference):
        replace(ref, endpoint=None, name="a" * 64)


def test_construction_closed_under_grammar_via_round_trip():
    # Any OciReference that constructs must re-parse from its own render().
    refs = [
        OciReference.parse("ubuntu"),
        OciReference.parse("docker.io/library/debian:12"),
        OciReference.parse("localhost/" + "a" * 64),
        OciReference.parse("reg.example.com:5000/team/proj/app:1.2"),
    ]
    for ref in refs:
        assert OciReference.parse(ref.render()) == ref


# =========================================================================== #
# UrlReference — the http(s):// member (§3.8, Block B1).
# =========================================================================== #

from kento import UrlReference  # noqa: E402  (grouped with the block's tests)


# --------------------------------------------------------------------------- #
# parse — valid URLs.
# --------------------------------------------------------------------------- #

def test_url_parse_basic_https_no_version():
    ref = UrlReference.parse("https://host/path/to/rootfs.txz")
    assert isinstance(ref, UrlReference)
    assert ref.scheme == "https"
    assert ref.secure is True
    assert ref.endpoint.host == "host"
    assert ref.endpoint.port is None
    assert ref.path == "path/to"
    assert ref.name == "rootfs.txz"
    assert ref.version is None
    assert ref.pathname == "path/to/rootfs.txz"


def test_url_parse_http_scheme_captured():
    ref = UrlReference.parse("http://host/x")
    assert ref.scheme == "http"
    assert ref.secure is False


def test_url_parse_with_version_suffix():
    ref = UrlReference.parse("https://host/path/rootfs+1.2")
    assert ref.name == "rootfs"
    assert ref.version == "1.2"


def test_url_parse_userinfo_password_held_but_masked_on_render():
    ref = UrlReference.parse("https://u:p@host:8443/x")
    assert ref.endpoint.username == "u"
    assert ref.endpoint.password == "p"        # held faithfully
    assert ref.endpoint.port == 8443
    assert "****" in ref.render()              # masked at render
    assert "p@" not in ref.render()


def test_url_parse_userinfo_username_only():
    ref = UrlReference.parse("https://user@host/x")
    assert ref.endpoint.username == "user"
    assert ref.endpoint.password is None
    assert ref.render() == "https://user@host/x"


def test_url_parse_ipv4_host():
    ref = UrlReference.parse("https://192.168.0.1:9000/img/rootfs")
    assert ref.endpoint.host == "192.168.0.1"
    assert ref.endpoint.port == 9000


def test_url_parse_bracketed_ipv6_host():
    ref = UrlReference.parse("http://[2001:db8::1]:8080/x/kernel")
    assert ref.endpoint.host == "[2001:db8::1]"
    assert ref.endpoint.port == 8080
    # brackets survive a render round-trip
    assert UrlReference.parse(ref.render()).endpoint.host == "[2001:db8::1]"


def test_url_parse_port_present_and_absent():
    assert UrlReference.parse("https://host:8443/x").endpoint.port == 8443
    assert UrlReference.parse("https://host/x").endpoint.port is None


def test_url_parse_no_path_yields_empty_name_and_path():
    ref = UrlReference.parse("https://host")
    assert ref.name == ""
    assert ref.path == ""
    assert ref.endpoint.host == "host"


# --------------------------------------------------------------------------- #
# endpoint ALWAYS present — fail-closed (gate C).
# --------------------------------------------------------------------------- #

def test_url_parse_rejects_missing_host():
    # mutation guard: removing the no-host check in _parse_authority reddens.
    with pytest.raises(MalformedReference):
        UrlReference.parse("https:///path/to/rootfs")


def test_url_construct_rejects_none_endpoint():
    # mutation guard: removing the endpoint-None check in __post_init__ reddens.
    with pytest.raises(MalformedReference):
        UrlReference(endpoint=None, path="", name="rootfs", version=None,
                     secure=True)


def test_url_replace_cannot_drop_endpoint():
    from dataclasses import replace
    ref = UrlReference.parse("https://host/x")
    with pytest.raises(MalformedReference):
        replace(ref, endpoint=None)


# --------------------------------------------------------------------------- #
# version — split on the LAST '+'.
# --------------------------------------------------------------------------- #

def test_url_version_splits_on_last_plus():
    ref = UrlReference.parse("https://host/a+b+1.2")
    assert ref.name == "a+b"
    assert ref.version == "1.2"


def test_url_version_absent_without_plus():
    ref = UrlReference.parse("https://host/rootfs.txz")
    assert ref.version is None


def test_url_trailing_plus_is_malformed():
    # mutation guard: dropping the empty-label check in _split_name_version
    # would record version="" instead of raising.
    with pytest.raises(MalformedReference):
        UrlReference.parse("https://host/rootfs+")


# --------------------------------------------------------------------------- #
# normalize — RFC-3986 canonicalization, PURE.
# --------------------------------------------------------------------------- #

def test_url_normalize_drops_default_https_port():
    ref = UrlReference.parse("https://host:443/x").normalize()
    assert ref.endpoint.port is None


def test_url_normalize_drops_default_http_port():
    ref = UrlReference.parse("http://host:80/x").normalize()
    assert ref.endpoint.port is None


def test_url_normalize_keeps_non_default_port():
    ref = UrlReference.parse("https://host:8443/x").normalize()
    assert ref.endpoint.port == 8443
    # an http URL on :443 is NOT the default for http, so it survives
    ref2 = UrlReference.parse("http://host:443/x").normalize()
    assert ref2.endpoint.port == 443


def test_url_normalize_lowercases_scheme_and_host_not_userinfo():
    ref = UrlReference.parse("https://User:Pass@HOST/x").normalize()
    assert ref.endpoint.host == "host"
    assert ref.scheme == "https"
    # userinfo is preserved verbatim (NOT lowercased)
    assert ref.endpoint.username == "User"
    assert ref.endpoint.password == "Pass"


def test_url_normalize_lowercases_constructed_uppercase_host():
    # mutation guard with BITE: urlsplit already lowercases a *parsed* host, so
    # the host-lowercase step only fires on a constructed/replace'd uppercase
    # host. validate_host permits uppercase, so this path is reachable — and
    # removing the `.lower()` in normalize() must redden THIS test.
    from dataclasses import replace
    ref = UrlReference.parse("https://host/a/rootfs")
    upper = replace(ref, endpoint=Endpoint(host="HOST", port=8443))
    assert upper.endpoint.host == "HOST"           # construction keeps it
    assert upper.normalize().endpoint.host == "host"


def test_url_normalize_uppercases_percent_encoding_hex():
    # RFC-3986 §6.2.2.1: percent-encoding hex is case-insensitive; canonical
    # form is UPPER-case. mutation guard: dropping _normalize_pct_encoding
    # leaves `%2f` verbatim and reddens this.
    ref = UrlReference.parse("https://host/a%2fb/root%5ffs").normalize()
    assert "%2F" in ref.pathname
    assert "%5F" in ref.pathname
    assert "%2f" not in ref.pathname
    # the byte content is NOT decoded (form-only normalization)
    assert "/a%2Fb/" in "/" + ref.pathname


def test_url_normalize_collapses_dot_dotdot_and_double_slash():
    ref = UrlReference.parse("https://host/a//b/../rootfs.txz").normalize()
    assert ref.path == "a"
    assert ref.name == "rootfs.txz"
    assert ref.pathname == "a/rootfs.txz"


def test_url_normalize_is_idempotent():
    ref = UrlReference.parse("HTTPS://Host:443/a/./b/../rootfs+1.0")
    once = ref.normalize()
    assert once.normalize() == once


def test_url_normalize_dotdot_cannot_escape_root():
    # Lexical resolution must not climb above the root.
    ref = UrlReference.parse("https://host/../../rootfs").normalize()
    assert ".." not in ref.pathname
    assert ref.name == "rootfs"


# --------------------------------------------------------------------------- #
# render — round-trips through parse on a normalized ref.
# --------------------------------------------------------------------------- #

def test_url_render_form():
    ref = UrlReference.parse("https://host/path/rootfs+2.0")
    assert ref.render() == "https://host/path/rootfs+2.0"
    assert str(ref) == ref.render()


def test_url_render_round_trips_through_parse_normalized():
    x = UrlReference.parse("https://u:p@host:8443/a/b/rootfs+1.0").normalize()
    y = SourceReference.parse(x.render())
    assert isinstance(y, UrlReference)
    assert y.scheme == x.scheme
    assert y.endpoint.host == x.endpoint.host
    assert y.endpoint.port == x.endpoint.port
    assert y.endpoint.username == x.endpoint.username
    assert y.path == x.path
    assert y.name == x.name
    assert y.version == x.version
    # documented exception: the secret does not round-trip (masked at render).
    assert y.endpoint.password == "****"


# --------------------------------------------------------------------------- #
# dispatch — SourceReference.parse routes by scheme (§3.8, Block B1 flip).
# --------------------------------------------------------------------------- #

def test_dispatch_https_returns_urlreference():
    ref = SourceReference.parse("https://host/path/rootfs.txz")
    assert isinstance(ref, UrlReference)
    assert ref.scheme == "https"


def test_dispatch_http_returns_urlreference():
    assert isinstance(SourceReference.parse("http://host/x"), UrlReference)


def test_dispatch_oci_and_bare_still_oci():
    assert isinstance(SourceReference.parse("oci://ubuntu"), OciReference)
    assert isinstance(SourceReference.parse("ubuntu"), OciReference)


def test_dispatch_file_still_not_implemented():
    with pytest.raises(NotImplementedError):
        SourceReference.parse("file:///x")


def test_dispatch_unknown_scheme_still_malformed():
    with pytest.raises(MalformedReference):
        SourceReference.parse("ftp://host/x")


# --------------------------------------------------------------------------- #
# query/fragment — fail-closed (NOT modelled; §3.8 disclosed scope limit).
# --------------------------------------------------------------------------- #

def test_url_rejects_query_string():
    with pytest.raises(MalformedReference):
        UrlReference.parse("https://host/x?y=1")


def test_url_rejects_fragment():
    with pytest.raises(MalformedReference):
        UrlReference.parse("https://host/x#frag")


def test_url_parse_rejects_empty_string():
    with pytest.raises(MalformedReference):
        UrlReference.parse("")


def test_url_parse_rejects_non_http_scheme_defensively():
    # Calling UrlReference.parse directly with a non-http(s) scheme is rejected
    # (defence in depth — the dispatch should never route it here).
    with pytest.raises(MalformedReference):
        UrlReference.parse("oci://ubuntu")


# --------------------------------------------------------------------------- #
# re-export — kento.UrlReference is the canonical public path.
# --------------------------------------------------------------------------- #

def test_url_reference_is_public_export():
    import kento
    assert "UrlReference" in kento.__all__
    assert kento.UrlReference is UrlReference


def test_url_construction_closed_under_grammar_via_round_trip():
    # Any UrlReference that constructs must re-parse from its own render()
    # (password is the documented exception; compare the masked rendering).
    refs = [
        UrlReference.parse("https://host/path/to/rootfs.txz"),
        UrlReference.parse("http://host:8080/x/kernel+1.0"),
        UrlReference.parse("https://[2001:db8::1]/img/rootfs"),
    ]
    for ref in refs:
        reparsed = UrlReference.parse(ref.render())
        assert reparsed.endpoint.host == ref.endpoint.host
        assert reparsed.path == ref.path
        assert reparsed.name == ref.name
        assert reparsed.version == ref.version
        assert reparsed.scheme == ref.scheme
