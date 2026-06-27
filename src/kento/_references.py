"""Boot-source locator value types — the ``SourceReference`` family.

These are **pure, inert, frozen value types** (spec §2 principle 2): no I/O,
ever; their methods are pure transforms (``parse`` / ``render`` / ``normalize``).
Once constructed, a value is plain data you can pass, copy, and reason about.

The public surface (``SourceReference``, ``OciReference``, ``Endpoint``,
``Digest``, ``MalformedReference``) is re-exported flat from ``kento`` — refer to
``kento.OciReference``, not ``kento._references.OciReference``.

Spec: ``~/workspace/kento-core-api-design.md`` §3 (OCI reference) + §3.8
(the ``SourceReference`` family). We model to the canonical grammar
(``distribution/reference``, §3.1) completely and faithfully — including the
parts kento itself never exercises (§2 principle 1).
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace

from kento.errors import KentoError

__all__ = [
    "MalformedReference",
    "Endpoint",
    "Digest",
    "SourceReference",
    "OciReference",
]


class MalformedReference(KentoError):
    """A source-reference string violates the grammar.

    Carries the offending input and a short description of which production
    failed, so a caller (or the CLI) can render a precise diagnostic. Raised
    by every parse/validation path in this module — no operation returns
    ``""``/``None`` to signal failure (§2 principle 5, §3.5).
    """

    def __init__(self, message: str, *, value: str, production: str | None = None):
        self.value = value
        self.production = production
        if production:
            full = f"malformed reference ({production}): {message}: {value!r}"
        else:
            full = f"malformed reference: {message}: {value!r}"
        super().__init__(full)


# --------------------------------------------------------------------------- #
# Grammar fragments — verbatim from §3.1 (distribution/reference).
#
# These are the SOLE source of truth for what a reference *is*; we model to
# them, not to kento usage. Anchored (^...$) variants are compiled where a
# whole-component match is required.
# --------------------------------------------------------------------------- #

# domain-component := /([a-zA-Z0-9]|[a-zA-Z0-9][a-zA-Z0-9-]*[a-zA-Z0-9])/
_DOMAIN_COMPONENT = r"(?:[a-zA-Z0-9]|[a-zA-Z0-9][a-zA-Z0-9-]*[a-zA-Z0-9])"
# domain-name := domain-component ['.' domain-component]*
_DOMAIN_NAME = rf"{_DOMAIN_COMPONENT}(?:\.{_DOMAIN_COMPONENT})*"
# IPv4address — the dotted-quad form (each octet 0-255 is enforced separately).
_IPV4 = r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}"
# host := domain-name | IPv4address | [ IPv6address ]
# IPv6 is matched permissively here (hex groups + colons inside brackets) and
# is accepted as written — faithful capture, not RFC-grade validation.
_IPV6_BRACKETED = r"\[[0-9A-Fa-f:.]+\]"
_HOST = rf"(?:{_IPV6_BRACKETED}|{_DOMAIN_NAME}|{_IPV4})"
# port-number := /[0-9]+/
_PORT = r"[0-9]+"

# alpha-numeric := /[a-z0-9]+/
_ALPHA_NUMERIC = r"[a-z0-9]+"
# separator := /[_.]|__|[-]*/
#   one of: a single '_' or '.', a literal '__', or a run of '-' (incl. empty).
# Used BETWEEN alpha-numeric runs inside one path-component.
_SEPARATOR = r"(?:[_.]|__|[-]+)"
# path-component := alpha-numeric [separator alpha-numeric]*
_PATH_COMPONENT = rf"{_ALPHA_NUMERIC}(?:{_SEPARATOR}{_ALPHA_NUMERIC})*"

# tag := /[\w][\w.-]{0,127}/
_TAG = r"[\w][\w.-]{0,127}"

# digest-algorithm-component := /[A-Za-z][A-Za-z0-9]*/
_DIGEST_ALGO_COMPONENT = r"[A-Za-z][A-Za-z0-9]*"
# digest-algorithm-separator := /[+.-_]/
# NOTE (literal-vs-intent divergence): the §3.1 text literally reads `[+.-_]`,
# which as a regex is a RANGE `.`-`_` (matching `/`, `:`, digits, A-Z, etc.) —
# an upstream typo. distribution/reference *means* the four literal separators
# `+ . - _`, so we implement the intent (`[+._-]`, a `-` placed last is a
# literal). Do NOT "fix" this back to `[+.-_]`. (Same spirit as the documented
# normalize-lowercase no-op below.)
_DIGEST_ALGO_SEP = r"[+._-]"
# digest-algorithm := component [ separator component ]*
_DIGEST_ALGO = rf"{_DIGEST_ALGO_COMPONENT}(?:{_DIGEST_ALGO_SEP}{_DIGEST_ALGO_COMPONENT})*"
# digest-hex := /[0-9a-fA-F]{32,}/   (>=128-bit digest value)
_DIGEST_HEX = r"[0-9a-fA-F]{32,}"

# identifier := /[a-f0-9]{64}/  (bare image-ID short form — NOT a name)
_IDENTIFIER = re.compile(r"^[a-f0-9]{64}$")

# Compiled anchored matchers for component validators.
_RE_HOST = re.compile(rf"^{_HOST}$")
_RE_PORT = re.compile(rf"^{_PORT}$")
_RE_PATH_COMPONENT = re.compile(rf"^{_PATH_COMPONENT}$")
_RE_TAG = re.compile(rf"^{_TAG}$", re.ASCII)
_RE_DIGEST_ALGO = re.compile(rf"^{_DIGEST_ALGO}$")
_RE_DIGEST_HEX = re.compile(rf"^{_DIGEST_HEX}$")

# §3.1 constraint: RepositoryNameTotalLengthMax = 255 (total domain/remote-name).
_REPO_NAME_TOTAL_MAX = 255

# Per-algorithm fixed encoded lengths (in hex chars). sha256 => exactly 64.
_DIGEST_FIXED_LEN = {
    "sha256": 64,
    "sha384": 96,
    "sha512": 128,
}

_MASK = "****"


# --------------------------------------------------------------------------- #
# Endpoint — the authority: host[:port] + optional userinfo.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Endpoint:
    """The authority of a reference — ``host[:port]`` plus optional userinfo.

    ``username``/``password`` model http(s) ``[user[:pass]@]host`` userinfo;
    they are always ``None`` for oci/file. The password is *parsed faithfully*
    but **masked** in ``render``/``__str__``/logs (RFC-3986 deprecates
    passwords-in-URL; real bytes go only to a fetcher, never to a log).
    """

    host: str
    port: int | None
    username: str | None = None
    password: str | None = None

    def __post_init__(self) -> None:
        validate_host(self.host)
        if self.port is not None and self.port < 0:
            raise MalformedReference(
                "port must be non-negative", value=str(self.port),
                production="port-number",
            )

    def render(self, *, mask_password: bool = True) -> str:
        """Render the authority. The password is masked by default.

        ``mask_password=False`` emits the real password — reserved for the one
        caller that actually contacts the registry/host (never for logs).
        """
        userinfo = ""
        if self.username is not None:
            if self.password is not None:
                pw = _MASK if mask_password else self.password
                userinfo = f"{self.username}:{pw}@"
            else:
                userinfo = f"{self.username}@"
        hostport = self.host if self.port is None else f"{self.host}:{self.port}"
        return f"{userinfo}{hostport}"

    def __str__(self) -> str:
        return self.render()


# --------------------------------------------------------------------------- #
# Digest — content identity, decomposed.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Digest:
    """A content digest — ``algorithm:encoded``, decomposed (§3.1/§3.2).

    The grammar permits a multi-component algorithm (e.g.
    ``multihash+base58``). ``encoded`` must be >=32 hex chars; for a known
    fixed-length algorithm (sha256 => 64) the length is checked exactly.
    """

    algorithm: str
    encoded: str

    def __post_init__(self) -> None:
        validate_digest(self.algorithm, self.encoded)

    @staticmethod
    def parse(s: str) -> "Digest":
        """Parse ``algorithm:encoded`` into a ``Digest``. Raises on violation."""
        if ":" not in s:
            raise MalformedReference(
                "digest must be 'algorithm:encoded'", value=s, production="digest",
            )
        algorithm, _, encoded = s.partition(":")
        return Digest(algorithm=algorithm, encoded=encoded)

    def render(self) -> str:
        return f"{self.algorithm}:{self.encoded}"

    def __str__(self) -> str:
        return self.render()


# --------------------------------------------------------------------------- #
# Component validators — all total; raise MalformedReference, never return None.
# --------------------------------------------------------------------------- #


def validate_host(host: str) -> None:
    """Validate a ``host`` production (domain-name | IPv4 | [IPv6])."""
    if not host:
        raise MalformedReference("empty host", value=host, production="host")
    if not _RE_HOST.match(host):
        raise MalformedReference("not a valid host", value=host, production="host")
    # An IPv4 dotted-quad must have octets in 0..255 (regex only bounds digits).
    if re.fullmatch(_IPV4, host):
        if any(int(octet) > 255 for octet in host.split(".")):
            raise MalformedReference(
                "IPv4 octet out of range", value=host, production="host",
            )


def validate_port(port: str) -> None:
    """Validate a ``port-number`` production (one or more digits)."""
    if not _RE_PORT.match(port):
        raise MalformedReference(
            "port must be digits", value=port, production="port-number",
        )


def validate_path_component(component: str) -> None:
    """Validate one ``path-component`` production."""
    if not component:
        raise MalformedReference(
            "empty path component", value=component, production="path-component",
        )
    if not _RE_PATH_COMPONENT.match(component):
        raise MalformedReference(
            "not a valid path component", value=component,
            production="path-component",
        )


def validate_tag(tag: str) -> None:
    """Validate a ``tag`` production (/[\\w][\\w.-]{0,127}/, ASCII word chars)."""
    if not _RE_TAG.match(tag):
        raise MalformedReference("not a valid tag", value=tag, production="tag")


def validate_digest(algorithm: str, encoded: str) -> None:
    """Validate a decomposed digest (algorithm + encoded). Total."""
    if not _RE_DIGEST_ALGO.match(algorithm):
        raise MalformedReference(
            "not a valid digest algorithm", value=algorithm,
            production="digest-algorithm",
        )
    if not _RE_DIGEST_HEX.match(encoded):
        raise MalformedReference(
            "digest hex must be >=32 hex chars", value=encoded,
            production="digest-hex",
        )
    fixed = _DIGEST_FIXED_LEN.get(algorithm.lower())
    if fixed is not None and len(encoded) != fixed:
        raise MalformedReference(
            f"{algorithm} digest must be exactly {fixed} hex chars, "
            f"got {len(encoded)}",
            value=encoded, production="digest-hex",
        )


# --------------------------------------------------------------------------- #
# SourceReference — the scheme-discriminated base (§3.8).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SourceReference(ABC):
    """A parsed, scheme-discriminated boot-source locator (§3.8).

    The shared structural skeleton across every scheme — ``endpoint`` /
    ``path`` / ``name`` / ``version`` (*where* the bytes are + which variant).
    A pure value: no I/O, no role (role is inferred at composition, not
    stored). Concrete members live in subclasses keyed by ``scheme``.
    """

    endpoint: Endpoint | None
    path: str
    name: str
    version: str | None

    # ----- scheme discriminator -----
    @property
    @abstractmethod
    def scheme(self) -> str:
        """``"oci"`` | ``"http"`` | ``"https"`` | ``"file"`` — the discriminator."""

    # ----- parse: dispatch by scheme (§3.8) -----
    @staticmethod
    def parse(s: str) -> "SourceReference":
        """Parse a locator string, dispatching by scheme (§3.8).

        Scheme-less or ``oci://`` routes to :meth:`OciReference.parse`
        (a bare ``droste-hair:latest`` is equivalent to ``oci://…``,
        back-compat). ``http(s)://`` / ``file://`` are FUTURE blocks — they
        raise a clear "not yet implemented" error rather than being silently
        dropped.
        """
        scheme, rest = _split_scheme(s)
        if scheme is None or scheme == "oci":
            return OciReference.parse(rest if scheme else s)
        if scheme in ("http", "https"):
            raise NotImplementedError(
                f"{scheme}:// source references (UrlReference) are not yet "
                f"implemented: {s!r}"
            )
        if scheme == "file":
            raise NotImplementedError(
                f"file:// source references (FileReference) are not yet "
                f"implemented: {s!r}"
            )
        raise MalformedReference(
            f"unknown source-reference scheme {scheme!r}", value=s,
            production="scheme",
        )

    # ----- render / normalize: per-subclass -----
    @abstractmethod
    def render(self) -> str:
        """Per-subclass canonical string form (also ``__str__``; pw masked)."""

    @abstractmethod
    def normalize(self) -> "SourceReference":
        """Per-subclass PURE canonicalization (run 30 — on the base)."""

    # ----- pathname: CONCRETE base property (run 30) -----
    @property
    def pathname(self) -> str:
        """``path`` + ``name`` joined ("library/ubuntu") — a uniform join.

        For oci this is the registry "repository"; for url/file it is the
        URL/file path. Concrete on the base (no per-subclass override) since
        the behavior is not OCI-specific.
        """
        if self.path:
            return f"{self.path}/{self.name}"
        return self.name

    def __str__(self) -> str:
        return self.render()


def _split_scheme(s: str) -> tuple[str | None, str]:
    """Split a leading ``scheme://`` off a locator string.

    Returns ``(scheme, remainder)`` or ``(None, s)`` when there is no scheme.
    Only a genuine ``<scheme>://`` prefix counts — a bare ``foo:bar`` (an oci
    tag) is NOT a scheme. The scheme matches RFC-3986 (alpha then
    alnum/+/-/.).
    """
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.\-]*)://", s)
    if m:
        return m.group(1).lower(), s[m.end():]
    return None, s


# --------------------------------------------------------------------------- #
# OciReference — the oci:// member (§3.2/§3.3/§3.4/§3.8).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OciReference(SourceReference):
    """The ``oci://`` member of the ``SourceReference`` family.

    Inherits ``endpoint``/``path``/``name``/``version`` and adds ``digest``
    (the content pin — OCI-only, §3.8). ``version`` is the mutable label the
    §3.1 grammar names ``tag``; ``digest`` and ``version`` may co-occur
    (``name:version@digest`` is legal, §3.2).
    """

    digest: Digest | None

    def __post_init__(self) -> None:
        # A constructed OciReference is always valid — direct construction is
        # not a back door around the grammar (§2 principle 5; gate (C)). parse
        # is the usual entry point, but `replace(...)`/manual construction
        # (e.g. in normalize) must yield ONLY references whose render() parse()
        # would accept again. So __post_init__ enforces the SAME `name`
        # production checks parse does — components, the 255 total-length cap,
        # AND the bare-identifier rejection — via one shared validator.
        _validate_name_production(
            self.endpoint, self.path, self.name,
            original=_render_name_production(self.endpoint, self.path, self.name),
        )
        if self.version is not None:
            validate_tag(self.version)
        # endpoint/digest validate themselves in their own __post_init__.

    @property
    def scheme(self) -> str:
        return "oci"

    # ------------------------------------------------------------------- #
    # Parse — faithful, no defaults, with the disambiguation heuristic.
    # ------------------------------------------------------------------- #
    @staticmethod
    def parse(s: str) -> "OciReference":
        """Parse an OCI reference faithfully (§3.3). Total; raises on violation.

        Applies NO defaults — no ``docker.io``, no ``library/``, no
        ``latest`` — recording exactly what was written. Normalization is a
        separate, explicit step (:meth:`normalize`).
        """
        if not isinstance(s, str) or not s:
            raise MalformedReference(
                "empty reference", value=str(s), production="reference",
            )

        # reference := name [ ":" tag ] [ "@" digest ]
        # Peel the digest first (last '@'), then the tag, then parse `name`.
        rest = s

        digest: Digest | None = None
        if "@" in rest:
            rest, _, digest_str = rest.rpartition("@")
            if not digest_str:
                raise MalformedReference(
                    "empty digest after '@'", value=s, production="digest",
                )
            if not rest:
                raise MalformedReference(
                    "missing name before '@'", value=s, production="reference",
                )
            digest = Digest.parse(digest_str)

        # tag := after the LAST ':' that is part of `name` (not a port colon).
        # A ':' belongs to the tag only when it appears AFTER the final '/'
        # (a port colon lives inside the leading domain, before the first '/').
        version: str | None = None
        last_slash = rest.rfind("/")
        tag_colon = rest.find(":", last_slash + 1)
        if tag_colon != -1:
            name_part, version = rest[:tag_colon], rest[tag_colon + 1:]
            if not version:
                raise MalformedReference(
                    "empty tag after ':'", value=s, production="tag",
                )
            validate_tag(version)
        else:
            name_part = rest

        if not name_part:
            raise MalformedReference(
                "missing name", value=s, production="name",
            )

        endpoint, path, name = OciReference._parse_name(name_part, original=s)

        return OciReference(
            endpoint=endpoint, path=path, name=name, version=version,
            digest=digest,
        )

    @staticmethod
    def _parse_name(name_part: str, *, original: str) -> tuple[Endpoint | None, str, str]:
        """Parse the ``name`` production into (endpoint, path, name).

        ``name := [domain '/'] remote-name``. Applies the §3.3 endpoint
        disambiguation heuristic, enforces the 255-char total cap, rejects a
        bare ``identifier`` as a name, and validates every path component.
        """
        components = name_part.split("/")
        first = components[0]

        endpoint: Endpoint | None = None
        if len(components) > 1 and _looks_like_domain(first):
            endpoint = OciReference._parse_endpoint(first, original=original)
            repo_components = components[1:]
        else:
            repo_components = components

        if not repo_components or any(c == "" for c in repo_components):
            raise MalformedReference(
                "empty path component in name", value=original,
                production="remote-name",
            )

        name = repo_components[-1]
        path = "/".join(repo_components[:-1])  # "" when no namespace prefix

        # The grammar checks on the `name` production (per-component validity,
        # 255 total-length cap, bare-identifier rejection) live in ONE shared
        # validator so parse and the constructor stay in lock-step — there is
        # no construction path that yields a ref parse would reject (gate C).
        _validate_name_production(endpoint, path, name, original=original)
        return endpoint, path, name

    @staticmethod
    def _parse_endpoint(domain: str, *, original: str) -> Endpoint:
        """Parse a ``domain := host [':' port-number]`` into an Endpoint.

        OCI references never carry userinfo, so username/password stay None.
        The host/port split is on the LAST ':' that is not inside an IPv6
        bracket (``[::1]:5000``).
        """
        host = domain
        port: int | None = None
        if domain.startswith("["):
            # [IPv6]  or  [IPv6]:port
            close = domain.find("]")
            if close == -1:
                raise MalformedReference(
                    "unterminated IPv6 host", value=original, production="host",
                )
            host = domain[: close + 1]
            remainder = domain[close + 1:]
            if remainder:
                if not remainder.startswith(":"):
                    raise MalformedReference(
                        "junk after IPv6 host", value=original, production="domain",
                    )
                port_str = remainder[1:]
                validate_port(port_str)
                port = int(port_str)
        elif ":" in domain:
            host, _, port_str = domain.rpartition(":")
            validate_port(port_str)
            port = int(port_str)

        validate_host(host)
        return Endpoint(host=host, port=port, username=None, password=None)

    # ------------------------------------------------------------------- #
    # Render — canonical string for podman calls (also __str__).
    # ------------------------------------------------------------------- #
    def render(self) -> str:
        """Render the canonical reference string (password masked, §3.5)."""
        out = ""
        if self.endpoint is not None:
            out += self.endpoint.render() + "/"
        out += self.pathname
        if self.version is not None:
            out += f":{self.version}"
        if self.digest is not None:
            out += f"@{self.digest.render()}"
        return out

    def __str__(self) -> str:
        return self.render()

    # ------------------------------------------------------------------- #
    # Normalize — separate, explicit, Docker-Hub convention (§3.4).
    # ------------------------------------------------------------------- #
    def normalize(self) -> "OciReference":
        """Apply Docker-Hub conventions, explicitly (§3.4). Pure.

        Mirrors ``ParseNormalizedNamed``: unqualified => ``docker.io``;
        ``index.docker.io`` => ``docker.io``; on docker.io a single-component
        repo gets the ``library/`` prefix; a name with no tag gets ``:latest``.

        Docker's own normalize also lowercases the repository, but our parse is
        stricter than Docker's: §3.1 ``path-component`` is ``[a-z0-9]+``, so any
        parsed/constructed ``OciReference`` already has a lowercase path+name.
        The lowercasing step is therefore a guaranteed no-op here and is
        omitted rather than carried as dead code.
        """
        endpoint = self.endpoint
        path = self.path
        version = self.version

        # defaultDomain: unqualified => docker.io.
        if endpoint is None:
            endpoint = Endpoint(host="docker.io", port=None)
        # legacyDefaultDomain: index.docker.io folds to docker.io.
        elif endpoint.host == "index.docker.io":
            endpoint = replace(endpoint, host="docker.io")

        on_docker_hub = endpoint.host == "docker.io"

        # officialRepoPrefix: docker.io + single-component repo => library/.
        if on_docker_hub and path == "":
            path = "library"

        # defaultTag: name-only ref => :latest (TagNameOnly).
        if version is None:
            version = "latest"

        return OciReference(
            endpoint=endpoint, path=path, name=self.name, version=version,
            digest=self.digest,
        )


def _looks_like_domain(component: str) -> bool:
    """The §3.3 endpoint-disambiguation heuristic.

    The leading ``/``-component is an ``Endpoint`` iff it is ``localhost``,
    OR it contains ``.`` or ``:``, OR it contains an uppercase letter.
    Otherwise there is no endpoint and every component is part of the
    repository path. (This is podman/docker ``splitDockerDomain`` detection,
    minus the defaulting.)
    """
    return (
        component == "localhost"
        or "." in component
        or ":" in component
        or component.lower() != component
    )


def _render_name_production(
    endpoint: Endpoint | None, path: str, name: str,
) -> str:
    """Render the ``name`` production string — domain + '/' + remote-name.

    This is the substring the 255-char cap applies to (no tag/digest). The
    endpoint is rendered with its password masked (the cap counts the masked
    form, which is what ``render()`` emits). Used for the length check and for
    error-message context in the constructor path.
    """
    out = ""
    if endpoint is not None:
        out += endpoint.render() + "/"
    out += f"{path}/{name}" if path else name
    return out


def _validate_name_production(
    endpoint: Endpoint | None, path: str, name: str, *, original: str,
) -> None:
    """Validate the full ``name`` production (§3.1) for given components.

    The SINGLE source of truth shared by ``OciReference.parse`` and
    ``OciReference.__post_init__`` so construction is closed under the grammar
    (gate C): every path component is valid, the domain+remote-name length is
    within the 255 cap, and a bare unqualified 64-hex leaf is rejected as an
    image identifier rather than accepted as a name.
    """
    if not name:
        raise MalformedReference(
            "missing name", value=original, production="name",
        )
    repo_components = ([] if path == "" else path.split("/"))
    repo_components.append(name)

    for component in repo_components:
        validate_path_component(component)

    # §3.1 constraint: total domain/remote-name length <= 255.
    name_production = _render_name_production(endpoint, path, name)
    if len(name_production) > _REPO_NAME_TOTAL_MAX:
        raise MalformedReference(
            f"repository name exceeds {_REPO_NAME_TOTAL_MAX} chars "
            f"(got {len(name_production)})",
            value=original, production="RepositoryNameTotalLengthMax",
        )

    # A bare 64-char [a-f0-9] string is an image IDENTIFIER, not a name —
    # rejected ONLY when unqualified (no endpoint) and the whole repository is
    # that single leaf (no namespace path). That is the ambiguous short-ID case.
    if endpoint is None and path == "" and _IDENTIFIER.match(name):
        raise MalformedReference(
            "bare 64-hex string is an image identifier, not a name; "
            "qualify it (e.g. with a registry) to use it as a reference",
            value=original, production="identifier",
        )
