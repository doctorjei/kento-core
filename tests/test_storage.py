"""Spec suite for the root-storage value type (Block 03).

Covers ``StorageMode`` enum membership + exact wire string values, the
str-enum behavior, and the flat-public re-export from ``kento``.

Spec: ~/workspace/kento-core-api-design.md §2, §8 (StorageMode roadmap bullet).
"""

from kento import StorageMode


# --------------------------------------------------------------------------- #
# Flat public re-export (Block 03 surface).
# --------------------------------------------------------------------------- #


def test_public_name_reexported_flat():
    import kento

    assert "StorageMode" in kento.__all__
    assert getattr(kento, "StorageMode") is not None


def test_no_module_stutter():
    import kento
    import kento._storage as impl

    assert kento.StorageMode is impl.StorageMode


# --------------------------------------------------------------------------- #
# StorageMode — membership + exact wire values (§8).
# --------------------------------------------------------------------------- #


def test_storagemode_members_and_values():
    # Exactly two members in 1.0; a future PERSISTENT_IMAGE is a comment, not a
    # member (§2 principle 7 — grow by adding values).
    assert {m.name for m in StorageMode} == {"OVERLAY", "EPHEMERAL_IMAGE"}
    assert StorageMode.OVERLAY.value == "overlay"
    assert StorageMode.EPHEMERAL_IMAGE.value == "ephemeral-image"


def test_storagemode_is_str_enum():
    # str-backed: the value IS the kento-storage wire string.
    assert isinstance(StorageMode.OVERLAY, str)
    assert StorageMode.OVERLAY == "overlay"
    assert StorageMode("ephemeral-image") is StorageMode.EPHEMERAL_IMAGE
