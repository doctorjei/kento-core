"""Tests for the typed ``Image`` family (Block 05 / SD1 — kento._images).

Structure + frozen-ness; the SD1 hierarchy (``Image`` generalized — no ``id`` on
the base + abstract ``is_writable()``; ``LayeredImage`` now ABSTRACT; ``OciImage``
the concrete OCI member carrying ``id`` + lifecycle); ``OciImage.id`` /
``OciImage.resolve_id`` totality; ``OciImage`` resolution + runtime lifecycle as
ADDITIVE wrappers (asserting DELEGATION to kento.layers / kento.vm, not
reimplementation); ``DiskFormat``; the documented stubs are declared +
abstract/uninstantiable; flat re-exports reachable as ``kento.X``. All I/O is
mocked (no real podman / mounts).
"""

import dataclasses
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import kento
from kento import (
    CompositeImage,
    Digest,
    DiskFormat,
    Image,
    ImageNotFoundError,
    Layer,
    LayeredImage,
    MalformedReference,
    OciImage,
    OciReference,
    VolumeImage,
)

SHA = "a" * 64
DIGEST_STR = f"sha256:{SHA}"


def _ref(s="docker.io/library/ubuntu:latest"):
    return OciReference.parse(s).unwrap()


def _digest():
    return Digest.parse(DIGEST_STR)


# --------------------------------------------------------------------------- #
# Flat re-exports + DiskFormat
# --------------------------------------------------------------------------- #


def test_flat_reexports_reachable():
    for name in ("Image", "LayeredImage", "OciImage", "Layer", "DiskFormat",
                 "VolumeImage", "CompositeImage"):
        assert hasattr(kento, name)
        assert name in kento.__all__


def test_diskformat_values():
    assert DiskFormat.RAW == "raw"
    assert DiskFormat.QCOW2 == "qcow2"
    # str-backed: the value IS the wire string.
    assert DiskFormat("qcow2") is DiskFormat.QCOW2
    assert isinstance(DiskFormat.RAW, str)


# --------------------------------------------------------------------------- #
# Layer — pure frozen value
# --------------------------------------------------------------------------- #


def test_layer_is_frozen_value():
    layer = Layer(id="abc", short_link="xy")
    assert layer.id == "abc"
    assert layer.short_link == "xy"
    with pytest.raises(dataclasses.FrozenInstanceError):
        layer.id = "other"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Image ABC — generalized (SD1): no `id` on the base, abstract `is_writable()`,
# uninstantiable, abstract lifecycle contract.
# --------------------------------------------------------------------------- #


def test_image_abc_uninstantiable():
    # Image is abstract (abstract is_writable + lifecycle), so even with all of
    # its OWN fields supplied it cannot be instantiated.
    with pytest.raises(TypeError):
        Image(source=_ref())  # type: ignore[abstract]


def test_image_base_has_no_id_field():
    # SD1: `id: Digest` was DEMOTED off the base onto OciImage (a writable image
    # has no content digest → identity = location). Re-adding `id` to the base
    # would reintroduce the constraint this move removed → this guard reddens.
    field_names = {f.name for f in dataclasses.fields(Image)}
    assert "id" not in field_names
    assert field_names == {"source", "kernel", "initramfs"}


def test_image_declares_is_writable_abstract():
    # SD1: is_writable() is the capability contract, abstract on the base so the
    # family does not assume read-only-ness. Mutation guard (gate C): removing
    # the @abstractmethod (or giving the base a body) reddens this.
    assert "is_writable" in Image.__abstractmethods__


def test_image_declares_lifecycle_abstract():
    for prim in ("prepare", "mount", "unmount", "release"):
        assert prim in Image.__abstractmethods__


# --------------------------------------------------------------------------- #
# LayeredImage — now ABSTRACT (SD1): the "layering" node, uninstantiable, no
# OCI-specific fields. OciImage IS-A LayeredImage IS-A Image.
# --------------------------------------------------------------------------- #


def test_layeredimage_is_abstract_uninstantiable():
    # SD1: LayeredImage is now abstract (the layering concept). It inherits the
    # abstract is_writable + lifecycle from Image and adds NO concrete impl, so
    # it cannot be constructed.
    assert LayeredImage.__abstractmethods__ >= {
        "is_writable", "prepare", "mount", "unmount", "release"}
    with pytest.raises(TypeError):
        LayeredImage(source=_ref())  # type: ignore[abstract]


def test_layeredimage_is_a_image():
    assert issubclass(LayeredImage, Image)


def test_layeredimage_carries_no_oci_fields():
    # SD1: the OCI/podman specifics (id, layers, overlay_root) live on OciImage,
    # NOT the abstract LayeredImage. LayeredImage only has the base Image fields.
    field_names = {f.name for f in dataclasses.fields(LayeredImage)}
    assert field_names == {"source", "kernel", "initramfs"}


def test_ociimage_is_a_layeredimage_is_a_image():
    # The SD1 hierarchy: OciImage <: LayeredImage <: Image.
    assert issubclass(OciImage, LayeredImage)
    assert issubclass(OciImage, Image)


def test_ociimage_is_writable_is_false():
    # SD1: OCI store layers are read-only by spec → is_writable() is False.
    # Mutation guard (gate C): flipping the body to True reddens this.
    img = _img_with_root()
    assert img.is_writable() is False


# --------------------------------------------------------------------------- #
# OciImage — structure, frozen, narrowed source, content id
# --------------------------------------------------------------------------- #


def test_ociimage_fields_and_frozen():
    img = OciImage(
        source=_ref(),
        id=_digest(),
        layers=(Layer(id="x", short_link="lx"),),
        overlay_root=Path("/store/overlay"),
    )
    assert isinstance(img, Image)
    assert isinstance(img, OciImage)
    assert img.source.scheme == "oci"
    assert img.id == _digest()
    assert img.kernel is None and img.initramfs is None
    assert img.layers == (Layer(id="x", short_link="lx"),)
    with pytest.raises(dataclasses.FrozenInstanceError):
        img.id = _digest()  # type: ignore[misc]


def test_ociimage_layers_list_frozen_to_tuple():
    img = OciImage(
        source=_ref(), id=_digest(),
        layers=[Layer(id="x", short_link="")],  # a list argument
        overlay_root=Path("/store/overlay"),
    )
    assert isinstance(img.layers, tuple)


def test_ociimage_layers_and_overlay_root_are_mandatory():
    # §4.1 verbatim: both fields are required (no defaults). A layer-less /
    # rootless OciImage is the degenerate value the mandatory fields exist
    # to prevent (gate C) — construction without them must fail.
    with pytest.raises(TypeError):
        OciImage(source=_ref(), id=_digest())  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        OciImage(  # type: ignore[call-arg]
            source=_ref(), id=_digest(),
            layers=(Layer(id="x", short_link=""),),
        )  # missing overlay_root
    with pytest.raises(TypeError):
        OciImage(  # type: ignore[call-arg]
            source=_ref(), id=_digest(), overlay_root=Path("/store"),
        )  # missing layers


def test_ociimage_id_is_mandatory():
    # SD1: `id` is demoted onto OciImage and stays MANDATORY there (an OCI image
    # is content-addressed). Construction without it must fail.
    with pytest.raises(TypeError):
        OciImage(  # type: ignore[call-arg]
            source=_ref(),
            layers=(Layer(id="x", short_link=""),),
            overlay_root=Path("/store/overlay"),
        )


def test_ociimage_kernel_override_carried():
    img = OciImage(
        source=_ref(), id=_digest(),
        layers=(Layer(id="x", short_link=""),),
        overlay_root=Path("/store/overlay"),
        kernel=Path("/k/vmlinuz"), initramfs=Path("/k/initrd"),
    )
    assert img.kernel == Path("/k/vmlinuz")
    assert img.initramfs == Path("/k/initrd")


# --------------------------------------------------------------------------- #
# OciImage.id totality — resolve_id raises (never returns ""/None)
# --------------------------------------------------------------------------- #


def test_resolve_id_returns_typed_digest():
    with patch("kento.layers.resolve_image_id", return_value=DIGEST_STR) as m:
        digest = OciImage.resolve_id("ubuntu:latest")
    assert isinstance(digest, Digest)
    assert digest.render() == DIGEST_STR
    m.assert_called_once_with("ubuntu:latest")


def test_resolve_id_raises_on_empty_not_sentinel():
    # resolve_image_id returns "" on failure (error-as-data) — the typed
    # surface must RAISE, never yield the sentinel (principle 5).
    with patch("kento.layers.resolve_image_id", return_value=""):
        with pytest.raises(ImageNotFoundError, match="could not resolve content id"):
            OciImage.resolve_id("ghost:latest")


def test_resolve_id_raises_on_malformed_digest():
    with patch("kento.layers.resolve_image_id", return_value="not-a-digest"):
        with pytest.raises(MalformedReference):
            OciImage.resolve_id("weird:latest")


# --------------------------------------------------------------------------- #
# resolve_id — BARE-HEX normalization (the real-podman {{.Id}} shape).
#
# REGRESSION: real podman (5.4.2) returns a BARE 64-hex sha256 with NO
# "sha256:" prefix. The old code did Digest.parse(raw), which requires
# "algorithm:encoded" and raised MalformedReference on EVERY real image (it was
# masked because the prior unit tests only fed the prefixed DIGEST_STR form).
# resolve_id must normalize the bare hex into Digest("sha256", hex).
# --------------------------------------------------------------------------- #

BARE_SHA = "279c3d3b" + "f" * 56  # a realistic bare 64-hex {{.Id}} (no prefix)


def test_resolve_id_normalizes_bare_hex_to_sha256_digest():
    # The real-podman shape: {{.Id}} is a bare 64-char hex, NO "sha256:" prefix.
    assert ":" not in BARE_SHA and len(BARE_SHA) == 64
    with patch("kento.layers.resolve_image_id", return_value=BARE_SHA) as m:
        digest = OciImage.resolve_id("ubuntu:latest")
    assert isinstance(digest, Digest)
    assert digest.algorithm == "sha256"
    assert digest.encoded == BARE_SHA          # bare hex preserved verbatim
    assert digest.render() == f"sha256:{BARE_SHA}"  # rendered WITH the prefix
    m.assert_called_once_with("ubuntu:latest")


def test_resolve_id_bare_hex_strips_whitespace_via_resolve_image_id():
    # resolve_image_id already .strip()s; a clean bare hex normalizes fine.
    with patch("kento.layers.resolve_image_id", return_value=SHA):
        digest = OciImage.resolve_id("ubuntu:latest")
    assert digest.encoded == SHA
    assert digest.render() == DIGEST_STR


def test_resolve_id_prefixed_form_still_parses_faithfully():
    # Some podman/docker builds DO emit "algorithm:encoded" — that path must
    # still parse faithfully (and keep a non-sha256 algorithm intact).
    with patch("kento.layers.resolve_image_id", return_value=DIGEST_STR):
        digest = OciImage.resolve_id("ubuntu:latest")
    assert digest.render() == DIGEST_STR


def test_resolve_id_garbage_bare_id_still_raises_typed():
    # A bare-but-NOT-64-hex id must still raise a typed error (validate_digest
    # via Digest.__post_init__) — never fabricate a bad Digest (gate C).
    for bad in ("not-a-digest", "z" * 64, "abc123", "a" * 63):
        with patch("kento.layers.resolve_image_id", return_value=bad):
            with pytest.raises(MalformedReference):
                OciImage.resolve_id("weird:latest")


# --------------------------------------------------------------------------- #
# OciImage.resolve — delegates to layers.resolve_layers /
# to_overlay_lowerdir / resolve_image_id (does not reimplement them)
# --------------------------------------------------------------------------- #


def test_resolve_delegates_and_populates():
    # S2 (Result sweep): resolve() returns Ok(OciImage); .unwrap() to the value.
    ref = _ref("ubuntu:latest")
    with patch("kento.layers.resolve_layers",
               return_value="/store/overlay/ID1/diff:/store/overlay/ID2/diff") as m_layers, \
         patch("kento.layers.to_overlay_lowerdir",
               return_value=("/store/overlay", "ID1/diff:ID2/diff")) as m_ovl, \
         patch("kento.layers.resolve_image_id", return_value=DIGEST_STR) as m_id:
        result = OciImage.resolve(ref)

    assert isinstance(result, kento.Ok)
    img = result.unwrap()

    # Delegation: each wrapped function was called with the rendered ref.
    m_layers.assert_called_once_with(ref.render())
    m_ovl.assert_called_once_with(
        "/store/overlay/ID1/diff:/store/overlay/ID2/diff")
    m_id.assert_called_once_with(ref.render())

    assert isinstance(img, OciImage)
    assert img.source is ref
    assert img.overlay_root == Path("/store/overlay")
    assert img.id.render() == DIGEST_STR
    # Ids come from the ABSOLUTE lowerdir (<root>/<id>/diff); short_link is read
    # from each <root>/<id>/link file and is "" when (as here) it is absent.
    assert img.layers == (
        Layer(id="ID1", short_link=""),
        Layer(id="ID2", short_link=""),
    )
    # Round-trips to the absolute lowerdir for the wrapped functions.
    assert img._render_lowerdir_abs() == \
        "/store/overlay/ID1/diff:/store/overlay/ID2/diff"


def test_resolve_returns_error_image_not_found():
    # S2: an absent image is a PREDICTABLE Error(IMAGE_NOT_FOUND), not a raise.
    ref = _ref("ghost:latest")
    with patch("kento.layers.resolve_layers",
               side_effect=ImageNotFoundError("nope")):
        result = OciImage.resolve(ref)
    assert isinstance(result, kento.Error)
    assert result.conditions[0].kind is kento.ConditionKind.IMAGE_NOT_FOUND
    assert result.conditions[0].message == "nope"


def test__resolve_RAISES_image_not_found_kind_fidelity():
    # KIND-FIDELITY mutation guard: the raising _resolve STAYS RAISING, so a deep
    # ImageNotFoundError reaches a still-raising caller (e.g. Instance.image()) /
    # the public boundary with its REAL type, not a generic ResultError. If
    # _resolve were made to .unwrap() a Result-returning resolve instead, the type
    # would become ResultError and this reddens.
    ref = _ref("ghost:latest")
    with patch("kento.layers.resolve_layers",
               side_effect=ImageNotFoundError("nope")):
        with pytest.raises(ImageNotFoundError):
            OciImage._resolve(ref)


def test_resolve_derives_root_when_to_overlay_yields_none(tmp_path):
    # When to_overlay_lowerdir finds no shared root ("") but the absolute
    # entries DO share one (the common OCI case where the store layout was just
    # unexpected to that helper), overlay_root is derived from the entries — a
    # real Path per §4.1, never None (resolves judgment call 5 without a foot-gun).
    abs_layers = "/store/overlay/ID1/diff:/store/overlay/ID2/diff"
    ref = _ref("ubuntu:latest")
    with patch("kento.layers.resolve_layers", return_value=abs_layers), \
         patch("kento.layers.to_overlay_lowerdir", return_value=("", abs_layers)), \
         patch("kento.layers.resolve_image_id", return_value=DIGEST_STR):
        img = OciImage.resolve(ref).unwrap()
    assert img.overlay_root == Path("/store/overlay")
    assert img.layers == (
        Layer(id="ID1", short_link=""),
        Layer(id="ID2", short_link=""),
    )
    assert img._render_lowerdir_abs() == abs_layers


def test_resolve_returns_error_on_no_shared_root():
    # A genuinely unexpected layout (entries don't share a <root>/<id>/diff root)
    # is a StateError internally -> Error(INVALID_STATE) at the resolve boundary
    # (gate C, principle 5) — NOT a fabricated rootless degenerate value.
    abs_layers = "/a/x/diff:/b/y/diff"  # roots /a and /b disagree
    ref = _ref("ubuntu:latest")
    with patch("kento.layers.resolve_layers", return_value=abs_layers), \
         patch("kento.layers.to_overlay_lowerdir", return_value=("", abs_layers)), \
         patch("kento.layers.resolve_image_id", return_value=DIGEST_STR):
        result = OciImage.resolve(ref)
    assert isinstance(result, kento.Error)
    assert result.conditions[0].kind is kento.ConditionKind.INVALID_STATE
    assert "common store root" in result.conditions[0].message


def test_resolve_returns_error_on_non_diff_entry():
    abs_layers = "/store/overlay/ID1/notdiff"
    ref = _ref("ubuntu:latest")
    with patch("kento.layers.resolve_layers", return_value=abs_layers), \
         patch("kento.layers.to_overlay_lowerdir", return_value=("", abs_layers)), \
         patch("kento.layers.resolve_image_id", return_value=DIGEST_STR):
        result = OciImage.resolve(ref)
    assert isinstance(result, kento.Error)
    assert result.conditions[0].kind is kento.ConditionKind.INVALID_STATE
    assert "<root>/<id>/diff" in result.conditions[0].message


def test_resolve_reads_short_link_from_link_file(tmp_path):
    # short_link is read from <root>/<id>/link (the same source
    # to_overlay_lowerdir uses); ids come from the absolute lowerdir.
    root = tmp_path / "overlay"
    (root / "ABCID").mkdir(parents=True)
    (root / "ABCID" / "link").write_text("SHORTLINK\n")
    (root / "DEFID").mkdir(parents=True)  # no link file -> short_link ""

    abs_layers = f"{root}/ABCID/diff:{root}/DEFID/diff"
    ref = _ref("ubuntu:latest")
    with patch("kento.layers.resolve_layers", return_value=abs_layers), \
         patch("kento.layers.to_overlay_lowerdir", return_value=(str(root), "")), \
         patch("kento.layers.resolve_image_id", return_value=DIGEST_STR):
        img = OciImage.resolve(ref).unwrap()
    assert img.layers == (
        Layer(id="ABCID", short_link="SHORTLINK"),
        Layer(id="DEFID", short_link=""),
    )
    assert img.overlay_root == root
    assert img._render_lowerdir_abs() == abs_layers


# --------------------------------------------------------------------------- #
# Runtime lifecycle — prepare/mount/unmount/release wrap layers/vm
# --------------------------------------------------------------------------- #


def _img_with_root():
    return OciImage(
        source=_ref("ubuntu:latest"),
        id=_digest(),
        layers=(Layer(id="ID1", short_link="s1"),
                Layer(id="ID2", short_link="s2")),
        overlay_root=Path("/store/overlay"),
    )


def test_prepare_runs_preflight_and_makes_upper_work(tmp_path):
    img = _img_with_root()
    state = tmp_path / "state"
    state.mkdir()
    with patch("kento.layers.preflight_overlay_layers") as m_pf:
        img.prepare(state)
    # Delegates to the existing preflight with the absolute lowerdir + state.
    m_pf.assert_called_once_with(
        "/store/overlay/ID1/diff:/store/overlay/ID2/diff", state)
    assert (state / "upper").is_dir()
    assert (state / "work").is_dir()


def test_prepare_propagates_preflight_failure(tmp_path):
    from kento.errors import StateError
    img = _img_with_root()
    state = tmp_path / "state"
    state.mkdir()
    with patch("kento.layers.preflight_overlay_layers",
               side_effect=StateError("too many layers")):
        with pytest.raises(StateError):
            img.prepare(state)


def test_mount_delegates_to_vm_mount_rootfs(tmp_path):
    img = _img_with_root()
    host = tmp_path / "host"
    state = tmp_path / "state"
    with patch("kento.vm.mount_rootfs") as m_mount:
        img.mount(host, state)
    m_mount.assert_called_once_with(
        host, "/store/overlay/ID1/diff:/store/overlay/ID2/diff", state)


def test_unmount_delegates_to_vm_unmount_rootfs(tmp_path):
    img = _img_with_root()
    host = tmp_path / "host"
    with patch("kento.vm.unmount_rootfs") as m_un:
        img.unmount(host)
    m_un.assert_called_once_with(host)


def test_release_is_noop_for_overlay(tmp_path):
    img = _img_with_root()
    state = tmp_path / "state"
    state.mkdir()
    (state / "upper").mkdir()
    # release must not destroy per-instance writable state (caller's policy).
    assert img.release(state) is None
    assert (state / "upper").is_dir()


def test_lifecycle_does_not_call_subprocess_directly(tmp_path):
    # The wrappers must DELEGATE; they must not fork podman/mount logic by
    # touching subprocess themselves. With the wrapped funcs mocked, no
    # subprocess.run in kento.layers / kento.vm should fire.
    img = _img_with_root()
    state = tmp_path / "state"
    state.mkdir()
    with patch("kento.layers.subprocess.run") as m_layers_run, \
         patch("kento.vm.subprocess.run") as m_vm_run, \
         patch("kento.layers.preflight_overlay_layers"), \
         patch("kento.vm.mount_rootfs"), \
         patch("kento.vm.unmount_rootfs"):
        img.prepare(state)
        img.mount(tmp_path / "host", state)
        img.unmount(tmp_path / "host")
        img.release(state)
    m_layers_run.assert_not_called()
    m_vm_run.assert_not_called()


# --------------------------------------------------------------------------- #
# Stubs — declared fields, abstract/uninstantiable, no lifecycle
# --------------------------------------------------------------------------- #


def test_volumeimage_is_abstract_stub():
    # SD1: inherits Image's abstract is_writable + lifecycle primitives
    # unimplemented -> stays uninstantiable design stub (NOT built out).
    assert VolumeImage.__abstractmethods__ >= {
        "is_writable", "prepare", "mount", "unmount", "release"}
    with pytest.raises(TypeError):
        VolumeImage(  # type: ignore[abstract]
            source=_ref(), id=_digest(),
            path=Path("/img.qcow2"), format=DiskFormat.QCOW2, backing=None,
        )


def test_volumeimage_declares_fields():
    # SD1 reconciliation: VolumeImage gains `id: Digest | None` (content Digest
    # when ro/content-addressed, None when writable). path/format/backing per §4.1.
    fields = {f.name for f in dataclasses.fields(VolumeImage)}
    assert {"path", "format", "backing", "id"} <= fields


def test_volumeimage_id_is_optional_digest():
    # SD1: VolumeImage.id is `Digest | None` — None expresses a writable
    # (location-identified) volume, a content Digest expresses a ro one. Both
    # must type-check as the field; we only assert the annotation here since the
    # stub is uninstantiable (no construction).
    id_field = next(
        f for f in dataclasses.fields(VolumeImage) if f.name == "id")
    assert "Digest" in str(id_field.type) and "None" in str(id_field.type)


def test_compositeimage_is_abstract_stub():
    assert CompositeImage.__abstractmethods__ >= {
        "is_writable", "prepare", "mount", "unmount", "release"}
    # SD1: CompositeImage no longer has an `id` field (demoted off the base);
    # it inherits no concrete impl, so it stays uninstantiable.
    with pytest.raises(TypeError):
        CompositeImage(source=_ref(), mounts={})  # type: ignore[abstract]


def test_compositeimage_declares_fields():
    fields = {f.name for f in dataclasses.fields(CompositeImage)}
    assert "mounts" in fields
    # SD1: `id` is NOT a CompositeImage field (off the base; CompositeImage is
    # not content-addressed — its identity is its composition).
    assert "id" not in fields


# --------------------------------------------------------------------------- #
# Sanity: no live-path import side effects (module is additive/pure to import)
# --------------------------------------------------------------------------- #


def test_module_import_has_no_subprocess_at_import():
    # Importing the module must not invoke podman. (Construction & lifecycle
    # are the only I/O moments, and those are explicit methods.)
    import importlib
    with patch("subprocess.run",
               side_effect=AssertionError("import must not run subprocess")):
        importlib.reload(importlib.import_module("kento._images"))
