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
from kento import fetch as fetch_mod
from kento.fetch import (
    DEFAULT_MAX_BYTES,
    _RedirectObserver,
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


# --------------------------------------------------------------------------- #
# Block B2-redirect-warn — a cleartext (http://) redirect hop is FOLLOWED (never
# failed) and surfaced as a WARNING; the initial-URL https policy is UNCHANGED.
# --------------------------------------------------------------------------- #


import email.message  # noqa: E402  (test-local: minimal headers for the handler)


def _hdrs() -> email.message.Message:
    """A minimal ``headers`` mapping for calling ``redirect_request`` directly."""
    return email.message.Message()


def _preload_observer(monkeypatch, hops):
    """Patch ``_redirect_opener`` so the observer ends the stream with ``hops``.

    Returns an observer already carrying ``hops`` in ``insecure_hops`` (simulating
    what urllib's redirect chain would have recorded), paired with the real
    opener. This is the ``build_opener``/opener seam the brief calls for; the body
    itself still streams through the patched ``urlopen``.
    """
    observer = _RedirectObserver()
    observer.insecure_hops = list(hops)
    opener = urllib.request.build_opener(observer)

    def _fake_opener():
        return opener, observer

    monkeypatch.setattr(fetch_mod, "_redirect_opener", _fake_opener)
    return observer


# --- handler unit: records an http:// hop, still follows; ignores https://. ---


def test_observer_records_http_hop_and_still_follows():
    obs = _RedirectObserver()
    req = urllib.request.Request("https://orig/x")
    new = obs.redirect_request(
        req, None, 302, "Found", _hdrs(), "http://evil.example/y"
    )
    assert new is not None  # still FOLLOWED (never blocked)
    assert obs.insecure_hops == ["http://evil.example/y"]


def test_observer_ignores_https_hop():
    obs = _RedirectObserver()
    req = urllib.request.Request("https://orig/x")
    new = obs.redirect_request(
        req, None, 302, "Found", _hdrs(), "https://cdn.example/y"
    )
    assert new is not None  # followed
    assert obs.insecure_hops == []  # nothing recorded for a secure hop


def test_observer_records_nothing_when_super_refuses(monkeypatch):
    # If the base handler declines the hop (returns None — its documented "can't,
    # but another handler might" path), we record NOTHING: we warn only about a
    # downgrade the fetch actually takes, never one urllib refuses to follow. The
    # ``new is not None`` guard is exercised by forcing super() to return None.
    monkeypatch.setattr(
        urllib.request.HTTPRedirectHandler,
        "redirect_request",
        lambda *a, **k: None,
    )
    obs = _RedirectObserver()
    req = urllib.request.Request("https://orig/x")
    new = obs.redirect_request(
        req, None, 302, "Found", _hdrs(), "http://evil.example/y"
    )
    assert new is None  # not followed → nothing recorded despite http:// target
    assert obs.insecure_hops == []


# --- fetch-level: insecure hop on a clean fetch → Warning(INSECURE_REDIRECT). ---


def test_insecure_hop_on_clean_fetch_is_warning(monkeypatch, tmp_path):
    ref = _ref("https://host/path/rootfs.txz")
    _patch_urlopen(monkeypatch, lambda *a, **k: _FakeResponse(b"good body"))
    _preload_observer(monkeypatch, ["http://evil.example/redir?token=abc"])

    dest = tmp_path / "out.bin"
    result = fetch_url(ref, dest)

    assert isinstance(result, Warning)
    assert result.is_ok()  # a Warning still carries the value
    assert result.unwrap() == dest
    assert dest.read_bytes() == b"good body"  # the bytes ARE delivered
    kinds = [c.kind for c in result.conditions]
    assert kinds == [ConditionKind.INSECURE_REDIRECT]
    cond = result.conditions[0]
    assert cond.severity is Severity.WARNING
    assert cond.context["insecure_hop_host"] == "evil.example"
    assert cond.context["hop_count"] == 1


def test_insecure_hop_does_not_leak_secret(monkeypatch, tmp_path):
    # The recorded hop carries a presigned token in its query; it must appear
    # NOWHERE in the Condition. Mutation guard: dumping newurl raw reddens this.
    ref = _ref("https://host/x")
    _patch_urlopen(monkeypatch, lambda *a, **k: _FakeResponse(b"x"))
    _preload_observer(
        monkeypatch, ["http://evil.example/p?token=SECRET&sig=DEADBEEF"]
    )

    result = fetch_url(ref, tmp_path / "out.bin")
    assert isinstance(result, Warning)
    cond = result.conditions[0]
    blob = cond.message + repr(dict(cond.context))
    assert "SECRET" not in blob
    assert "DEADBEEF" not in blob
    assert "token" not in blob
    assert "evil.example" in blob  # only the host is exposed


def test_multiple_insecure_hops_collapse_to_one_condition(monkeypatch, tmp_path):
    ref = _ref("https://host/x")
    _patch_urlopen(monkeypatch, lambda *a, **k: _FakeResponse(b"x"))
    _preload_observer(
        monkeypatch,
        ["http://first.example/a", "http://last.example/b?token=T"],
    )

    result = fetch_url(ref, tmp_path / "out.bin")
    assert isinstance(result, Warning)
    # ONE condition summarizing; host is the LAST hop, count is 2.
    assert len(result.conditions) == 1
    cond = result.conditions[0]
    assert cond.kind is ConditionKind.INSECURE_REDIRECT
    assert cond.context["insecure_hop_host"] == "last.example"
    assert cond.context["hop_count"] == 2


def test_clean_https_redirect_is_plain_ok(monkeypatch, tmp_path):
    # An https→https redirect (observer records nothing) → Ok, no warning.
    ref = _ref("https://host/x")
    _patch_urlopen(monkeypatch, lambda *a, **k: _FakeResponse(b"body"))
    _preload_observer(monkeypatch, [])  # no insecure hop recorded

    result = fetch_url(ref, tmp_path / "out.bin")
    assert isinstance(result, Ok)
    assert result.conditions == ()


def test_fragment_and_insecure_hop_ride_together(monkeypatch, tmp_path):
    # Both a dropped fragment AND a cleartext hop on a clean fetch → ONE Warning
    # carrying BOTH conditions.
    ref = _ref("https://host/path/rootfs.txz#section-2")
    _patch_urlopen(monkeypatch, lambda *a, **k: _FakeResponse(b"body"))
    _preload_observer(monkeypatch, ["http://evil.example/y"])

    result = fetch_url(ref, tmp_path / "out.bin")
    assert isinstance(result, Warning)
    kinds = {c.kind for c in result.conditions}
    assert kinds == {
        ConditionKind.FRAGMENT_DROPPED,
        ConditionKind.INSECURE_REDIRECT,
    }


def test_insecure_hop_then_failure_is_error_not_warning(monkeypatch, tmp_path):
    # A hop is recorded, but the fetch then FAILS (HTTP error). The result is the
    # Error — the redirect warning must NOT ride on it. Mutation guard: attaching
    # the warning to the Error path reddens this.
    ref = _ref("https://host/missing.txz")

    def _raise(*a, **k):
        raise urllib.error.HTTPError(
            url="https://host/missing.txz", code=404, msg="Not Found",
            hdrs=None, fp=None,
        )

    _patch_urlopen(monkeypatch, _raise)
    _preload_observer(monkeypatch, ["http://evil.example/y"])

    result = fetch_url(ref, tmp_path / "out.bin")
    assert isinstance(result, Error)
    kinds = [c.kind for c in result.conditions]
    assert kinds == [ConditionKind.HTTP_ERROR]
    assert ConditionKind.INSECURE_REDIRECT not in kinds


def test_insecure_hop_then_size_exceeded_is_error_not_warning(
    monkeypatch, tmp_path
):
    # A hop is recorded, but the body then exceeds the cap. The result is the
    # SIZE_EXCEEDED Error; the redirect warning does not ride on it.
    ref = _ref("https://host/big.txz")
    _patch_urlopen(
        monkeypatch, lambda *a, **k: _FakeResponse(b"A" * 4096)
    )
    _preload_observer(monkeypatch, ["http://evil.example/y"])

    dest = tmp_path / "big.bin"
    result = fetch_url(ref, dest, max_bytes=16)
    assert isinstance(result, Error)
    kinds = [c.kind for c in result.conditions]
    assert kinds == [ConditionKind.SIZE_EXCEEDED]
    assert not dest.exists()


def test_initial_non_https_still_errors_no_network(monkeypatch, tmp_path):
    # The initial-URL https policy is UNCHANGED: a non-https initial URL is a
    # hard Error(NON_HTTPS), never a warning, and no network is touched.
    ref = _ref("http://host/path/rootfs.txz")

    def _boom(*a, **k):
        raise AssertionError("urlopen called for a non-https initial URL")

    calls = _patch_urlopen(monkeypatch, _boom)
    result = fetch_url(ref, tmp_path / "out.bin")
    assert isinstance(result, Error)
    assert [c.kind for c in result.conditions] == [ConditionKind.NON_HTTPS]
    assert calls == []


def test_opener_default_restored_after_fetch(monkeypatch, tmp_path):
    # The redirect opener is installed only for the duration of the urlopen call;
    # the previous global default opener must be restored afterward (no leak into
    # a caller's later urllib use).
    sentinel = urllib.request.build_opener()
    urllib.request.install_opener(sentinel)
    try:
        ref = _ref("https://host/x")
        _patch_urlopen(monkeypatch, lambda *a, **k: _FakeResponse(b"x"))
        fetch_url(ref, tmp_path / "out.bin")
        assert urllib.request._opener is sentinel
    finally:
        urllib.request.install_opener(None)


def test_conditionkind_insecure_redirect_value():
    assert ConditionKind.INSECURE_REDIRECT == "insecure_redirect"
    assert ConditionKind.INSECURE_REDIRECT.value == "insecure_redirect"
