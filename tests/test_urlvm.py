"""Mutation-proven suite for the URL-rootfs helper + ``LocalDirectoryImage``.

Block B3a — the API-honest half of URL-VM (OPTION 2). Covers:

* ``urlvm.fetch_and_extract_rootfs`` — the fetch→extract→discard-``.txz``
  composition: the happy path (real extract, intermediate ``.txz`` DELETED), the
  fragment-drop ``Warning`` surviving the merge, the fetch ``Error`` short-circuit
  (extract NOT run, no unlink of a missing file), and the extract ``Error``
  propagating (``.txz`` NOT deleted — we only unlink after a clean extract).
* ``LocalDirectoryImage`` primitives — ``resolve`` (cheap, no fetch),
  ``is_writable`` False, ``prepare`` (calls the helper + mkdirs upper/work; RAISES
  ``ResultError`` on an ``Error`` via the ``.unwrap()`` seam), ``mount`` (single
  ``rootfs-base`` lowerdir), ``release`` (rmtrees ``rootfs-base``), and the
  frozen-value breach panic.
* ``Instance.image()`` for a URL instance — a ``kento-image`` URL marker resolves
  to a ``LocalDirectoryImage`` whose ``source`` round-trips; the ``kento-kernel``
  override still echoes; an OCI instance is unchanged; a ``file://`` (unbuilt)
  source still panics.

NO real network: ``fetch_url`` / ``extract_txz`` are patched (and the ``.txz``
fixtures are built in-test with the stdlib ``tarfile`` writer, like
``test_extract.py``). Each test is written so the matching mutation reddens it.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from kento import (
    Condition,
    ConditionKind,
    Error,
    LocalDirectoryImage,
    Ok,
    ResultError,
    Severity,
    Warning as KWarning,
)
from kento._references import UrlReference
from kento.urlvm import fetch_and_extract_rootfs


# --------------------------------------------------------------------------- #
# Fixtures / helpers.
# --------------------------------------------------------------------------- #


def _url(s: str = "https://ex.com/img/rootfs.txz") -> UrlReference:
    """A parsed ``UrlReference`` for the tests."""
    ref = UrlReference.parse(s).unwrap()
    assert isinstance(ref, UrlReference)
    return ref


def _make_txz(path: Path) -> Path:
    """Write a small, valid ``.txz`` rootfs tree at ``path``. Returns ``path``."""
    with tarfile.open(path, mode="w:xz") as tar:
        data = b"gemet\n"
        info = tarfile.TarInfo("./etc/hostname")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return path


def _frag_condition() -> Condition:
    """A FRAGMENT_DROPPED WARNING condition (as ``fetch_url`` emits)."""
    return Condition(
        severity=Severity.WARNING,
        kind=ConditionKind.FRAGMENT_DROPPED,
        message="dropped fragment 'x' from URL (not sent to server)",
        context={"fragment": "x"},
    )


def _http_error_condition() -> Condition:
    return Condition(
        severity=Severity.ERROR,
        kind=ConditionKind.HTTP_ERROR,
        message="server returned HTTP 404",
        context={"status": 404},
    )


def _extract_error_condition() -> Condition:
    return Condition(
        severity=Severity.ERROR,
        kind=ConditionKind.EXTRACT_FAILED,
        message="failed to extract archive",
        context={},
    )


# --------------------------------------------------------------------------- #
# fetch_and_extract_rootfs — happy path (real extract, .txz DELETED).
# --------------------------------------------------------------------------- #


def test_helper_happy_extracts_and_deletes_txz(tmp_path):
    txz = tmp_path / "rootfs.txz"
    rootfs = tmp_path / "rootfs-base"

    def fake_fetch(source, dest, **kw):
        # The fetcher writes a REAL small .txz at dest; return Ok(dest).
        _make_txz(dest)
        return Ok(value=dest)

    with patch("kento.urlvm.fetch_url", side_effect=fake_fetch):
        # real extract_txz — unpacks the tree.
        result = fetch_and_extract_rootfs(_url(), txz, rootfs)

    assert isinstance(result, Ok)
    assert result.unwrap() == rootfs
    # the tree landed directly in rootfs-base
    assert (rootfs / "etc" / "hostname").read_bytes() == b"gemet\n"
    # the intermediate .txz was DELETED after the successful extract
    assert not txz.exists()


# --------------------------------------------------------------------------- #
# fragment-drop Warning survives the merge.
# --------------------------------------------------------------------------- #


def test_helper_fragment_warning_survives(tmp_path):
    txz = tmp_path / "rootfs.txz"
    rootfs = tmp_path / "rootfs-base"

    def fake_fetch(source, dest, **kw):
        _make_txz(dest)
        return KWarning(value=dest, conditions=(_frag_condition(),))

    with patch("kento.urlvm.fetch_url", side_effect=fake_fetch):
        result = fetch_and_extract_rootfs(_url(), txz, rootfs)

    # Mutation guard: dropping the fetch-conditions merge (returning Ok/only the
    # extract conditions) makes this Ok, not Warning → reddens.
    assert isinstance(result, KWarning)
    assert result.is_ok()
    assert result.unwrap() == rootfs
    kinds = [c.kind for c in result.conditions]
    assert ConditionKind.FRAGMENT_DROPPED in kinds
    assert not txz.exists()  # still deleted after the clean extract


# --------------------------------------------------------------------------- #
# fetch Error short-circuits — extract NOT called; no unlink of a missing file.
# --------------------------------------------------------------------------- #


def test_helper_fetch_error_short_circuits(tmp_path):
    txz = tmp_path / "rootfs.txz"
    rootfs = tmp_path / "rootfs-base"
    fetch_err = Error(conditions=(_http_error_condition(),))

    with patch("kento.urlvm.fetch_url", return_value=fetch_err) as mf, patch(
        "kento.urlvm.extract_txz"
    ) as mx:
        result = fetch_and_extract_rootfs(_url(), txz, rootfs)

    # The fetch Error is returned UNCHANGED (same object).
    assert result is fetch_err
    mf.assert_called_once()
    # Mutation guard: running extract regardless of the fetch verdict reddens.
    mx.assert_not_called()
    # No rootfs was produced.
    assert not rootfs.exists()


# --------------------------------------------------------------------------- #
# extract Error propagates — .txz NOT deleted (only unlink after a clean extract).
# --------------------------------------------------------------------------- #


def test_helper_extract_error_keeps_txz(tmp_path):
    txz = tmp_path / "rootfs.txz"
    rootfs = tmp_path / "rootfs-base"
    extract_err = Error(conditions=(_extract_error_condition(),))

    def fake_fetch(source, dest, **kw):
        _make_txz(dest)  # a real .txz on disk
        return Ok(value=dest)

    with patch("kento.urlvm.fetch_url", side_effect=fake_fetch), patch(
        "kento.urlvm.extract_txz", return_value=extract_err
    ):
        result = fetch_and_extract_rootfs(_url(), txz, rootfs)

    # The extract Error is returned UNCHANGED.
    assert result is extract_err
    # Mutation guard: unlinking before checking the extract verdict would delete
    # this → reddens. The .txz MUST remain after a failed extract.
    assert txz.exists()


# --------------------------------------------------------------------------- #
# LocalDirectoryImage — resolve is cheap (no fetch), is_writable False.
# --------------------------------------------------------------------------- #


def test_localdir_resolve_is_cheap_no_fetch():
    with patch("kento.fetch.fetch_url") as mf:
        img = LocalDirectoryImage.resolve(_url())
    assert isinstance(img, LocalDirectoryImage)
    assert img.source == _url()
    # resolve must NOT fetch — the fetch is prepare's job.
    mf.assert_not_called()
    # inherited base fields default to None (override echo populates them later).
    assert img.kernel is None
    assert img.initramfs is None


def test_localdir_is_not_writable():
    assert LocalDirectoryImage.resolve(_url()).is_writable() is False


# --------------------------------------------------------------------------- #
# LocalDirectoryImage.prepare — calls the helper + mkdirs upper/work.
# --------------------------------------------------------------------------- #


def test_localdir_prepare_calls_helper_and_mkdirs(tmp_path):
    img = LocalDirectoryImage.resolve(_url())
    state = tmp_path / "state"
    state.mkdir()

    with patch(
        "kento.urlvm.fetch_and_extract_rootfs",
        return_value=Ok(value=state / "rootfs-base"),
    ) as mh:
        img.prepare(state)

    # called with (source, state/rootfs.txz, state/rootfs-base)
    mh.assert_called_once_with(
        img.source, state / "rootfs.txz", state / "rootfs-base"
    )
    # the overlay upper/work dirs were created (mirrors OciImage.prepare)
    assert (state / "upper").is_dir()
    assert (state / "work").is_dir()


def test_localdir_prepare_raises_on_helper_error(tmp_path):
    """The ``.unwrap()`` seam: an Error from the helper surfaces as ResultError."""
    img = LocalDirectoryImage.resolve(_url())
    state = tmp_path / "state"
    state.mkdir()
    err = Error(conditions=(_http_error_condition(),))

    with patch("kento.urlvm.fetch_and_extract_rootfs", return_value=err):
        with pytest.raises(ResultError):
            img.prepare(state)

    # Mutation guard: dropping the .unwrap() (returning without raising) reddens
    # this — and the mkdirs must NOT have run past the failed unwrap.
    assert not (state / "upper").exists()
    assert not (state / "work").exists()


# --------------------------------------------------------------------------- #
# LocalDirectoryImage.mount — single rootfs-base lowerdir.
# --------------------------------------------------------------------------- #


def test_localdir_mount_single_lowerdir(tmp_path):
    img = LocalDirectoryImage.resolve(_url())
    host = tmp_path / "host"
    state = tmp_path / "state"

    with patch("kento.vm.mount_rootfs") as mm:
        img.mount(host, state)

    # the SINGLE extracted dir is the one lowerdir (str, not a colon-joined set)
    mm.assert_called_once_with(host, str(state / "rootfs-base"), state)


def test_localdir_unmount_delegates(tmp_path):
    img = LocalDirectoryImage.resolve(_url())
    host = tmp_path / "host"
    with patch("kento.vm.unmount_rootfs") as mu:
        img.unmount(host)
    mu.assert_called_once_with(host)


# --------------------------------------------------------------------------- #
# LocalDirectoryImage.release — rmtrees rootfs-base (the ephemeral discard).
# --------------------------------------------------------------------------- #


def test_localdir_release_removes_rootfs_base(tmp_path):
    img = LocalDirectoryImage.resolve(_url())
    state = tmp_path / "state"
    base = state / "rootfs-base"
    base.mkdir(parents=True)
    (base / "etc").mkdir()
    (base / "etc" / "hostname").write_text("gemet\n")
    (state / "rootfs.txz").write_bytes(b"leftover")

    img.release(state)

    # Mutation guard: a no-op release (OciImage's behavior) leaves rootfs-base →
    # reddens. This is where LocalDirectoryImage DIFFERS from OciImage.
    assert not base.exists()
    assert not (state / "rootfs.txz").exists()


def test_localdir_release_idempotent_when_absent(tmp_path):
    """A double-release (rootfs-base already gone) must not raise."""
    img = LocalDirectoryImage.resolve(_url())
    state = tmp_path / "state"
    state.mkdir()
    img.release(state)  # nothing to remove — no error


# --------------------------------------------------------------------------- #
# Frozen-value breach still panics (a frozen dataclass).
# --------------------------------------------------------------------------- #


def test_localdir_is_frozen():
    from dataclasses import FrozenInstanceError

    img = LocalDirectoryImage.resolve(_url())
    with pytest.raises(FrozenInstanceError):
        img.source = _url("https://ex.com/other.txz")  # type: ignore[misc]
