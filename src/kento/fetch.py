"""Result-native HTTPS fetcher — the first REAL I/O boundary-conversion site.

This module is the showcase for the ``Result`` doctrine's **boundary-conversion
discipline** (design-doc §2 principle 5, ``result-type-design.md`` §4/§5):

* The **predictable** network failures a reasonable caller must handle — a
  timeout, an HTTP 4xx/5xx, a connection/DNS/TLS failure, an oversized body —
  are CAUGHT and converted to an ``Error`` condition (the ``Result`` channel).
* The **unexpected** failure — a local-disk ``OSError`` while writing ``dest`` —
  is NOT caught. It is an environmental/system fault, not part of the fetch
  contract, so it PROPAGATES as a panic (the exception channel).
* The fragment-drop is the first REAL ``Warning``: a non-empty URL fragment is
  not sent to the server (RFC-3986 §3.5 separates ``#…`` before dereference, so
  it is never in the HTTP request); we drop it and record a ``Warning`` condition
  — the fetch still proceeds (browser/curl/wget parity).

Policy lives HERE, at the I/O edge — not in the pure ``UrlReference`` value type
(which parses ``http`` faithfully). The fetcher REFUSES a non-https URL, enforces
the size cap DURING streaming (Content-Length can lie), and never caches.

stdlib ``urllib.request`` only (the zero-pip-deps principle). This module is an
INTERNAL I/O helper (like ``layers.py``): not re-exported from ``kento``; tests
import ``from kento.fetch import fetch_url``.

Spec: ``~/playbook/blocks/block-b2-fetcher-core.md`` (the LOCKED block brief) +
``kento-core-api-design.md`` §2 principle 5 + ``url-vm-source-design.md`` Fetcher
+ OPTION 2 LOCKED + ``result-type-design.md`` (the ``Result`` family).
"""

from __future__ import annotations

import logging
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path

from kento._references import UrlReference
from kento._result import (
    Condition,
    ConditionKind,
    Error,
    Result,
    Severity,
)

__all__ = ["fetch_url", "DEFAULT_TIMEOUT", "DEFAULT_MAX_BYTES"]

logger = logging.getLogger("kento")

# Default per-fetch byte ceiling (OPTION 2 M1): 2 GiB. A full-OS rootfs is
# 100s of MB to a few GB, so this is generous-but-bounded. Overridable per-call
# (``max_bytes=``) or via the ``KENTO_URL_MAX_BYTES`` env var.
DEFAULT_MAX_BYTES = 2 * 1024**3

# Default socket timeout for the fetch (seconds). Bounds DNS/connect/read so a
# dead or slow host converts to a predictable ``FETCH_TIMEOUT`` rather than
# hanging the caller forever.
DEFAULT_TIMEOUT = 30.0

# Stream chunk size (bytes). Read the body in fixed chunks so the size cap is
# enforced incrementally — we never buffer the whole (possibly-lying) body.
_CHUNK = 64 * 1024


def _max_bytes_from_env() -> int:
    """Resolve the default size cap from ``KENTO_URL_MAX_BYTES`` or the constant.

    Used only when the caller passes ``max_bytes=None`` (the default). House
    env-override idiom (``os.environ.get(...)``); a malformed env value is a
    configuration bug, so ``int()`` is allowed to raise (a panic — not the
    fetch contract).
    """
    raw = os.environ.get("KENTO_URL_MAX_BYTES")
    if raw is None:
        return DEFAULT_MAX_BYTES
    return int(raw)


def _wire_url(ref: UrlReference) -> str:
    """Build the dereference URL from a ``UrlReference`` — the REAL wire bytes.

    The single place the real password is rendered (``mask_password=False``): it
    goes ONLY to ``urlopen``, never to a log or ``Condition``. The query IS
    appended (load-bearing — presigned-URL auth tokens live there); the fragment
    is NOT (RFC-3986 §3.5 — separated before dereference; the fetcher drops it
    with a warning instead). Mirrors ``UrlReference.render`` component order, but
    with the real userinfo and without the fragment.
    """
    url = f"{ref.scheme}://" + ref.endpoint.render(mask_password=False) + "/" + ref.pathname
    if ref.version is not None:
        url += f"+{ref.version}"
    if ref.query:
        url += f"?{ref.query}"
    return url


def _is_timeout(exc: urllib.error.URLError) -> bool:
    """True when a ``URLError`` actually wraps a timeout.

    A read/connect timeout surfaces as ``URLError`` whose ``.reason`` is a
    ``TimeoutError``/``socket.timeout``. We route those to ``FETCH_TIMEOUT``
    rather than the generic ``FETCH_FAILED`` so the caller can tell a slow/dead
    host from a refused/DNS/TLS failure.
    """
    return isinstance(exc.reason, (TimeoutError, socket.timeout))


def fetch_url(
    ref: UrlReference,
    dest: Path,
    *,
    max_bytes: int | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Result[Path]:
    """Stream an ``https://`` URL to ``dest``, Result-native (the §2 pr.5 showcase).

    Returns a ``Result[Path]`` whose value (on success) is ``dest``:

    * ``Ok(dest)`` — fetched cleanly.
    * ``Warning(dest, [FRAGMENT_DROPPED])`` — fetched, but ``ref.fragment`` was
      non-empty and dropped (not sent to the server; browser parity).
    * ``Error`` carrying one ERROR condition — a PREDICTABLE failure converted at
      this boundary: ``NON_HTTPS`` (refused before any network call),
      ``FETCH_TIMEOUT``, ``HTTP_ERROR`` (4xx/5xx), ``FETCH_FAILED``
      (connection/DNS/TLS), or ``SIZE_EXCEEDED`` (body over the cap).

    An UNEXPECTED failure — a local-disk ``OSError`` while writing ``dest`` — is
    deliberately NOT caught: it propagates as a panic (an environmental fault, not
    the fetch contract). ``dest`` is the target FILE path; its parent must already
    exist (the fetcher writes the file, not the directory).

    ``max_bytes=None`` resolves the cap from ``KENTO_URL_MAX_BYTES`` (else
    ``DEFAULT_MAX_BYTES``); an explicit ``max_bytes=`` wins. No caching — every
    call fetches fresh.
    """
    # --- Policy at the I/O edge: refuse non-https, BEFORE any network call. The
    # value type parses http faithfully; the fetcher is where the policy lives
    # (OPTION 2 M2). ``ref.render()`` masks the password.
    if not ref.secure:
        return Error(
            conditions=(
                Condition(
                    severity=Severity.ERROR,
                    kind=ConditionKind.NON_HTTPS,
                    message="refusing to fetch a non-https URL",
                    context={"url": ref.render()},
                ),
            ),
        )

    cap = _max_bytes_from_env() if max_bytes is None else max_bytes

    # The masked url for Conditions/logs (the real one stays in ``wire`` →
    # urlopen only). Accumulate the fragment-drop warning (if any) so a clean
    # fetch becomes Warning(dest, [...]) via Result.of.
    masked = ref.render()
    conditions: list[Condition] = []
    if ref.fragment:
        conditions.append(
            Condition(
                severity=Severity.WARNING,
                kind=ConditionKind.FRAGMENT_DROPPED,
                message=(
                    f"dropped fragment '{ref.fragment}' from URL "
                    "(not sent to server)"
                ),
                context={"fragment": ref.fragment},
            )
        )

    wire = _wire_url(ref)
    logger.debug("fetch: GET %s -> %s (cap=%d)", masked, dest, cap)

    # --- The boundary. Catch ONLY the predictable network failures and convert
    # them to an Error; let everything else propagate (panic). HTTPError is a
    # subclass of URLError, so it MUST be caught first.
    try:
        return _stream_to_file(wire, dest, cap, timeout, masked, conditions)
    except urllib.error.HTTPError as exc:
        return Error(
            conditions=(
                Condition(
                    severity=Severity.ERROR,
                    kind=ConditionKind.HTTP_ERROR,
                    message=f"server returned HTTP {exc.code}",
                    context={"status": exc.code, "url": masked},
                ),
            ),
        )
    except urllib.error.URLError as exc:
        if _is_timeout(exc):
            return Error(
                conditions=(
                    Condition(
                        severity=Severity.ERROR,
                        kind=ConditionKind.FETCH_TIMEOUT,
                        message="timed out fetching URL",
                        context={"url": masked},
                    ),
                ),
            )
        return Error(
            conditions=(
                Condition(
                    severity=Severity.ERROR,
                    kind=ConditionKind.FETCH_FAILED,
                    message=f"failed to fetch URL: {exc.reason}",
                    context={"reason": str(exc.reason), "url": masked},
                ),
            ),
        )
    except (TimeoutError, socket.timeout):
        # A bare timeout (not wrapped in URLError) — same FETCH_TIMEOUT verdict.
        return Error(
            conditions=(
                Condition(
                    severity=Severity.ERROR,
                    kind=ConditionKind.FETCH_TIMEOUT,
                    message="timed out fetching URL",
                    context={"url": masked},
                ),
            ),
        )


def _stream_to_file(
    wire: str,
    dest: Path,
    cap: int,
    timeout: float,
    masked: str,
    conditions: list[Condition],
) -> Result[Path]:
    """Open ``wire``, stream the body to ``dest``, enforcing ``cap`` per-chunk.

    Returns ``Result.of(dest, conditions)`` on a clean read (``Ok`` or, when the
    fragment was dropped, ``Warning``). On a body that EXCEEDS the cap mid-stream
    it aborts, deletes the partial ``dest``, and returns ``Error(SIZE_EXCEEDED)``
    — the cap is enforced on bytes actually STREAMED, never on the (lie-able)
    Content-Length. Network failures raise out of this function to ``fetch_url``'s
    boundary; a disk ``OSError`` while writing also propagates (panic) — it is NOT
    converted here.
    """
    # ``urlopen`` opens the connection (may raise URLError/HTTPError/timeout — the
    # boundary catches it). The ``with`` on the response closes the socket on every
    # exit path. We do NOT pass/honor any cache (no-cache: a fresh GET each call).
    with urllib.request.urlopen(wire, timeout=timeout) as resp:
        total = 0
        exceeded = False
        # The destination file handle is opened in the SAME ``with`` as the
        # network read so a size-cap abort can clean up the partial file. A disk
        # OSError from open()/write() is intentionally OUTSIDE every except in
        # ``fetch_url``'s boundary → it panics.
        with open(dest, "wb") as out:
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break  # genuine EOF — clean fetch.
                total += len(chunk)
                if total > cap:
                    # Over the cap: stop reading and drop the partial file. The
                    # check is on bytes actually STREAMED, never Content-Length
                    # (which can lie). ``out`` is flushed/closed on ``with`` exit;
                    # we unlink AFTER so the file is fully closed.
                    exceeded = True
                    break
                out.write(chunk)

    if not exceeded:
        return Result.of(dest, tuple(conditions))

    # The cap-break fired. The partial file is now closed; remove it. A failure
    # to remove the partial is a disk fault → propagate (panic), consistent with
    # the write-OSError rule.
    dest.unlink(missing_ok=True)
    return Error(
        conditions=(
            Condition(
                severity=Severity.ERROR,
                kind=ConditionKind.SIZE_EXCEEDED,
                message=(
                    f"download exceeded the {cap}-byte size cap "
                    "(streamed bytes, not Content-Length)"
                ),
                context={"cap": cap, "url": masked},
            ),
        ),
    )
