"""Mutation-proven suite for the ``.txz`` rootfs extractor (``kento.extract``).

Block B2-extract — the second I/O boundary-conversion site (sibling of
``test_fetch.py``). SAFETY is the heart: the archive bytes are untrusted and
extraction lands on the HOST filesystem before the KVM boundary, so a hostile
tarball must NOT be able to WRITE outside ``dest_dir``. We extract a real OS
rootfs (a filesystem IMAGE, not a data archive) with the PEP 706
``tarfile.tar_filter`` (``for_data=False``): it blocks every traversal-write
outside ``dest_dir`` but DELIBERATELY permits absolute symlink TARGETS and
special files, which are correct-by-construction in a rootfs and only ever
dereferenced inside the guest VM, never on the host. Asserts:

* happy path: a normal tree (incl. a legitimate rootfs relative symlink) →
  ``Ok(dest_dir)`` with exact bytes and the benign symlink preserved.
* the real-rootfs regression (run-41 E2E): a symlink whose TARGET is an ABSOLUTE
  path (e.g. ``sitecustomize.py -> /etc/python3.13/…``) now EXTRACTS cleanly —
  ``data_filter`` wrongly rejected these with ``AbsoluteLinkError``.
* the tar-slip guard (PEP 706 tar filter): ``../evil`` traversal, an absolute
  member NAME, and the link-then-write-through symlink escape each →
  ``Error(EXTRACT_FAILED)`` AND nothing is written outside ``dest_dir``.
* corruption: a truncated/garbage ``.txz`` and a valid-xz-of-non-tar payload →
  ``Error(EXTRACT_FAILED)``.
* the PANIC boundary: a disk ``OSError`` during extraction is NOT converted — it
  propagates (mirrors ``fetch.py``).
* the fail-closed panic: if ``tarfile.tar_filter`` is absent, ``extract_txz``
  RAISES ``RuntimeError`` and does NOT extract.

All ``.txz`` fixtures are BUILT IN-TEST with the stdlib ``tarfile`` writer — no
checked-in binaries. Each test is written so the corresponding mutation (drop the
``filter=`` arg, trust an unfiltered extract, swallow the disk OSError, skip the
fail-closed guard) reddens it.

Spec: ``~/playbook/blocks/block-b2-extract-core.md`` (LOCKED) + design-doc §2
principle 5 + ``url-vm-source-design.md`` OPTION 2 + ``result-type-design.md``.
"""

import io
import lzma
import tarfile
from pathlib import Path

import pytest

from kento import ConditionKind, Error, Ok, Severity
from kento.extract import extract_txz


# --------------------------------------------------------------------------- #
# Fixture builders — construct small `.txz` archives in-test (no binaries).
# --------------------------------------------------------------------------- #


def _add_file(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    """Add a regular file member ``name`` with contents ``data``."""
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def _add_symlink(tar: tarfile.TarFile, name: str, target: str) -> None:
    """Add a symlink member ``name`` -> ``target`` (no payload)."""
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    tar.addfile(info)


def _make_txz(path: Path, build) -> Path:
    """Write a ``.txz`` at ``path``; ``build(tar)`` populates it. Returns ``path``."""
    with tarfile.open(path, mode="w:xz") as tar:
        build(tar)
    return path


# --------------------------------------------------------------------------- #
# happy path — a normal rootfs-style tree extracts cleanly into dest_dir.
# --------------------------------------------------------------------------- #


def test_happy_path_extracts_tree_with_bytes_and_symlink(tmp_path):
    kernel = b"\x7fELF fake vmlinuz bytes" * 10
    hostname = b"gemet\n"

    def build(tar):
        _add_file(tar, "./boot/vmlinuz", kernel)
        _add_file(tar, "./etc/hostname", hostname)
        # A legitimate merged-usr rootfs relative symlink (lib -> usr/lib). The
        # data filter must NOT over-reject this — merged-usr rootfs REQUIRE it.
        _add_symlink(tar, "./lib", "usr/lib")

    archive = _make_txz(tmp_path / "rootfs.txz", build)
    dest = tmp_path / "root"

    result = extract_txz(archive, dest)

    assert isinstance(result, Ok)
    assert result.unwrap() == dest  # the return value IS dest_dir
    # extracted DIRECTLY into dest (no top-level-dir stripping)
    assert (dest / "boot" / "vmlinuz").read_bytes() == kernel
    assert (dest / "etc" / "hostname").read_bytes() == hostname
    # the benign relative symlink survived the filter
    link = dest / "lib"
    assert link.is_symlink()
    assert Path(link.readlink()) == Path("usr/lib")


def test_dest_dir_is_created(tmp_path):
    """The extractor creates dest_dir itself (its parent already exists)."""
    def build(tar):
        _add_file(tar, "./etc/hostname", b"x\n")

    archive = _make_txz(tmp_path / "rootfs.txz", build)
    dest = tmp_path / "fresh-dir"  # does not exist yet
    assert not dest.exists()

    result = extract_txz(archive, dest)

    assert isinstance(result, Ok)
    assert dest.is_dir()
    assert (dest / "etc" / "hostname").read_bytes() == b"x\n"


# --------------------------------------------------------------------------- #
# real-rootfs regression (run-41 E2E): an ABSOLUTE-target symlink extracts.
# This is the whole point of the data_filter -> tar_filter switch. A real OS
# rootfs is full of legitimate absolute symlinks (gemet's bifrost-1.7.3.txz has
# 593); ``data_filter`` raised ``AbsoluteLinkError`` on every one of them and the
# URL-VM E2E failed at extract. ``tar_filter`` permits them: they are never
# dereferenced on the host, only inside the guest where the target resolves.
# Mutation guard: reverting to ``tarfile.data_filter`` reddens this.
# --------------------------------------------------------------------------- #


def test_absolute_symlink_target_extracts_cleanly(tmp_path):
    def build(tar):
        _add_file(tar, "./etc/hostname", b"gemet\n")
        # The exact shape that broke the E2E: a member whose symlink TARGET is an
        # absolute path pointing at a location that only exists inside the guest.
        _add_symlink(
            tar, "./usr/lib/python3.13/sitecustomize.py",
            "/etc/python3.13/sitecustomize.py",
        )
        # And a masked-unit style ``-> /dev/null`` absolute link.
        _add_symlink(tar, "./etc/systemd/system/foo.service", "/dev/null")

    archive = _make_txz(tmp_path / "rootfs.txz", build)
    dest = tmp_path / "root"

    result = extract_txz(archive, dest)

    assert isinstance(result, Ok)
    assert result.unwrap() == dest
    link = dest / "usr" / "lib" / "python3.13" / "sitecustomize.py"
    assert link.is_symlink()
    # the absolute target is preserved verbatim (NOT rewritten/rejected)
    assert Path(link.readlink()) == Path("/etc/python3.13/sitecustomize.py")
    masked = dest / "etc" / "systemd" / "system" / "foo.service"
    assert masked.is_symlink()
    assert Path(masked.readlink()) == Path("/dev/null")


# --------------------------------------------------------------------------- #
# tar-slip: path traversal, absolute NAME, symlink escape → Error, no host write.
# Mutation guard: dropping `filter=tarfile.tar_filter` reddens these.
# --------------------------------------------------------------------------- #


def test_path_traversal_rejected(tmp_path):
    # A member climbing out of dest_dir with ``..``. The tar filter rejects it
    # (``OutsideDestinationError`` ⊆ ``FilterError`` ⊆ ``TarError``).
    escape_target = tmp_path / "evil"  # the parent of dest_dir

    def build(tar):
        _add_file(tar, "../evil", b"pwned")

    archive = _make_txz(tmp_path / "malicious.txz", build)
    dest = tmp_path / "root"

    result = extract_txz(archive, dest)

    assert isinstance(result, Error)
    assert [c.kind for c in result.conditions] == [ConditionKind.EXTRACT_FAILED]
    assert result.conditions[0].severity is Severity.ERROR
    # NOTHING written at the escape target
    assert not escape_target.exists()


def test_absolute_path_contained_not_escaped(tmp_path):
    # An absolute member NAME ``/etc/kento-pwned``. The PEP 706 tar filter's
    # documented handling is to STRIP the leading slash and extract it SAFELY
    # INSIDE dest_dir (root/etc/kento-pwned) — it does NOT raise, because the
    # result never leaves the destination. (This is an absolute member NAME, NOT
    # an absolute symlink TARGET — tar_filter contains the former and permits the
    # latter; see test_absolute_symlink_target_extracts_cleanly.) SAFETY — no host
    # write outside dest_dir — is met. We assert the safe outcome, not a rejection.
    host_target = Path("/etc/kento-pwned")  # must never be touched
    assert not host_target.exists()

    def build(tar):
        _add_file(tar, "/etc/kento-pwned", b"pwned")

    archive = _make_txz(tmp_path / "abs.txz", build)
    dest = tmp_path / "root"

    result = extract_txz(archive, dest)

    # Contained inside dest_dir (leading slash stripped) — a clean Ok.
    assert isinstance(result, Ok)
    assert (dest / "etc" / "kento-pwned").read_bytes() == b"pwned"
    # the real host /etc was NOT written outside dest_dir
    assert not host_target.exists()
    # nothing landed above dest_dir either
    assert not (tmp_path / "etc").exists()


def test_symlink_escape_rejected(tmp_path):
    # Classic link-then-write-through: a symlink ``evil`` -> an absolute dir,
    # followed by a member ``evil/x`` that would write THROUGH the link outside
    # dest_dir. The tar filter rejects the escaping link (a link whose target
    # escapes the destination), even though it permits non-escaping absolute
    # symlink targets — the escape is the write-outside-dest threat.
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "x"

    def build(tar):
        _add_symlink(tar, "evil", str(outside))  # absolute escaping link
        _add_file(tar, "evil/x", b"pwned")

    archive = _make_txz(tmp_path / "linkescape.txz", build)
    dest = tmp_path / "root"

    result = extract_txz(archive, dest)

    assert isinstance(result, Error)
    assert [c.kind for c in result.conditions] == [ConditionKind.EXTRACT_FAILED]
    # nothing written THROUGH the link, outside dest_dir
    assert not sentinel.exists()


def test_filter_error_carries_member_context(tmp_path):
    # A rejected hostile member gets the "unsafe member" message and the offending
    # member name in context (distinguished from plain corruption).
    def build(tar):
        _add_file(tar, "../evil", b"pwned")

    archive = _make_txz(tmp_path / "malicious.txz", build)
    result = extract_txz(archive, tmp_path / "root")

    assert isinstance(result, Error)
    cond = result.conditions[0]
    assert cond.kind is ConditionKind.EXTRACT_FAILED
    assert "unsafe archive member" in cond.message
    assert cond.context["archive"] == str(archive)
    assert cond.context["member"] == "../evil"


# --------------------------------------------------------------------------- #
# corruption — a bad xz container / a valid-xz-of-non-tar payload → Error.
# --------------------------------------------------------------------------- #


def test_corrupt_xz_rejected(tmp_path):
    archive = tmp_path / "corrupt.txz"
    archive.write_bytes(b"this is not a valid xz stream at all")

    result = extract_txz(archive, tmp_path / "root")

    assert isinstance(result, Error)
    assert [c.kind for c in result.conditions] == [ConditionKind.EXTRACT_FAILED]
    assert result.conditions[0].context["archive"] == str(archive)
    assert "failed to extract archive" in result.conditions[0].message


def test_truncated_xz_rejected(tmp_path):
    # A valid xz-of-tar, then chopped in half → the reader hits a truncated stream.
    def build(tar):
        _add_file(tar, "./etc/hostname", b"gemet\n" * 5000)

    full = _make_txz(tmp_path / "full.txz", build)
    data = full.read_bytes()
    truncated = tmp_path / "truncated.txz"
    truncated.write_bytes(data[: len(data) // 2])

    result = extract_txz(truncated, tmp_path / "root")

    assert isinstance(result, Error)
    assert [c.kind for c in result.conditions] == [ConditionKind.EXTRACT_FAILED]


def test_gzip_tar_named_txz_rejected_pins_r_xz(tmp_path):
    # The format is LOCKED to xz: we open ``mode="r:xz"`` so a NON-xz tar (here a
    # gzip-compressed tar merely NAMED .txz) fails closed rather than being
    # silently accepted. A ``r:xz`` opener raises ReadError ⊆ TarError on a gzip
    # stream → EXTRACT_FAILED. Mutation guard: relaxing ``r:xz`` to ``r:*`` would
    # ACCEPT this gzip tar → this test reddens.
    archive = tmp_path / "actually-gzip.txz"
    with tarfile.open(archive, mode="w:gz") as tar:
        _add_file(tar, "./etc/hostname", b"gemet\n")

    result = extract_txz(archive, tmp_path / "root")

    assert isinstance(result, Error)
    assert [c.kind for c in result.conditions] == [ConditionKind.EXTRACT_FAILED]
    assert result.conditions[0].context["archive"] == str(archive)


def test_valid_xz_non_tar_payload_rejected(tmp_path):
    # A well-formed xz stream whose decompressed payload is NOT a tar.
    archive = tmp_path / "notar.txz"
    archive.write_bytes(lzma.compress(b"just some plain text, definitely not a tar"))

    result = extract_txz(archive, tmp_path / "root")

    assert isinstance(result, Error)
    assert [c.kind for c in result.conditions] == [ConditionKind.EXTRACT_FAILED]


# --------------------------------------------------------------------------- #
# the PANIC boundary — a disk OSError during extraction is NOT converted.
# Mutation guard: adding `except OSError -> Error` reddens this.
# --------------------------------------------------------------------------- #


def test_disk_oserror_during_extract_propagates_as_panic(monkeypatch, tmp_path):
    # A valid archive; patch TarFile.extractall to raise OSError (disk-full,
    # permission-denied). The extractor must NOT convert it to an Error — it
    # propagates.
    def build(tar):
        _add_file(tar, "./etc/hostname", b"x\n")

    archive = _make_txz(tmp_path / "rootfs.txz", build)

    def _boom(self, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(tarfile.TarFile, "extractall", _boom)

    with pytest.raises(OSError):
        extract_txz(archive, tmp_path / "root")


def test_mkdir_oserror_propagates_as_panic(monkeypatch, tmp_path):
    # A disk OSError while creating dest_dir is likewise environmental → panic.
    def build(tar):
        _add_file(tar, "./etc/hostname", b"x\n")

    archive = _make_txz(tmp_path / "rootfs.txz", build)

    real_mkdir = Path.mkdir

    def _boom(self, *a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "mkdir", _boom)

    with pytest.raises(OSError):
        extract_txz(archive, tmp_path / "root")

    monkeypatch.setattr(Path, "mkdir", real_mkdir)  # restore for cleanliness


def test_missing_archive_propagates_as_panic(tmp_path):
    # A missing archive file surfaces as FileNotFoundError (subclass of OSError)
    # → propagates (an internal invariant breach; the fetcher guarantees the file).
    missing = tmp_path / "does-not-exist.txz"
    with pytest.raises(OSError):
        extract_txz(missing, tmp_path / "root")


# --------------------------------------------------------------------------- #
# fail-closed panic — no tar filter → RuntimeError, and NO extraction attempted.
# Mutation guard: dropping the hasattr guard would let this reach extractall.
# --------------------------------------------------------------------------- #


def test_tar_filter_absent_fails_closed(monkeypatch, tmp_path):
    def build(tar):
        _add_file(tar, "./etc/hostname", b"x\n")

    archive = _make_txz(tmp_path / "rootfs.txz", build)
    dest = tmp_path / "root"

    # Assert extractall is NEVER reached: patch it to fail loudly.
    def _must_not_run(self, *a, **k):
        raise AssertionError("extractall called despite absent tar filter")

    monkeypatch.setattr(tarfile.TarFile, "extractall", _must_not_run)
    monkeypatch.delattr(tarfile, "tar_filter", raising=False)

    with pytest.raises(RuntimeError, match="tar filter"):
        extract_txz(archive, dest)

    # and it must NOT return an Error, and dest_dir must not have been created
    assert not dest.exists()


# --------------------------------------------------------------------------- #
# the new ConditionKind member.
# --------------------------------------------------------------------------- #


def test_extract_failed_kind_value():
    assert ConditionKind.EXTRACT_FAILED.value == "extract_failed"
