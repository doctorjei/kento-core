"""Tests for the typed ``Image`` family (Block 05 — kento._images).

Structure + frozen-ness; ``Image.id`` / ``LayeredImage.resolve_id`` totality;
``LayeredImage`` resolution + runtime lifecycle as ADDITIVE wrappers (asserting
DELEGATION to kento.layers / kento.vm, not reimplementation); ``DiskFormat``;
the documented stubs are declared + abstract/uninstantiable; flat re-exports
reachable as ``kento.X``. All I/O is mocked (no real podman / mounts).
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
    OciReference,
    VolumeImage,
)

SHA = "a" * 64
DIGEST_STR = f"sha256:{SHA}"


def _ref(s="docker.io/library/ubuntu:latest"):
    return OciReference.parse(s)


def _digest():
    return Digest.parse(DIGEST_STR)


# --------------------------------------------------------------------------- #
# Flat re-exports + DiskFormat
# --------------------------------------------------------------------------- #


def test_flat_reexports_reachable():
    for name in ("Image", "LayeredImage", "Layer", "DiskFormat",
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
# Image ABC — uninstantiable; abstract contract
# --------------------------------------------------------------------------- #


def test_image_abc_uninstantiable():
    with pytest.raises(TypeError):
        Image(source=_ref(), id=_digest())  # type: ignore[abstract]


def test_image_declares_lifecycle_abstract():
    for prim in ("prepare", "mount", "unmount", "release"):
        assert prim in Image.__abstractmethods__


# --------------------------------------------------------------------------- #
# LayeredImage — structure, frozen, narrowed source
# --------------------------------------------------------------------------- #


def test_layeredimage_fields_and_frozen():
    img = LayeredImage(
        source=_ref(),
        id=_digest(),
        layers=(Layer(id="x", short_link="lx"),),
        overlay_root=Path("/store/overlay"),
    )
    assert isinstance(img, Image)
    assert img.source.scheme == "oci"
    assert img.id == _digest()
    assert img.kernel is None and img.initramfs is None
    assert img.layers == (Layer(id="x", short_link="lx"),)
    with pytest.raises(dataclasses.FrozenInstanceError):
        img.id = _digest()  # type: ignore[misc]


def test_layeredimage_layers_list_frozen_to_tuple():
    img = LayeredImage(
        source=_ref(), id=_digest(),
        layers=[Layer(id="x", short_link="")],  # a list argument
        overlay_root=Path("/store/overlay"),
    )
    assert isinstance(img.layers, tuple)


def test_layeredimage_layers_and_overlay_root_are_mandatory():
    # §4.1 verbatim: both fields are required (no defaults). A layer-less /
    # rootless LayeredImage is the degenerate value the mandatory fields exist
    # to prevent (gate C) — construction without them must fail.
    with pytest.raises(TypeError):
        LayeredImage(source=_ref(), id=_digest())  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        LayeredImage(  # type: ignore[call-arg]
            source=_ref(), id=_digest(),
            layers=(Layer(id="x", short_link=""),),
        )  # missing overlay_root
    with pytest.raises(TypeError):
        LayeredImage(  # type: ignore[call-arg]
            source=_ref(), id=_digest(), overlay_root=Path("/store"),
        )  # missing layers


def test_layeredimage_kernel_override_carried():
    img = LayeredImage(
        source=_ref(), id=_digest(),
        layers=(Layer(id="x", short_link=""),),
        overlay_root=Path("/store/overlay"),
        kernel=Path("/k/vmlinuz"), initramfs=Path("/k/initrd"),
    )
    assert img.kernel == Path("/k/vmlinuz")
    assert img.initramfs == Path("/k/initrd")


# --------------------------------------------------------------------------- #
# Image.id totality — resolve_id raises (never returns ""/None)
# --------------------------------------------------------------------------- #


def test_resolve_id_returns_typed_digest():
    with patch("kento.layers.resolve_image_id", return_value=DIGEST_STR) as m:
        digest = LayeredImage.resolve_id("ubuntu:latest")
    assert isinstance(digest, Digest)
    assert digest.render() == DIGEST_STR
    m.assert_called_once_with("ubuntu:latest")


def test_resolve_id_raises_on_empty_not_sentinel():
    # resolve_image_id returns "" on failure (error-as-data) — the typed
    # surface must RAISE, never yield the sentinel (principle 5).
    with patch("kento.layers.resolve_image_id", return_value=""):
        with pytest.raises(ImageNotFoundError, match="could not resolve content id"):
            LayeredImage.resolve_id("ghost:latest")


def test_resolve_id_raises_on_malformed_digest():
    with patch("kento.layers.resolve_image_id", return_value="not-a-digest"):
        with pytest.raises(MalformedReference):
            LayeredImage.resolve_id("weird:latest")


# --------------------------------------------------------------------------- #
# LayeredImage.resolve — delegates to layers.resolve_layers /
# to_overlay_lowerdir / resolve_image_id (does not reimplement them)
# --------------------------------------------------------------------------- #


def test_resolve_delegates_and_populates():
    ref = _ref("ubuntu:latest")
    with patch("kento.layers.resolve_layers",
               return_value="/store/overlay/ID1/diff:/store/overlay/ID2/diff") as m_layers, \
         patch("kento.layers.to_overlay_lowerdir",
               return_value=("/store/overlay", "ID1/diff:ID2/diff")) as m_ovl, \
         patch("kento.layers.resolve_image_id", return_value=DIGEST_STR) as m_id:
        img = LayeredImage.resolve(ref)

    # Delegation: each wrapped function was called with the rendered ref.
    m_layers.assert_called_once_with(ref.render())
    m_ovl.assert_called_once_with(
        "/store/overlay/ID1/diff:/store/overlay/ID2/diff")
    m_id.assert_called_once_with(ref.render())

    assert isinstance(img, LayeredImage)
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


def test_resolve_propagates_image_not_found():
    ref = _ref("ghost:latest")
    with patch("kento.layers.resolve_layers",
               side_effect=ImageNotFoundError("nope")):
        with pytest.raises(ImageNotFoundError):
            LayeredImage.resolve(ref)


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
        img = LayeredImage.resolve(ref)
    assert img.overlay_root == Path("/store/overlay")
    assert img.layers == (
        Layer(id="ID1", short_link=""),
        Layer(id="ID2", short_link=""),
    )
    assert img._render_lowerdir_abs() == abs_layers


def test_resolve_raises_on_no_shared_root():
    # A genuinely unexpected layout (entries don't share a <root>/<id>/diff
    # root) is a typed StateError — NOT a fabricated rootless degenerate value
    # (gate C, principle 5).
    from kento.errors import StateError
    abs_layers = "/a/x/diff:/b/y/diff"  # roots /a and /b disagree
    ref = _ref("ubuntu:latest")
    with patch("kento.layers.resolve_layers", return_value=abs_layers), \
         patch("kento.layers.to_overlay_lowerdir", return_value=("", abs_layers)), \
         patch("kento.layers.resolve_image_id", return_value=DIGEST_STR):
        with pytest.raises(StateError, match="common store root"):
            LayeredImage.resolve(ref)


def test_resolve_raises_on_non_diff_entry():
    from kento.errors import StateError
    abs_layers = "/store/overlay/ID1/notdiff"
    ref = _ref("ubuntu:latest")
    with patch("kento.layers.resolve_layers", return_value=abs_layers), \
         patch("kento.layers.to_overlay_lowerdir", return_value=("", abs_layers)), \
         patch("kento.layers.resolve_image_id", return_value=DIGEST_STR):
        with pytest.raises(StateError, match="<root>/<id>/diff"):
            LayeredImage.resolve(ref)


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
        img = LayeredImage.resolve(ref)
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
    return LayeredImage(
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
    # Inherits Image's abstract primitives unimplemented -> uninstantiable.
    assert VolumeImage.__abstractmethods__ >= {
        "prepare", "mount", "unmount", "release"}
    with pytest.raises(TypeError):
        VolumeImage(  # type: ignore[abstract]
            source=_ref(), id=_digest(),
            path=Path("/img.qcow2"), format=DiskFormat.QCOW2, backing=None,
        )


def test_volumeimage_declares_fields():
    fields = {f.name for f in dataclasses.fields(VolumeImage)}
    assert {"path", "format", "backing"} <= fields


def test_compositeimage_is_abstract_stub():
    assert CompositeImage.__abstractmethods__ >= {
        "prepare", "mount", "unmount", "release"}
    with pytest.raises(TypeError):
        CompositeImage(source=_ref(), id=_digest(), mounts={})  # type: ignore[abstract]


def test_compositeimage_declares_fields():
    fields = {f.name for f in dataclasses.fields(CompositeImage)}
    assert "mounts" in fields


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
