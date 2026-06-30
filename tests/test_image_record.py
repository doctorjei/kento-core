"""Tests for the typed ``ImageRecord`` ledger (storage-depth SD3, JC1).

``ImageRecord`` is kento's typed LEDGER ENTRY about an image (§12.4) — the typed
projection of kento's own markers (per-guest ``kento-image`` refs + the
``kento-hold.<guest>`` holds from SD2), keyed on content **id**. It REPLACES the
string-returning ``images.list_images()`` (removed; the CLI now formats the typed
records). These tests cover:

* the value type + frozen-ness + tuple coercion;
* the derived props (held / in_use / dangling / status) and their orthogonality;
* ``ImageRecord.list()`` — in-use, orphaned, dangling-but-held, multi-tagged,
  multi-guest, id-grouping (two tags / a guest+hold collapsing to one id),
  stable ordering, and the log+skip of an unresolvable legacy tag-only hold and
  an unresolvable guest reference (id mandatory);
* mutation guards — break id-grouping or the ``dangling = not refs`` rule and a
  test reddens;
* ``ImageRecord.get`` — all three input forms (str / Digest / OciReference) and
  a not-managed raise;
* ``image.record()`` — TOTAL (empty, never None) + the ``record().resolve()``
  round-trip.

All podman / disk I/O is mocked. The flat re-export ``kento.ImageRecord`` /
``kento.ManagedStatus`` are the canonical paths.
"""

import dataclasses
from unittest.mock import patch

import pytest

import kento
from kento import (
    Digest,
    Hold,
    ImageRecord,
    ManagedStatus,
    OciReference,
)
from kento.errors import ImageNotFoundError

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _digest(hex64):
    return Digest(algorithm="sha256", encoded=hex64)


def _ref(s):
    return OciReference.parse(s).unwrap()


# --------------------------------------------------------------------------- #
# Flat re-export + value-type shape.
# --------------------------------------------------------------------------- #


def test_flat_reexports():
    assert kento.ImageRecord is ImageRecord
    assert kento.ManagedStatus is ManagedStatus
    assert "ImageRecord" in kento.__all__
    assert "ManagedStatus" in kento.__all__


def test_managed_status_wire_values():
    """2-state enum, members serialize as today's table STATUS strings."""
    assert ManagedStatus.IN_USE == "in-use"
    assert ManagedStatus.ORPHANED == "orphaned"
    assert {s.value for s in ManagedStatus} == {"in-use", "orphaned"}


def test_record_is_frozen_and_kw_only():
    rec = ImageRecord(id=_digest(SHA_A))
    with pytest.raises(dataclasses.FrozenInstanceError):
        rec.id = _digest(SHA_B)  # type: ignore[misc]
    with pytest.raises(TypeError):
        ImageRecord(_digest(SHA_A))  # type: ignore[misc]  positional


def test_record_id_mandatory():
    """id has no default — a record cannot exist without a content id."""
    with pytest.raises(TypeError):
        ImageRecord()  # type: ignore[call-arg]


def test_record_coerces_iterables_to_tuples():
    rec = ImageRecord(
        id=_digest(SHA_A),
        refs=[_ref("a:1")],
        guests=["g"],
        holds=[Hold(instance="g", pinned=_digest(SHA_A))],
    )
    assert isinstance(rec.refs, tuple)
    assert isinstance(rec.guests, tuple)
    assert isinstance(rec.holds, tuple)


# --------------------------------------------------------------------------- #
# Derived properties — held / in_use / dangling / status (orthogonality).
# --------------------------------------------------------------------------- #


def test_props_in_use_tagged():
    rec = ImageRecord(id=_digest(SHA_A), refs=(_ref("a:1"),), guests=("g",))
    assert rec.in_use is True
    assert rec.held is False
    assert rec.dangling is False
    assert rec.status is ManagedStatus.IN_USE


def test_props_orphaned_but_tagged():
    """ORPHANED (no guest) but NOT dangling (still tagged) — orthogonal axes."""
    rec = ImageRecord(
        id=_digest(SHA_A),
        refs=(_ref("a:1"),),
        holds=(Hold(instance="ghost", pinned=_digest(SHA_A)),),
    )
    assert rec.in_use is False
    assert rec.held is True
    assert rec.dangling is False
    assert rec.status is ManagedStatus.ORPHANED


def test_props_dangling_but_held():
    """No tag (dangling) AND no guest (orphaned), pinned by a hold."""
    rec = ImageRecord(
        id=_digest(SHA_A),
        holds=(Hold(instance="ghost", pinned=_digest(SHA_A)),),
    )
    assert rec.dangling is True
    assert rec.held is True
    assert rec.in_use is False
    assert rec.status is ManagedStatus.ORPHANED


def test_props_empty_record():
    rec = ImageRecord(id=_digest(SHA_A))
    assert rec.dangling is True
    assert rec.held is False
    assert rec.in_use is False
    assert rec.status is ManagedStatus.ORPHANED


# --------------------------------------------------------------------------- #
# ImageRecord.list() — reconstruct from kento markers.
# --------------------------------------------------------------------------- #


def _patch_list(guest_refs, holds, id_map):
    """Patch the three I/O seams ImageRecord.list() composes.

    guest_refs: {image_str: [guest, ...]}  (images._guest_image_refs result)
    holds:      list[Hold]                 (Hold.list result)
    id_map:     {ref_string: hex64 | None} — None => ImageNotFoundError (gone).
    """
    def _resolve_id(ref):
        val = id_map.get(ref)
        if val is None:
            raise ImageNotFoundError(f"gone: {ref}")
        return _digest(val)

    return (
        patch("kento.images._guest_image_refs", return_value=guest_refs),
        # ImageRecord._list calls the RAISING Hold._list (kind fidelity), so the
        # internal seam to patch is _list, not the public Result-returning list.
        patch.object(Hold, "_list", classmethod(lambda cls: list(holds))),
        patch("kento._images.OciImage.resolve_id", staticmethod(_resolve_id)),
    )


def test_list_empty():
    g, h, i = _patch_list({}, [], {})
    with g, h, i:
        assert ImageRecord.list().unwrap() == []


def test_list_in_use_with_hold():
    """A guest-referenced, held image: in-use, tagged, held."""
    g, h, i = _patch_list(
        {"imagea:latest": ["box"]},
        [Hold(instance="box", pinned=_digest(SHA_A))],
        {"imagea:latest": SHA_A},
    )
    with g, h, i:
        recs = ImageRecord.list().unwrap()
    assert len(recs) == 1
    rec = recs[0]
    assert rec.id == _digest(SHA_A)
    assert rec.guests == ("box",)
    assert rec.refs == (_ref("imagea:latest"),)
    assert rec.held is True
    assert rec.status is ManagedStatus.IN_USE


def test_list_orphaned_hold_dangling():
    """A modern hold for a vanished guest, no tag — dangling-but-held, orphaned."""
    g, h, i = _patch_list(
        {},
        [Hold(instance="ghost", pinned=_digest(SHA_B))],
        {},
    )
    with g, h, i:
        recs = ImageRecord.list().unwrap()
    assert len(recs) == 1
    rec = recs[0]
    assert rec.id == _digest(SHA_B)
    assert rec.guests == ()
    assert rec.refs == ()
    assert rec.dangling is True
    assert rec.status is ManagedStatus.ORPHANED


def test_list_groups_guest_and_hold_by_id():
    """A guest ref AND a hold resolving to the SAME id collapse to ONE record."""
    g, h, i = _patch_list(
        {"imagea:latest": ["box"]},
        [Hold(instance="box", pinned=_digest(SHA_A))],  # same id as imageA
        {"imagea:latest": SHA_A},
    )
    with g, h, i:
        recs = ImageRecord.list().unwrap()
    assert len(recs) == 1  # not two
    assert recs[0].guests == ("box",)
    assert len(recs[0].holds) == 1


def test_list_multi_tagged_same_id():
    """Two distinct tags resolving to one id => one record, two refs."""
    g, h, i = _patch_list(
        {"imagea:latest": ["box"], "imagea:1.0": ["other"]},
        [],
        {"imagea:latest": SHA_A, "imagea:1.0": SHA_A},
    )
    with g, h, i:
        recs = ImageRecord.list().unwrap()
    assert len(recs) == 1
    rendered = {r.render() for r in recs[0].refs}
    assert rendered == {"imagea:latest", "imagea:1.0"}
    assert set(recs[0].guests) == {"box", "other"}


def test_list_multi_guest_same_tag():
    """Several guests on the same tag => one record, guests merged + sorted."""
    g, h, i = _patch_list(
        {"imagea:latest": ["zebra", "alpha"]},
        [],
        {"imagea:latest": SHA_A},
    )
    with g, h, i:
        recs = ImageRecord.list().unwrap()
    assert recs[0].guests == ("alpha", "zebra")


def test_list_skips_unresolvable_guest_ref(caplog):
    """A guest ref whose content is GONE is logged + skipped (id mandatory)."""
    g, h, i = _patch_list(
        {"gone:tag": ["box"], "imagea:latest": ["live"]},
        [],
        {"imagea:latest": SHA_A},  # gone:tag absent => ImageNotFoundError
    )
    with g, h, i:
        recs = ImageRecord.list().unwrap()
    assert len(recs) == 1
    assert recs[0].id == _digest(SHA_A)
    assert recs[0].guests == ("live",)


def test_list_skips_unresolvable_legacy_tag_hold(caplog):
    """A LEGACY tag-pin hold whose content is gone is logged + skipped."""
    g, h, i = _patch_list(
        {},
        [Hold(instance="ghost", pinned=_ref("gone:tag"))],
        {},  # gone:tag unresolvable
    )
    with g, h, i:
        recs = ImageRecord.list().unwrap()
    assert recs == []


def test_list_legacy_tag_hold_resolves_when_present():
    """A LEGACY tag-pin hold whose content IS local resolves to the id."""
    g, h, i = _patch_list(
        {},
        [Hold(instance="ghost", pinned=_ref("imagea:latest"))],
        {"imagea:latest": SHA_A},
    )
    with g, h, i:
        recs = ImageRecord.list().unwrap()
    assert len(recs) == 1
    assert recs[0].id == _digest(SHA_A)
    assert recs[0].held is True
    assert recs[0].dangling is True  # the hold's tag is not a guest "seen tag"


def test_list_bare_hex_guest_ref_is_id_not_tag():
    """A bare-hex guest image string is the id itself, no podman resolve, no ref."""
    g, h, i = _patch_list(
        {SHA_A: ["box"]},
        [],
        {},  # no resolve_id call expected (bare hex IS the id)
    )
    with g, h, i:
        recs = ImageRecord.list().unwrap()
    assert len(recs) == 1
    assert recs[0].id == _digest(SHA_A)
    assert recs[0].refs == ()  # a bare id is not a "seen tag"
    assert recs[0].guests == ("box",)


def test_list_sorted_by_id():
    """Stable, deterministic ordering: sorted by rendered content id."""
    g, h, i = _patch_list(
        {"z:1": ["g1"], "a:1": ["g2"], "m:1": ["g3"]},
        [],
        {"z:1": SHA_C, "a:1": SHA_A, "m:1": SHA_B},
    )
    with g, h, i:
        recs = ImageRecord.list().unwrap()
    assert [r.id.encoded for r in recs] == [SHA_A, SHA_B, SHA_C]


# --------------------------------------------------------------------------- #
# Mutation guards.
# --------------------------------------------------------------------------- #


def test_mutation_grouping_collapses_distinct_ids():
    """MUTATION: if grouping keyed on the TAG (not the id), a guest+hold that
    share an id but differ in tag/instance would NOT collapse. We assert the
    id-keyed collapse: one id, one record — break _bucket's id key and this
    reddens (two records, or a wrong guest/hold split)."""
    g, h, i = _patch_list(
        {"imagea:latest": ["box"]},
        [Hold(instance="box", pinned=_digest(SHA_A))],
        {"imagea:latest": SHA_A},
    )
    with g, h, i:
        recs = ImageRecord.list().unwrap()
    assert len(recs) == 1
    assert recs[0].in_use and recs[0].held


def test_mutation_dangling_is_not_refs():
    """MUTATION: dangling must be `not refs`. A tagged record is NOT dangling;
    an untagged held record IS. Break the guard and one of these reddens."""
    tagged = ImageRecord(id=_digest(SHA_A), refs=(_ref("a:1"),))
    untagged = ImageRecord(id=_digest(SHA_B))
    assert tagged.dangling is False
    assert untagged.dangling is True


# --------------------------------------------------------------------------- #
# ImageRecord.get() — all three input forms + not-managed raise.
# --------------------------------------------------------------------------- #


def _managed_one():
    return _patch_list(
        {"imagea:latest": ["box"]},
        [Hold(instance="box", pinned=_digest(SHA_A))],
        {"imagea:latest": SHA_A},
    )


def test_get_by_digest():
    g, h, i = _managed_one()
    with g, h, i:
        rec = ImageRecord.get(_digest(SHA_A)).unwrap()
    assert rec.id == _digest(SHA_A)
    assert rec.guests == ("box",)


def test_get_by_rendered_digest_str():
    g, h, i = _managed_one()
    with g, h, i:
        rec = ImageRecord.get(f"sha256:{SHA_A}").unwrap()
    assert rec.id == _digest(SHA_A)


def test_get_by_bare_hex_str():
    g, h, i = _managed_one()
    with g, h, i:
        rec = ImageRecord.get(SHA_A).unwrap()
    assert rec.id == _digest(SHA_A)


def test_get_by_tag_str():
    g, h, i = _managed_one()
    with g, h, i:
        rec = ImageRecord.get("imagea:latest").unwrap()
    assert rec.id == _digest(SHA_A)


def test_get_by_ocireference():
    g, h, i = _managed_one()
    with g, h, i:
        rec = ImageRecord.get(_ref("imagea:latest")).unwrap()
    assert rec.id == _digest(SHA_A)


def test_get_not_managed_returns_error():
    """S2: an id neither guest-referenced nor held => Error(IMAGE_NOT_FOUND)."""
    g, h, i = _managed_one()
    with g, h, i:
        result = ImageRecord.get(_digest(SHA_B))
    assert isinstance(result, kento.Error)
    assert result.conditions[0].kind is kento.ConditionKind.IMAGE_NOT_FOUND


# --------------------------------------------------------------------------- #
# image.record() — TOTAL (never None) + round-trip with resolve().
# --------------------------------------------------------------------------- #


def _oci(hex64):
    from kento import Layer, OciImage
    from pathlib import Path
    return OciImage(
        source=_ref("imagea:latest"),
        id=_digest(hex64),
        layers=(Layer(id="x", short_link=""),),
        overlay_root=Path("/var/lib/containers/storage/overlay"),
    )


def test_image_record_total_when_managed():
    """A managed image's record carries its guests/holds."""
    img = _oci(SHA_A)
    g, h, i = _managed_one()
    with g, h, i:
        rec = img.record()
    assert rec.id == _digest(SHA_A)
    assert rec.guests == ("box",)
    assert rec.held is True


def test_image_record_total_when_unmanaged():
    """An UNMANAGED image still has a record — empty, never None (TOTAL)."""
    img = _oci(SHA_C)  # not in the managed set below
    g, h, i = _managed_one()
    with g, h, i:
        rec = img.record()
    assert rec is not None
    assert rec.id == _digest(SHA_C)
    assert rec.guests == ()
    assert rec.holds == ()
    assert rec.refs == ()
    assert rec.dangling is True


def test_record_resolve_round_trip():
    """record().resolve() returns Ok of the artifact (resolved by content id).

    S2: ImageRecord.resolve calls the RAISING OciImage._get (kind fidelity), so
    we patch _get; resolve() wraps the result in Ok, .unwrap() recovers it.
    """
    img = _oci(SHA_A)
    seen = []

    def _capture(cls, ref):
        seen.append(ref)
        return img

    g, h, i = _managed_one()
    with g, h, i, patch(
        "kento._images.OciImage._get", classmethod(_capture),
    ):
        rec = img.record()
        back = rec.resolve().unwrap()
    assert back is img
    # resolve() resolves by the rendered content id (not a tag).
    assert seen == [f"sha256:{SHA_A}"]


def test_resolve_returns_error_when_content_gone():
    """S2: a dangling record's resolve() surfaces the absence as Error.

    The deep ImageNotFoundError raised by the RAISING _get reaches resolve()'s
    boundary with its real kind -> Error(IMAGE_NOT_FOUND), not INTERNAL
    (KIND-FIDELITY). Patching _get (the raising form) is the seam resolve uses.
    """
    rec = ImageRecord(id=_digest(SHA_B))

    def _gone(cls, ref):
        raise ImageNotFoundError(f"gone: {ref}")

    with patch("kento._images.OciImage._get", classmethod(_gone)):
        result = rec.resolve()
    assert isinstance(result, kento.Error)
    assert result.conditions[0].kind is kento.ConditionKind.IMAGE_NOT_FOUND


def test_base_image_record_id_raises():
    """A non-content-addressed Image has no content-keyed ledger entry."""
    from kento._images import Image
    from kento.errors import StateError
    # Use a minimal Image subclass that doesn't override _record_id.
    @dataclasses.dataclass(frozen=True, kw_only=True)
    class _Bare(Image):
        def is_writable(self):  # pragma: no cover - not exercised
            return True
        def prepare(self, state_dir):  # pragma: no cover
            pass
        def mount(self, host_dir, state_dir):  # pragma: no cover
            pass
        def unmount(self, host_dir):  # pragma: no cover
            pass
        def release(self, state_dir):  # pragma: no cover
            pass
    bare = _Bare(source=_ref("imagea:latest"))
    with pytest.raises(StateError):
        bare.record()
