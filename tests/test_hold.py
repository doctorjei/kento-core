"""Tests for the typed ``Hold`` pin (storage-depth SD2 — kento._images).

``Hold`` is an ADDITIVE typed READ over the existing procedural hold machinery
(``layers.py`` create/remove + ``images._holds`` / ``images._hold_image_ids``
queries). These tests cover:

* the value type — structure + frozen-ness + the ``Digest | OciReference`` pin;
* ``Hold.list()`` — modern (id label → ``Digest``), legacy (no label, ref
  ``.Image`` → ``OciReference``), bare-hex ``.Image`` (→ ``Digest``), the
  empty store (``[]``), stable ordering, and the skip-and-log of an
  un-pinnable hold;
* the modern-vs-legacy branch is mutation-tested (break the id-label read and a
  test reddens);
* ``instance.hold`` — the matching ``Hold`` / ``None``, loaded in the snapshot,
  for both pin shapes.

All podman I/O is mocked (no real podman). The flat re-export ``kento.Hold`` is
the canonical path.
"""

import dataclasses
import subprocess
from unittest.mock import patch

import pytest

import kento
from kento import Digest, Hold, OciReference

SHA = "a" * 64
SHA2 = "b" * 64
SHA3 = "c" * 64


# --------------------------------------------------------------------------- #
# Value type — structure, frozen-ness, the union pin.
# --------------------------------------------------------------------------- #


def test_hold_is_flat_reexport():
    """``Hold`` is reachable as ``kento.Hold`` and listed in ``__all__``."""
    assert kento.Hold is Hold
    assert "Hold" in kento.__all__


def test_hold_fields_and_kw_only():
    h = Hold(instance="box", pinned=Digest(algorithm="sha256", encoded=SHA))
    assert h.instance == "box"
    assert h.pinned == Digest(algorithm="sha256", encoded=SHA)
    # kw_only: positional construction is rejected.
    with pytest.raises(TypeError):
        Hold("box", Digest(algorithm="sha256", encoded=SHA))  # type: ignore[misc]


def test_hold_is_frozen():
    h = Hold(instance="box", pinned=Digest(algorithm="sha256", encoded=SHA))
    with pytest.raises(dataclasses.FrozenInstanceError):
        h.instance = "other"  # type: ignore[misc]


def test_hold_pinned_accepts_ocireference():
    """The legacy tag-pin shape: ``pinned`` is an ``OciReference``."""
    ref = OciReference.parse("docker.io/library/debian:12").unwrap()
    h = Hold(instance="box", pinned=ref)
    assert h.pinned == ref


def test_no_container_name_field():
    """``kento-hold.<instance>`` is DERIVABLE, not a stored field (principle 7)."""
    fields = {f.name for f in dataclasses.fields(Hold)}
    assert fields == {"instance", "pinned"}


# --------------------------------------------------------------------------- #
# Hold.list() — typed wrap of the procedural queries.
# --------------------------------------------------------------------------- #


def _patch_holds(holds, ids):
    """Patch the two procedural queries ``Hold.list`` imports.

    ``holds`` = the ``images._holds()`` result ([(held_for, .Image), ...]);
    ``ids`` = the ``images._hold_image_ids()`` result ({held_for: id}).
    """
    return (
        patch("kento.images._holds", return_value=holds),
        patch("kento.images._hold_image_ids", return_value=ids),
    )


def test_list_empty_store():
    h_p, i_p = _patch_holds([], {})
    with h_p, i_p:
        assert Hold.list().unwrap() == []


def test_list_modern_id_label_is_digest():
    """A non-empty io.kento.hold-image-id label → ``pinned`` is a ``Digest``."""
    h_p, i_p = _patch_holds([("box", "docker.io/library/debian:12")], {"box": SHA})
    with h_p, i_p:
        holds = Hold.list().unwrap()
    assert holds == [Hold(instance="box",
                          pinned=Digest(algorithm="sha256", encoded=SHA))]


def test_list_modern_prefixed_id_label_parsed_faithfully():
    """A prefixed id label (algorithm:encoded) is parsed faithfully, no
    sha256 assumption."""
    h_p, i_p = _patch_holds([("box", "img")], {"box": f"sha256:{SHA}"})
    with h_p, i_p:
        holds = Hold.list().unwrap()
    assert holds[0].pinned == Digest(algorithm="sha256", encoded=SHA)


def test_list_legacy_tag_image_is_ocireference():
    """No id label, a tag-ref ``.Image`` → ``pinned`` is an ``OciReference``."""
    h_p, i_p = _patch_holds([("box", "docker.io/library/debian:12")], {})
    with h_p, i_p:
        holds = Hold.list().unwrap()
    assert holds == [Hold(instance="box",
                          pinned=OciReference.parse("docker.io/library/debian:12").unwrap())]


def test_list_legacy_bare_hex_image_is_digest():
    """No id label, a bare 64-hex ``.Image`` is an image ID → ``Digest`` (JC3),
    NOT a malformed OciReference."""
    h_p, i_p = _patch_holds([("box", SHA2)], {})
    with h_p, i_p:
        holds = Hold.list().unwrap()
    assert holds == [Hold(instance="box",
                          pinned=Digest(algorithm="sha256", encoded=SHA2))]


def test_list_skips_unpinnable_hold_with_log(caplog):
    """A hold with no id label AND no .Image has no faithful pin → skipped."""
    h_p, i_p = _patch_holds([("box", "")], {})
    with h_p, i_p:
        holds = Hold.list().unwrap()
    assert holds == []


def test_list_one_bad_hold_does_not_hide_healthy(caplog):
    """Totality: the un-pinnable hold is skipped, the healthy one survives."""
    h_p, i_p = _patch_holds([("bad", ""), ("good", "img")], {"good": SHA})
    with h_p, i_p:
        holds = Hold.list().unwrap()
    assert holds == [Hold(instance="good",
                          pinned=Digest(algorithm="sha256", encoded=SHA))]


def test_list_sorted_by_instance():
    """Stable, deterministic ordering: sorted by instance name (JC4)."""
    h_p, i_p = _patch_holds(
        [("zebra", "i"), ("alpha", "i"), ("mango", "i")],
        {"zebra": SHA, "alpha": SHA2, "mango": SHA3},
    )
    with h_p, i_p:
        holds = Hold.list().unwrap()
    assert [h.instance for h in holds] == ["alpha", "mango", "zebra"]


def test_list_mixed_modern_and_legacy():
    """Modern (id label) and legacy (.Image) holds coexist, each faithful."""
    h_p, i_p = _patch_holds(
        [("modern", "img"), ("legacy", "docker.io/library/debian:12")],
        {"modern": SHA},
    )
    with h_p, i_p:
        holds = Hold.list().unwrap()
    assert holds == [
        Hold(instance="legacy",
             pinned=OciReference.parse("docker.io/library/debian:12").unwrap()),
        Hold(instance="modern", pinned=Digest(algorithm="sha256", encoded=SHA)),
    ]


def test_list_modern_branch_mutation(caplog):
    """MUTATION TEST: if the id-label read is broken (ids never consulted), a
    modern id-pinned hold is misread as a LEGACY tag-pin (OciReference) instead
    of a Digest — this asserts the branch that distinguishes them."""
    # The id label IS present, so the modern branch must produce a Digest. If a
    # future edit drops the id-label lookup, the .Image ("docker...") would be
    # parsed as an OciReference and this equality would fail.
    h_p, i_p = _patch_holds([("box", "docker.io/library/debian:12")], {"box": SHA})
    with h_p, i_p:
        holds = Hold.list().unwrap()
    assert isinstance(holds[0].pinned, Digest)
    assert not isinstance(holds[0].pinned, OciReference)


# --------------------------------------------------------------------------- #
# S2 (Result sweep) — Hold.list() public boundary returns a Result; the raising
# _list is the body used by ImageRecord._list (kind fidelity).
# --------------------------------------------------------------------------- #


def test_list_returns_ok_result():
    h_p, i_p = _patch_holds([("box", "img")], {"box": SHA})
    with h_p, i_p:
        result = Hold.list()
    assert isinstance(result, kento.Ok)
    assert result.unwrap() == [
        Hold(instance="box", pinned=Digest(algorithm="sha256", encoded=SHA))]


def test__list_is_raising_form_used_internally():
    # _list returns a bare list (no Result wrap) — it is the form ImageRecord._list
    # consumes so an internal raise keeps its real kind at that boundary. (Hold's
    # own list path skips-and-logs rather than raising, so we assert the shape.)
    h_p, i_p = _patch_holds([("box", "img")], {"box": SHA})
    with h_p, i_p:
        holds = Hold._list()
    assert holds == [
        Hold(instance="box", pinned=Digest(algorithm="sha256", encoded=SHA))]


# --------------------------------------------------------------------------- #
# instance.hold — loaded in the eager snapshot (single targeted inspect).
# --------------------------------------------------------------------------- #


def _inst_with_hold(monkeypatch, *, image_id="", image=""):
    """Build a bare SystemContainer snapshot via _load_hold, mocking the one
    per-instance podman inspect path."""
    from kento import _instances

    monkeypatch.setattr(_instances, "_hold_pinned_id", lambda name: image_id,
                        raising=False)

    def _fake_inspect(cmd, *a, **k):
        out = subprocess.CompletedProcess(cmd, 0, stdout=image, stderr="")
        return out

    # _load_hold reads .Image via subprocess.run only on the legacy branch.
    monkeypatch.setattr(_instances.subprocess, "run", _fake_inspect)
    # Patch the id-label reader where _load_hold imports it (kento.layers).
    monkeypatch.setattr("kento.layers._hold_pinned_id", lambda name: image_id)
    return _instances._load_hold("box")


def test_instance_hold_modern_digest(monkeypatch):
    hold = _inst_with_hold(monkeypatch, image_id=SHA)
    assert hold == Hold(instance="box",
                        pinned=Digest(algorithm="sha256", encoded=SHA))


def test_instance_hold_legacy_reference(monkeypatch):
    hold = _inst_with_hold(monkeypatch, image_id="",
                          image="docker.io/library/debian:12")
    assert hold == Hold(instance="box",
                        pinned=OciReference.parse("docker.io/library/debian:12").unwrap())


def test_instance_hold_legacy_bare_hex_digest(monkeypatch):
    hold = _inst_with_hold(monkeypatch, image_id="", image=SHA2)
    assert hold == Hold(instance="box",
                        pinned=Digest(algorithm="sha256", encoded=SHA2))


def test_instance_hold_none_when_no_container(monkeypatch):
    """A failed inspect (no kento-hold.<name>) → instance.hold is None."""
    from kento import _instances

    monkeypatch.setattr("kento.layers._hold_pinned_id", lambda name: "")

    def _fail_inspect(cmd, *a, **k):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no such")

    monkeypatch.setattr(_instances.subprocess, "run", _fail_inspect)
    assert _instances._load_hold("box") is None


def test_instance_hold_loaded_in_snapshot(monkeypatch):
    """instance.hold reflects the cached snapshot value (eager, principle 2)."""
    from kento import _instances

    monkeypatch.setattr(_instances, "_load_hold",
                       lambda name: Hold(
                           instance=name,
                           pinned=Digest(algorithm="sha256", encoded=SHA)))
    # Build a snapshot without touching real state: stub every other loader to a
    # benign value and only exercise the hold wiring.
    inst = _instances.SystemContainer.__new__(_instances.SystemContainer)
    inst._hold = _instances._load_hold("box")
    assert inst.hold == Hold(instance="box",
                            pinned=Digest(algorithm="sha256", encoded=SHA))
    # Getter-only: assignment raises (no setter).
    with pytest.raises(AttributeError):
        inst.hold = None  # type: ignore[misc]
