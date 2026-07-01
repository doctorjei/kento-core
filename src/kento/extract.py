"""Result-native ``.txz`` rootfs extractor — the second I/O boundary site.

This module is the sibling of ``kento.fetch``: where the fetcher STREAMS an
``https://`` URL to a local ``.txz`` file, this extractor takes that local file
and unpacks it SAFELY into a destination directory that becomes the VM's single
overlay ``lowerdir`` (URL-VM Phase B, OPTION 2 — flat single-tree ``.txz`` only).

It follows the SAME **boundary-conversion discipline** the fetcher established
(design-doc §2 principle 5, ``result-type-design.md`` §4/§5):

* A **predictable** archive failure — a corrupt/truncated xz stream, a payload
  that is not a tar, OR a **hostile member** (path traversal, an absolute path,
  a link whose target escapes the destination, a device/fifo) — is CAUGHT and
  converted to an ``Error(EXTRACT_FAILED)`` (the ``Result`` channel). A rejected
  hostile archive is a caller-handleable "this archive is unsafe", not a bug.
* The **unexpected** failure — a local-disk ``OSError`` while writing under
  ``dest_dir`` (disk-full, permission-denied) — is NOT caught. It is an
  environmental/system fault, not part of the extract contract, so it PROPAGATES
  as a panic (the exception channel), EXACTLY the boundary ``fetch.py`` draws for
  its write-``OSError``. (A missing ``archive`` surfaces as ``FileNotFoundError``
  ⊆ ``OSError`` → also propagates: the fetcher guarantees the file exists, so a
  missing archive is an internal invariant breach, not the extract contract.)

**Safety is the heart of this block.** The archive bytes are UNTRUSTED and
extraction happens on the HOST filesystem BEFORE the KVM boundary, so a hostile
tarball MUST NOT be able to write outside ``dest_dir``. We do NOT hand-roll the
tar-slip defense — hand-rolled symlink-aware extraction safety is a known CVE
source. We extract with the PEP 706 stdlib **data filter**
(``tarfile.data_filter``), the vetted, maintained defense against traversal,
absolute paths, escaping links, and special files. If the data filter is
unavailable (Python 3.11.0–3.11.3 lack it) we REFUSE to extract untrusted bytes
unsafely and RAISE (fail-closed panic) rather than silently degrade.

stdlib only (``tarfile``, ``lzma``, ``logging``, ``pathlib``). Zero pip deps.
This module is an INTERNAL I/O helper (like ``fetch.py``/``layers.py``): not
re-exported from ``kento``; tests import ``from kento.extract import extract_txz``.

Spec: ``~/playbook/blocks/block-b2-extract-core.md`` (the LOCKED block brief) +
``kento-core-api-design.md`` §2 principle 5 + ``url-vm-source-design.md`` OPTION 2
LOCKED + ``result-type-design.md`` (the ``Result`` family).
"""

from __future__ import annotations

import logging
import lzma
import tarfile
from pathlib import Path

from kento._result import (
    Condition,
    ConditionKind,
    Error,
    Ok,
    Result,
    Severity,
)

__all__ = ["extract_txz"]

logger = logging.getLogger("kento")


def extract_txz(archive: Path, dest_dir: Path) -> Result[Path]:
    """Extract a ``.txz`` rootfs tarball SAFELY into ``dest_dir``, Result-native.

    Returns a ``Result[Path]`` whose value (on success) is ``dest_dir``:

    * ``Ok(dest_dir)`` — extracted cleanly. The archive's tree lands DIRECTLY in
      ``dest_dir`` (gemet ``.txz`` is rooted at ``./`` → ``dest_dir/boot``,
      ``dest_dir/etc``, …); ``dest_dir`` IS the rootfs. No top-level-dir
      stripping, no layer flatten (single-tree format — OPTION 2 LOCKED).
    * ``Error`` carrying one ERROR ``EXTRACT_FAILED`` condition — a PREDICTABLE
      failure converted at this boundary: a corrupt/truncated xz stream, a
      not-a-tar payload, OR a hostile member the data filter rejected (traversal,
      absolute path, escaping link, special file). The context carries the
      ``archive`` path (and, for a filter rejection, the offending ``member``).

    An UNEXPECTED failure — a local-disk ``OSError`` while writing under
    ``dest_dir`` — is deliberately NOT caught: it propagates as a panic (an
    environmental fault, not the extract contract). A missing ``archive`` file
    surfaces as ``FileNotFoundError`` ⊆ ``OSError`` and likewise propagates (the
    fetcher guarantees the file, so a missing archive is an invariant breach).

    ``dest_dir`` contract (mirrors ``fetch_url``'s "caller owns the location, we
    do the I/O" split): the extractor CREATES ``dest_dir`` itself (its parent —
    the caller's container dir — already exists). It does NOT pre-clean existing
    content (the caller passes a fresh ephemeral dir).

    Opens strictly as xz (``mode="r:xz"``): the format is LOCKED to ``.txz``, so
    this fails closed on a non-xz stream rather than silently accepting, say, a
    ``.tar.gz``.
    """
    # Fail CLOSED if the PEP 706 data filter is unavailable (Python 3.11.0–3.11.3
    # lack it; ``requires-python = ">=3.11"`` technically permits those). We
    # REFUSE to extract untrusted bytes without the vetted tar-slip defense rather
    # than silently degrade to an unsafe unfiltered extraction. This is a panic (a
    # bug/environment fault), NOT a caller-handleable outcome — hence a raise, not
    # an Error. Checked BEFORE any filesystem work so nothing is written.
    if not hasattr(tarfile, "data_filter"):
        raise RuntimeError(
            "safe tar extraction requires the tarfile data filter "
            "(Python 3.11.4+); refusing to extract an untrusted archive "
            "without it"
        )

    # The extractor owns creating ``dest_dir`` (parent already exists). A disk
    # ``OSError`` here is an environmental fault → propagates (panic), same as the
    # extraction write path below and ``fetch.py``'s write-OSError rule.
    dest_dir.mkdir(parents=True, exist_ok=True)

    logger.debug("extract: %s -> %s (r:xz, data_filter)", archive, dest_dir)

    # --- The boundary. Catch ONLY the predictable archive failures and convert
    # them to an Error; let everything else propagate (panic).
    #
    # ``tarfile.ReadError`` ⊆ ``tarfile.TarError`` covers corrupt / not-a-tar, and
    # ``tarfile.FilterError`` ⊆ ``tarfile.TarError`` covers the data filter's
    # traversal / absolute-path / escaping-link / special-file rejections — so this
    # ONE except clause spans both corruption and hostility. ``lzma.LZMAError`` and
    # ``EOFError`` cover a corrupt/truncated xz container that ``r:xz`` cannot even
    # begin to read. A disk ``OSError`` is DELIBERATELY absent from the tuple → it
    # panics (the fetch.py boundary).
    try:
        with tarfile.open(archive, mode="r:xz") as tar:
            tar.extractall(dest_dir, filter=tarfile.data_filter)
    except (tarfile.TarError, lzma.LZMAError, EOFError) as exc:
        return _extract_error(archive, exc)

    return Ok(value=dest_dir)


def _extract_error(archive: Path, exc: Exception) -> Error:
    """Build the ``EXTRACT_FAILED`` ``Error`` for a caught archive failure.

    ONE ``ConditionKind`` (``EXTRACT_FAILED``) either way — corruption and
    hostility share the kind; the message and ``context`` carry the detail (the
    enum comment says do not speculatively enumerate; a distinct ``UNSAFE_ARCHIVE``
    kind is additive later if a consumer ever needs to branch on it).

    A ``FilterError`` (a rejected hostile member) gets a distinct "unsafe member"
    message and, when the offending member name is available, a ``member`` key in
    ``context``; every other caught failure gets the generic extract message. The
    ``archive`` path is always in ``context``.
    """
    context: dict[str, object] = {"archive": str(archive)}
    # ``FilterError`` may be absent on an unfilterable stdlib; ``getattr`` with the
    # empty tuple makes ``isinstance`` cleanly False in that case. (We only reach
    # here on Python that HAS ``data_filter``, so ``FilterError`` is present, but
    # the guard is defensive and free.)
    filter_error = getattr(tarfile, "FilterError", ())
    if isinstance(exc, filter_error):
        member = getattr(getattr(exc, "tarinfo", None), "name", None)
        if member is not None:
            context["member"] = member
        message = f"unsafe archive member rejected: {exc}"
    else:
        message = f"failed to extract archive: {exc}"
    return Error(
        conditions=(
            Condition(
                severity=Severity.ERROR,
                kind=ConditionKind.EXTRACT_FAILED,
                message=message,
                context=context,
            ),
        ),
    )
