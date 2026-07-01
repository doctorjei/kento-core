"""URL-rootfs helper — compose the two I/O leaves into a rootfs directory.

This is the single place the two Result-native I/O leaves are chained:

* ``kento.fetch.fetch_url`` streams an ``https://`` URL to a local ``.txz`` file;
* ``kento.extract.extract_txz`` unpacks that ``.txz`` SAFELY into a directory that
  becomes the VM's single overlay ``lowerdir`` (URL-VM Phase B, OPTION 2).

:func:`fetch_and_extract_rootfs` sequences them into ``fetch → extract → discard
the intermediate ``.txz`` → the rootfs dir``. It exists so that BOTH the honest
``LocalDirectoryImage.prepare`` primitive (``_images.py``) AND B3b's procedural
create branch call ONE implementation — the fetch→extract logic is never
duplicated (W2: honest primitive + procedural path share this helper).

**Boundary discipline is INHERITED from the leaves.** This helper adds NO new
``try``/``except`` around the network or the tar: the leaves already convert their
PREDICTABLE failures (a timeout / HTTP error / oversized body; a corrupt or
hostile archive) to an ``Error`` at their own boundary, and both let an
UNEXPECTED local-disk ``OSError`` propagate as a panic. This helper only
*sequences* the two Results and *merges* their conditions — so a fragment-drop
``Warning`` from the fetch survives to the final Result. The intermediate-``.txz``
``unlink`` is a disk op: a failure there is a disk fault → it PROPAGATES (panic),
exactly the boundary the leaves draw for their own write/unlink ``OSError``.

stdlib only (``pathlib``). This module is an INTERNAL helper (like
``fetch.py``/``extract.py``/``layers.py``): NOT re-exported from ``kento``; tests
and callers import ``from kento.urlvm import fetch_and_extract_rootfs``.

Spec: ``~/playbook/blocks/block-b3a-localdirimage-core.md`` (the LOCKED block
brief) + ``url-vm-source-design.md`` OPTION 2 LOCKED + ``kento-core-api-design.md``
§2 principle 5 (boundary-conversion) + ``result-type-design.md`` (the ``Result``
family; ``Result.of`` collapses a merged condition stack to the right subclass).
"""

from __future__ import annotations

from pathlib import Path

from kento._references import UrlReference
from kento._result import Result
from kento.extract import extract_txz
from kento.fetch import fetch_url

__all__ = ["fetch_and_extract_rootfs"]


def fetch_and_extract_rootfs(
    source: UrlReference, txz_dest: Path, rootfs_dir: Path
) -> Result[Path]:
    """Fetch ``source`` to ``txz_dest``, extract to ``rootfs_dir``, discard the ``.txz``.

    Returns a ``Result[Path]`` whose value (on success) is ``rootfs_dir``:

    * ``Ok(rootfs_dir)`` — fetched and extracted cleanly.
    * ``Warning(rootfs_dir, [FRAGMENT_DROPPED])`` — the fetch dropped a non-empty
      URL fragment (browser parity) but otherwise succeeded, and the extract was
      clean; the fetch's ``Warning`` condition survives the merge.
    * ``Error`` — a PREDICTABLE failure from EITHER leaf, propagated UNCHANGED:
      the fetch ``Error`` (``NON_HTTPS`` / ``FETCH_TIMEOUT`` / ``HTTP_ERROR`` /
      ``FETCH_FAILED`` / ``SIZE_EXCEEDED``) short-circuits (extract is NOT run),
      or the extract ``Error`` (``EXTRACT_FAILED``) when the fetch was clean.

    The intermediate ``.txz`` is DELETED only AFTER a successful extract (we keep
    just the extracted tree — ephemeral, space): on a fetch OR extract ``Error``
    it is left as-is. An UNEXPECTED local-disk ``OSError`` — from either leaf's
    own I/O, or from the ``.txz`` ``unlink`` here — is deliberately NOT caught: it
    propagates as a panic (an environmental fault, not the fetch/extract
    contract), consistent with the leaves' write-``OSError`` boundary. This helper
    adds NO ``try``/``except`` of its own.

    ``txz_dest`` is the intermediate file path and ``rootfs_dir`` the extraction
    directory; their common parent (the caller's per-instance container dir) must
    already exist — the fetcher writes the file, the extractor creates its own
    ``rootfs_dir``.
    """
    fetch = fetch_url(source, txz_dest)
    if not fetch.is_ok():
        # Propagate the fetch Error unchanged; do NOT run extract, and leave the
        # (partial/absent) txz alone — we only unlink after a SUCCESSFUL extract.
        return fetch

    extract = extract_txz(txz_dest, rootfs_dir)
    if not extract.is_ok():
        # Propagate the extract Error unchanged; the .txz is intentionally NOT
        # deleted (only removed after a successful extract, above).
        return extract

    # Extract succeeded → discard the intermediate .txz (keep only the tree). A
    # disk OSError here PROPAGATES (panic) — consistent with the leaves' unlink
    # boundary; no conversion.
    txz_dest.unlink(missing_ok=True)

    # Merge the two leaves' conditions so a fragment-drop Warning from the fetch
    # survives: Result.of collapses the stack to Ok (empty/NOTE) or Warning
    # (a WARNING present). An Error is impossible here — both is_ok() were True.
    return Result.of(rootfs_dir, fetch.conditions + extract.conditions)
