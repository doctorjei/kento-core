"""Mutation-proven suite for the Result-native HTTPS fetcher (``kento.fetch``).

Block B2 — the first REAL I/O boundary-conversion site. Asserts the LOCKED
exception→ConditionKind mapping (the heart of the block):

* https-only policy: a non-https ``ref`` is refused BEFORE any network call.
* the wire URL: real password, query INCLUDED, fragment EXCLUDED.
* the fragment-drop ``Warning`` (browser parity — fetch still proceeds).
* the size cap enforced DURING the stream (Content-Length is NOT trusted).
* the boundary conversions: timeout → ``FETCH_TIMEOUT``; ``HTTPError`` →
  ``HTTP_ERROR``; ``URLError`` (incl. TLS) → ``FETCH_FAILED``.
* the PANIC boundary: a local-disk ``OSError`` while writing ``dest`` is NOT
  converted — it propagates.
* the env / explicit ``max_bytes`` cap resolution.

NO real network: ``urllib.request.urlopen`` is patched to return a fake
streaming response or to raise. Each test is written so the corresponding
mutation (drop an ``except``, trust Content-Length, swallow the disk OSError)
reddens it.

Spec: ``~/playbook/blocks/block-b2-fetcher-core.md`` (LOCKED) + design-doc §2
principle 5 + ``url-vm-source-design.md`` Fetcher/OPTION 2.
"""

import io
import socket
import urllib.error
import urllib.request

import pytest

from kento import ConditionKind, Error, Ok, Severity, UrlReference, Warning
from kento.fetch import (
    DEFAULT_MAX_BYTES,
    fetch_url,
)


# --------------------------------------------------------------------------- #
# Helpers — a fake urlopen response + ref construction.
# --------------------------------------------------------------------------- #


class _FakeResponse:
    """A minimal context-manager stand-in for ``urlopen``'s HTTPResponse.

    Streams ``body`` in chunks via ``read(n)``; records whether it was closed.
    Enough surface for the fetcher (``read`` + the ``with`` protocol).
    """

    def __init__(self, body: bytes):
        self._buf = io.BytesIO(body)
        self.closed = False

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc) -> None:
        self.closed = True


def _ref(url: str) -> UrlReference:
    """Parse a URL string into a ``UrlReference`` (unwrap the ``Ok``)."""
    return UrlReference.parse(url).unwrap()


def _patch_urlopen(monkeypatch, fn):
    """Patch the symbol the fetcher actually calls (``urllib.request.urlopen``).

    The fetcher imports the module and calls ``urllib.request.urlopen`` by
    attribute, so we patch it on the module. ``fn`` receives ``(url, *,
    timeout=...)`` and returns a response (or raises).
    """
    calls = []

    def wrapper(url, *args, **kwargs):
        calls.append(url)
        return fn(url, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", wrapper)
    return calls


# --------------------------------------------------------------------------- #
# non-https reject — NO network call.
# --------------------------------------------------------------------------- #


def test_non_https_refused_without_network(monkeypatch, tmp_path):
    ref = _ref("http://host/path/rootfs.txz")

    def _boom(*a, **k):  # urlopen must NOT be reached
        raise AssertionError("urlopen called for a non-https URL")

    calls = _patch_urlopen(monkeypatch, _boom)

    result = fetch_url(ref, tmp_path / "out.bin")

    assert isinstance(result, Error)
    assert result.is_error()
    assert [c.kind for c in result.conditions] == [ConditionKind.NON_HTTPS]
    assert result.conditions[0].severity is Severity.ERROR
    assert calls == []  # urlopen never invoked


def test_non_https_condition_masks_password(monkeypatch, tmp_path):
    ref = _ref("http://user:secret@host/x")
    _patch_urlopen(monkeypatch, lambda *a, **k: _FakeResponse(b""))

    result = fetch_url(ref, tmp_path / "out.bin")
    assert isinstance(result, Error)
    url_ctx = result.conditions[0].context["url"]
    assert "secret" not in url_ctx
    assert "****" in url_ctx


# --------------------------------------------------------------------------- #
# success — exact bytes; wire URL has real password, query, NO fragment.
# --------------------------------------------------------------------------- #


def test_success_writes_exact_bytes(monkeypatch, tmp_path):
    payload = b"hello rootfs bytes" * 100
    ref = _ref("https://host/path/to/rootfs.txz")
    _patch_urlopen(monkeypatch, lambda *a, **k: _FakeResponse(payload))

    dest = tmp_path / "out.bin"
    result = fetch_url(ref, dest)

    assert isinstance(result, Ok)
    assert result.unwrap() == dest
    assert dest.read_bytes() == payload


def test_wire_url_has_real_password_query_no_fragment(monkeypatch, tmp_path):
    ref = _ref("https://user:secret@host/path/rootfs.txz?token=abc#frag")
    calls = _patch_urlopen(monkeypatch, lambda *a, **k: _FakeResponse(b"x"))

    fetch_url(ref, tmp_path / "out.bin")

    assert len(calls) == 1
    wire = calls[0]
    assert "secret" in wire  # real password on the wire
    assert "token=abc" in wire  # query IS sent (load-bearing)
    assert "#frag" not in wire  # fragment is NOT sent
    assert wire.startswith("https://")


def test_wire_url_includes_version(monkeypatch, tmp_path):
    ref = _ref("https://host/path/rootfs+1.2")
    calls = _patch_urlopen(monkeypatch, lambda *a, **k: _FakeResponse(b"x"))
    fetch_url(ref, tmp_path / "out.bin")
    assert calls[0].endswith("rootfs+1.2")


# --------------------------------------------------------------------------- #
# fragment drop → Warning(dest, [FRAGMENT_DROPPED]).
# --------------------------------------------------------------------------- #


def test_fragment_drop_warning(monkeypatch, tmp_path):
    ref = _ref("https://host/path/rootfs.txz#section-2")
    calls = _patch_urlopen(monkeypatch, lambda *a, **k: _FakeResponse(b"abc"))

    dest = tmp_path / "out.bin"
    result = fetch_url(ref, dest)

    assert isinstance(result, Warning)
    assert result.is_ok()  # a Warning still carries the value
    assert result.unwrap() == dest
    assert dest.read_bytes() == b"abc"
    kinds = [c.kind for c in result.conditions]
    assert kinds == [ConditionKind.FRAGMENT_DROPPED]
    cond = result.conditions[0]
    assert cond.severity is Severity.WARNING
    assert cond.context["fragment"] == "section-2"
    assert "section-2" in cond.message
    # the dropped fragment never reached the wire
    assert "#section-2" not in calls[0]


def test_no_fragment_is_plain_ok(monkeypatch, tmp_path):
    ref = _ref("https://host/path/rootfs.txz")
    _patch_urlopen(monkeypatch, lambda *a, **k: _FakeResponse(b"abc"))
    result = fetch_url(ref, tmp_path / "out.bin")
    assert isinstance(result, Ok)
    assert result.conditions == ()


# --------------------------------------------------------------------------- #
# size cap — enforced DURING the stream; partial dest deleted.
# Mutation guard: a server that LIES (no/low Content-Length) still gets capped.
# --------------------------------------------------------------------------- #


def test_size_cap_enforced_during_stream(monkeypatch, tmp_path):
    # Body far exceeds the cap; the fake reports NO Content-Length at all, so a
    # fetcher that trusted Content-Length would never trip → this test reddens
    # that mutation.
    body = b"A" * (5 * 1024 * 1024)  # 5 MiB
    ref = _ref("https://host/path/big.txz")
    _patch_urlopen(monkeypatch, lambda *a, **k: _FakeResponse(body))

    dest = tmp_path / "big.bin"
    result = fetch_url(ref, dest, max_bytes=1024)  # 1 KiB cap

    assert isinstance(result, Error)
    assert [c.kind for c in result.conditions] == [ConditionKind.SIZE_EXCEEDED]
    assert result.conditions[0].context["cap"] == 1024
    # the partial file must be deleted
    assert not dest.exists()


def test_size_cap_exact_boundary_ok(monkeypatch, tmp_path):
    # Exactly cap bytes is allowed (strict ">" comparison).
    body = b"B" * 1024
    ref = _ref("https://host/path/exact.txz")
    _patch_urlopen(monkeypatch, lambda *a, **k: _FakeResponse(body))
    dest = tmp_path / "exact.bin"
    result = fetch_url(ref, dest, max_bytes=1024)
    assert isinstance(result, Ok)
    assert dest.read_bytes() == body


# --------------------------------------------------------------------------- #
# boundary conversions — each mutation (drop an except) reddens its test.
# --------------------------------------------------------------------------- #


def test_timeout_converts_to_fetch_timeout(monkeypatch, tmp_path):
    ref = _ref("https://host/x")

    def _raise(*a, **k):
        raise TimeoutError("read timed out")

    _patch_urlopen(monkeypatch, _raise)
    result = fetch_url(ref, tmp_path / "out.bin")
    assert isinstance(result, Error)
    assert [c.kind for c in result.conditions] == [ConditionKind.FETCH_TIMEOUT]


def test_urlerror_timeout_reason_converts_to_fetch_timeout(monkeypatch, tmp_path):
    ref = _ref("https://host/x")

    def _raise(*a, **k):
        raise urllib.error.URLError(socket.timeout("timed out"))

    _patch_urlopen(monkeypatch, _raise)
    result = fetch_url(ref, tmp_path / "out.bin")
    assert isinstance(result, Error)
    assert [c.kind for c in result.conditions] == [ConditionKind.FETCH_TIMEOUT]


def test_httperror_converts_to_http_error_with_status(monkeypatch, tmp_path):
    ref = _ref("https://host/missing.txz")

    def _raise(*a, **k):
        raise urllib.error.HTTPError(
            url="https://host/missing.txz", code=404, msg="Not Found",
            hdrs=None, fp=None,
        )

    _patch_urlopen(monkeypatch, _raise)
    result = fetch_url(ref, tmp_path / "out.bin")
    assert isinstance(result, Error)
    cond = result.conditions[0]
    assert cond.kind is ConditionKind.HTTP_ERROR
    assert cond.context["status"] == 404


def test_urlerror_tls_converts_to_fetch_failed(monkeypatch, tmp_path):
    ref = _ref("https://host/x")

    def _raise(*a, **k):
        raise urllib.error.URLError("ssl handshake failed")

    _patch_urlopen(monkeypatch, _raise)
    result = fetch_url(ref, tmp_path / "out.bin")
    assert isinstance(result, Error)
    cond = result.conditions[0]
    assert cond.kind is ConditionKind.FETCH_FAILED
    assert "ssl handshake failed" in cond.context["reason"]


def test_httperror_caught_before_urlerror(monkeypatch, tmp_path):
    # HTTPError IS-A URLError; ordering the except HTTPError first is required, or
    # a 4xx/5xx would be misclassified as FETCH_FAILED. This asserts the order.
    ref = _ref("https://host/x")

    def _raise(*a, **k):
        raise urllib.error.HTTPError(
            url="https://host/x", code=503, msg="busy", hdrs=None, fp=None,
        )

    _patch_urlopen(monkeypatch, _raise)
    result = fetch_url(ref, tmp_path / "out.bin")
    assert result.conditions[0].kind is ConditionKind.HTTP_ERROR


# --------------------------------------------------------------------------- #
# the PANIC boundary — a disk OSError is NOT converted; it propagates.
# Mutation guard: wrapping the write in `except OSError -> Error` reddens this.
# --------------------------------------------------------------------------- #


def test_disk_oserror_propagates_as_panic(monkeypatch, tmp_path):
    # dest parent does not exist → open(dest, "wb") raises OSError. The fetcher
    # must NOT convert it to an Error; it propagates.
    ref = _ref("https://host/x")
    _patch_urlopen(monkeypatch, lambda *a, **k: _FakeResponse(b"data"))

    dest = tmp_path / "does-not-exist" / "out.bin"
    with pytest.raises(OSError):
        fetch_url(ref, dest)


def test_write_oserror_propagates_as_panic(monkeypatch, tmp_path):
    # A write() that raises OSError mid-stream is a disk fault → propagate.
    ref = _ref("https://host/x")
    _patch_urlopen(monkeypatch, lambda *a, **k: _FakeResponse(b"data" * 100))

    real_open = open

    class _BadFile:
        def __init__(self, fh):
            self._fh = fh

        def write(self, data):
            raise OSError("disk full")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()

    def _bad_open(path, mode="r", *a, **k):
        return _BadFile(real_open(path, mode, *a, **k))

    monkeypatch.setattr("kento.fetch.open", _bad_open, raising=False)
    with pytest.raises(OSError):
        fetch_url(ref, tmp_path / "out.bin")


# --------------------------------------------------------------------------- #
# cap resolution — env default, explicit wins over env.
# --------------------------------------------------------------------------- #


def test_default_cap_constant():
    assert DEFAULT_MAX_BYTES == 2 * 1024**3


def test_env_cap_overrides_default(monkeypatch, tmp_path):
    monkeypatch.setenv("KENTO_URL_MAX_BYTES", "10")
    ref = _ref("https://host/x")
    _patch_urlopen(monkeypatch, lambda *a, **k: _FakeResponse(b"X" * 100))
    dest = tmp_path / "out.bin"
    result = fetch_url(ref, dest)  # max_bytes=None → reads env (10)
    assert isinstance(result, Error)
    assert result.conditions[0].kind is ConditionKind.SIZE_EXCEEDED
    assert result.conditions[0].context["cap"] == 10
    assert not dest.exists()


def test_explicit_max_bytes_wins_over_env(monkeypatch, tmp_path):
    monkeypatch.setenv("KENTO_URL_MAX_BYTES", "5")  # would trip
    ref = _ref("https://host/x")
    _patch_urlopen(monkeypatch, lambda *a, **k: _FakeResponse(b"X" * 100))
    dest = tmp_path / "out.bin"
    # explicit cap of 1000 wins → the 100-byte body fits
    result = fetch_url(ref, dest, max_bytes=1000)
    assert isinstance(result, Ok)
    assert dest.read_bytes() == b"X" * 100


def test_env_unset_uses_default(monkeypatch, tmp_path):
    monkeypatch.delenv("KENTO_URL_MAX_BYTES", raising=False)
    ref = _ref("https://host/x")
    _patch_urlopen(monkeypatch, lambda *a, **k: _FakeResponse(b"small"))
    dest = tmp_path / "out.bin"
    result = fetch_url(ref, dest)  # default 2 GiB → fits
    assert isinstance(result, Ok)
    assert dest.read_bytes() == b"small"
