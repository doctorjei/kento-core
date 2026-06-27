"""Tests for the ``Image`` content-lifecycle handle ops (Block 06 — kento._images).

``LayeredImage.pull`` / ``get`` / ``list`` / ``remove`` (§4.4, §11.5 M19–M23).
All are ADDITIVE wrappers over kento.layers / podman; the tests assert
DELEGATION (no forked podman/hold logic) and the disclosed judgment calls:
method placement (classmethods on LayeredImage), ``list()`` partial-failure
policy (total over the store — skip-with-log), and ``str | OciReference``
normalization (parse a str BEFORE shelling out). All I/O is mocked (no real
podman). Baseline 1442 passed / 1 skipped must stay green.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from kento import (
    ImageNotFoundError,
    LayeredImage,
    MalformedReference,
    OciReference,
)
from kento.errors import StateError, SubprocessError

SHA = "a" * 64
DIGEST_STR = f"sha256:{SHA}"


def _ref(s="docker.io/library/ubuntu:latest"):
    return OciReference.parse(s)


def _ok(args):
    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def _resolved_img(ref):
    """A populated LayeredImage to stand in for a resolve() result."""
    from kento import Digest, Layer

    return LayeredImage(
        source=ref,
        id=Digest.parse(DIGEST_STR),
        layers=(Layer(id="ID1", short_link=""),),
        overlay_root=Path("/store/overlay"),
    )


# --------------------------------------------------------------------------- #
# pull (M19) — podman pull then resolve; str|OciReference; typed failure
# --------------------------------------------------------------------------- #


def test_pull_runs_podman_pull_then_resolves():
    ref = _ref("ubuntu:latest")
    sentinel = _resolved_img(ref)
    with patch("kento.subprocess_util.subprocess.run",
               side_effect=lambda *a, **k: _ok(a[0])) as m_run, \
         patch.object(LayeredImage, "resolve", return_value=sentinel) as m_res:
        img = LayeredImage.pull(ref)

    # Delegation: podman pull <rendered ref>, then resolve(ref).
    pull_cmds = [c.args[0] for c in m_run.call_args_list]
    assert ["podman", "pull", ref.render()] in pull_cmds
    m_res.assert_called_once_with(ref)
    assert img is sentinel


def test_pull_accepts_str_and_parses_before_shelling_out():
    # A str ref is parsed/validated through OciReference.parse BEFORE podman.
    with patch("kento.subprocess_util.subprocess.run",
               side_effect=lambda *a, **k: _ok(a[0])) as m_run, \
         patch.object(LayeredImage, "resolve",
                      side_effect=lambda oci: _resolved_img(oci)) as m_res:
        img = LayeredImage.pull("ubuntu:latest")

    # resolve() received a parsed OciReference, not the raw string.
    (called_ref,), _ = m_res.call_args
    assert isinstance(called_ref, OciReference)
    assert isinstance(img, LayeredImage)
    assert ["podman", "pull", called_ref.render()] in \
        [c.args[0] for c in m_run.call_args_list]


def test_pull_malformed_str_raises_before_any_podman_call():
    with patch("kento.subprocess_util.subprocess.run") as m_run:
        with pytest.raises(MalformedReference):
            LayeredImage.pull("UPPER/case@bad")  # invalid ref grammar
    m_run.assert_not_called()  # never reached the shell


def test_pull_no_force_param():
    import inspect

    sig = inspect.signature(LayeredImage.pull)
    assert "force" not in sig.parameters  # M19: force DROPPED


def test_pull_raises_typed_on_podman_failure():
    ref = _ref("ghost:latest")

    def _fail(*a, **k):
        return subprocess.CompletedProcess(a[0], 125, stdout="",
                                           stderr="manifest unknown")

    with patch("kento.subprocess_util.subprocess.run", side_effect=_fail):
        with pytest.raises(SubprocessError):
            LayeredImage.pull(ref)


# --------------------------------------------------------------------------- #
# get (M20) — resolve a local image; no network; absent raises
# --------------------------------------------------------------------------- #


def test_get_delegates_to_resolve_no_pull():
    ref = _ref("ubuntu:latest")
    sentinel = _resolved_img(ref)
    with patch("kento.subprocess_util.subprocess.run") as m_run, \
         patch.object(LayeredImage, "resolve", return_value=sentinel) as m_res:
        img = LayeredImage.get(ref)

    m_res.assert_called_once_with(ref)
    assert img is sentinel
    # get is read-only — it must NOT shell out to `podman pull` itself.
    for call in m_run.call_args_list:
        assert "pull" not in call.args[0]


def test_get_accepts_str():
    with patch.object(LayeredImage, "resolve",
                      side_effect=lambda oci: _resolved_img(oci)) as m_res:
        LayeredImage.get("ubuntu:latest")
    (called_ref,), _ = m_res.call_args
    assert isinstance(called_ref, OciReference)


def test_get_absent_raises_not_fabricated():
    # resolve() raises ImageNotFoundError when the image isn't local — get
    # propagates it; it does NOT fabricate a handle.
    ref = _ref("ghost:latest")
    with patch.object(LayeredImage, "resolve",
                      side_effect=ImageNotFoundError("absent")):
        with pytest.raises(ImageNotFoundError):
            LayeredImage.get(ref)


def test_get_absent_raises_through_the_REAL_resolve_path():
    # m1 coverage gap: anchor get()'s absent-raise on the REAL resolve chain,
    # not a mocked resolve. layers.resolve_layers raises ImageNotFoundError for
    # an absent image; resolve() composes it; get() must propagate. (If resolve
    # silently fabricated a handle instead, THIS test reddens.)
    ref = _ref("ghost:latest")
    with patch("kento.layers.resolve_layers",
               side_effect=ImageNotFoundError("not in local store")):
        with pytest.raises(ImageNotFoundError):
            LayeredImage.get(ref)


def test_get_absent_raises_when_resolve_id_returns_empty_REAL_path():
    # The OTHER real raise path: resolve_layers succeeds but resolve_image_id
    # returns "" (error-as-data) — resolve_id must RAISE ImageNotFoundError
    # rather than fabricate a Digest. Mutating resolve_id to fabricate would
    # make this go from RED (raise) to a silent pass, catching the m1 gap.
    ref = _ref("ubuntu:latest")
    abs_layers = "/store/overlay/ID1/diff"
    with patch("kento.layers.resolve_layers", return_value=abs_layers), \
         patch("kento.layers.to_overlay_lowerdir",
               return_value=("/store/overlay", "ID1/diff")), \
         patch("kento.layers.resolve_image_id", return_value=""):
        with pytest.raises(ImageNotFoundError):
            LayeredImage.get(ref)


def test_pull_absent_after_pull_raises_through_REAL_resolve_path():
    # After a (mocked-successful) podman pull, the post-pull resolve runs for
    # real; if the image still isn't resolvable, pull propagates the raise
    # instead of returning a fabricated handle.
    ref = _ref("ghost:latest")
    with patch("kento.subprocess_util.subprocess.run",
               side_effect=lambda *a, **k: _ok(a[0])), \
         patch("kento.layers.resolve_layers",
               side_effect=ImageNotFoundError("still absent")):
        with pytest.raises(ImageNotFoundError):
            LayeredImage.pull(ref)


# --------------------------------------------------------------------------- #
# list (M21) — podman images query + resolve each; total over the store
# --------------------------------------------------------------------------- #


def _images_query_run(stdout):
    """side_effect: answer the `podman images` enumeration query."""
    def _run(*a, **k):
        return subprocess.CompletedProcess(a[0], 0, stdout=stdout, stderr="")
    return _run


def test_list_enumerates_and_resolves_each():
    out = "docker.io/library/ubuntu:latest\nquay.io/fedora:39\n"
    with patch("kento.subprocess_util.subprocess.run",
               side_effect=_images_query_run(out)) as m_run, \
         patch.object(LayeredImage, "resolve",
                      side_effect=lambda oci: _resolved_img(oci)) as m_res:
        imgs = LayeredImage.list()

    # Delegation: a `podman images` query (NOT images.list_images()).
    query = m_run.call_args_list[0].args[0]
    assert query[:2] == ["podman", "images"]
    assert len(imgs) == 2
    assert m_res.call_count == 2
    assert all(isinstance(i, LayeredImage) for i in imgs)


def test_list_skips_dangling_none_entries():
    out = "<none>:<none>\ndocker.io/library/ubuntu:latest\n"
    with patch("kento.subprocess_util.subprocess.run",
               side_effect=_images_query_run(out)), \
         patch.object(LayeredImage, "resolve",
                      side_effect=lambda oci: _resolved_img(oci)) as m_res:
        imgs = LayeredImage.list()
    assert len(imgs) == 1  # the <none>:<none> dangling entry is skipped
    assert m_res.call_count == 1


def test_list_total_over_store_skips_unresolvable_entry():
    # Disclosed policy: one entry that fails to resolve mid-list is SKIPPED
    # WITH A LOG, never fatal — list() stays total over the store (§2 / §7.2).
    out = "good/image:latest\nraced/image:latest\n"

    def _resolve(oci):
        if oci.name == "image" and oci.path == "raced":
            raise ImageNotFoundError("raced a removal")
        return _resolved_img(oci)

    with patch("kento.subprocess_util.subprocess.run",
               side_effect=_images_query_run(out)), \
         patch.object(LayeredImage, "resolve", side_effect=_resolve):
        imgs = LayeredImage.list()
    # The good image survives; the unresolvable one is dropped, not raised.
    assert len(imgs) == 1
    assert imgs[0].source.name == "image"
    assert imgs[0].source.path == "good"


def test_list_raises_when_enumeration_query_itself_fails():
    # A failure of the `podman images` query is the WHOLE listing failing —
    # that DOES raise (distinct from one unresolvable entry).
    def _fail(*a, **k):
        return subprocess.CompletedProcess(a[0], 1, stdout="",
                                           stderr="podman down")

    with patch("kento.subprocess_util.subprocess.run", side_effect=_fail):
        with pytest.raises(SubprocessError):
            LayeredImage.list()


def test_list_empty_store_returns_empty_list():
    with patch("kento.subprocess_util.subprocess.run",
               side_effect=_images_query_run("")):
        assert LayeredImage.list() == []


# --------------------------------------------------------------------------- #
# remove (M23) — podman rmi; refuse if held unless force; reuse images._holds
# --------------------------------------------------------------------------- #


def _img(ref_str="docker.io/library/ubuntu:latest"):
    return _resolved_img(_ref(ref_str))


# A hold pins by CONTENT ID, not repo:tag (layers.create_image_hold:
# resolved {{.Id}} + io.kento.hold-image-id label). _img()'s digest is
# sha256:aaa... — so a REALISTIC hold pins exactly that content id.
CONTENT_ID = DIGEST_STR  # what self.id.render() yields for _img()


def test_remove_runs_podman_rmi_when_unheld():
    img = _img()
    rendered = img.source.render()
    with patch("kento.images._hold_image_ids", return_value={}) as m_ids, \
         patch("kento.images._holds", return_value=[]) as m_holds, \
         patch("kento.subprocess_util.subprocess.run",
               side_effect=lambda *a, **k: _ok(a[0])) as m_run:
        result = img.remove()

    assert result is None
    # Reuses existing hold knowledge (no forked hold logic).
    assert m_ids.called or m_holds.called
    rmi_cmds = [c.args[0] for c in m_run.call_args_list]
    assert ["podman", "rmi", rendered] in rmi_cmds


def test_remove_refuses_when_held_by_content_id_label():
    # Realistic modern hold: io.kento.hold-image-id label = the content id.
    # The OLD broken check (repo:tag compare) would NOT have caught this.
    img = _img()
    with patch("kento.images._hold_image_ids",
               return_value={"guest-a": CONTENT_ID}), \
         patch("kento.images._holds", return_value=[("guest-a", CONTENT_ID)]), \
         patch("kento.subprocess_util.subprocess.run") as m_run:
        with pytest.raises(StateError, match="held"):
            img.remove()
    m_run.assert_not_called()  # refused BEFORE any podman rmi


def test_remove_refuses_when_held_by_holds_content_id_no_label():
    # Modern hold whose {{.Image}} is the content id but the label map is empty
    # (e.g. the _hold_image_ids query missed it) — the _holds() fallback pins.
    img = _img()
    with patch("kento.images._hold_image_ids", return_value={}), \
         patch("kento.images._holds", return_value=[("guest-a", CONTENT_ID)]), \
         patch("kento.subprocess_util.subprocess.run") as m_run:
        with pytest.raises(StateError, match="held"):
            img.remove()
    m_run.assert_not_called()


def test_remove_refuses_when_legacy_hold_pinned_by_tag():
    # Legacy hold (older podman, id unresolvable at create) pins by repo:tag,
    # so _holds() {{.Image}} is the rendered ref — the rendered-tag fallback
    # must still catch it.
    img = _img()
    rendered = img.source.render()
    with patch("kento.images._hold_image_ids", return_value={}), \
         patch("kento.images._holds", return_value=[("guest-a", rendered)]), \
         patch("kento.subprocess_util.subprocess.run") as m_run:
        with pytest.raises(StateError, match="held"):
            img.remove()
    m_run.assert_not_called()


def test_remove_not_held_when_hold_pins_a_different_image():
    # A hold for some OTHER image (different content id AND tag) must NOT block
    # removal of this one.
    img = _img()
    other_id = f"sha256:{'b' * 64}"
    with patch("kento.images._hold_image_ids",
               return_value={"guest-x": other_id}), \
         patch("kento.images._holds", return_value=[("guest-x", other_id)]), \
         patch("kento.subprocess_util.subprocess.run",
               side_effect=lambda *a, **k: _ok(a[0])) as m_run:
        img.remove()
    rmi_cmds = [c.args[0] for c in m_run.call_args_list]
    assert ["podman", "rmi", img.source.render()] in rmi_cmds


def test_remove_force_removes_past_a_hold():
    img = _img()
    rendered = img.source.render()
    with patch("kento.images._hold_image_ids",
               return_value={"guest-a": CONTENT_ID}), \
         patch("kento.images._holds", return_value=[("guest-a", CONTENT_ID)]), \
         patch("kento.subprocess_util.subprocess.run",
               side_effect=lambda *a, **k: _ok(a[0])) as m_run:
        img.remove(force=True)

    rmi_cmds = [c.args[0] for c in m_run.call_args_list]
    assert ["podman", "rmi", "--force", rendered] in rmi_cmds


def test_remove_not_found_raises_typed():
    img = _img()

    def _fail(*a, **k):
        return subprocess.CompletedProcess(a[0], 1, stdout="",
                                           stderr="no such image")

    with patch("kento.images._hold_image_ids", return_value={}), \
         patch("kento.images._holds", return_value=[]), \
         patch("kento.subprocess_util.subprocess.run", side_effect=_fail):
        with pytest.raises(SubprocessError):
            img.remove()


def test_remove_force_does_not_consult_holds():
    # force removes unconditionally — no need to query holds at all.
    img = _img()
    with patch("kento.images._hold_image_ids") as m_ids, \
         patch("kento.images._holds") as m_holds, \
         patch("kento.subprocess_util.subprocess.run",
               side_effect=lambda *a, **k: _ok(a[0])):
        img.remove(force=True)
    m_ids.assert_not_called()
    m_holds.assert_not_called()


# --------------------------------------------------------------------------- #
# No forked podman/hold logic — ops compose existing functions only
# --------------------------------------------------------------------------- #


def test_ops_do_not_wrap_display_list_images():
    # list() must NOT call images.list_images() (a display TABLE STRING).
    out = "docker.io/library/ubuntu:latest\n"
    with patch("kento.images.list_images") as m_display, \
         patch("kento.subprocess_util.subprocess.run",
               side_effect=_images_query_run(out)), \
         patch.object(LayeredImage, "resolve",
                      side_effect=lambda oci: _resolved_img(oci)):
        LayeredImage.list()
    m_display.assert_not_called()
