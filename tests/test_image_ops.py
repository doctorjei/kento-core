"""Tests for the ``Image`` content-lifecycle handle ops (Block 06 — kento._images).

``OciImage.pull`` / ``get`` / ``list`` / ``remove`` (§4.4, §11.5 M19–M23).
All are ADDITIVE wrappers over kento.layers / podman; the tests assert
DELEGATION (no forked podman/hold logic) and the disclosed judgment calls:
method placement (classmethods on OciImage), ``list()`` partial-failure
policy (total over the store — skip-with-log), and ``str | OciReference``
normalization (parse a str BEFORE shelling out). All I/O is mocked (no real
podman). Baseline 1442 passed / 1 skipped must stay green.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import kento
from kento import (
    ImageNotFoundError,
    OciImage,
    OciReference,
)
from kento.errors import KentoError, StateError, SubprocessError

SHA = "a" * 64
DIGEST_STR = f"sha256:{SHA}"


def _ref(s="docker.io/library/ubuntu:latest"):
    return OciReference.parse(s).unwrap()


def _ok(args):
    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def _resolved_img(ref):
    """A populated OciImage to stand in for a resolve() result."""
    from kento import Digest, Layer

    return OciImage(
        source=ref,
        id=Digest.parse(DIGEST_STR),
        layers=(Layer(id="ID1", short_link=""),),
        overlay_root=Path("/store/overlay"),
    )


# --------------------------------------------------------------------------- #
# pull (M19) — podman pull then resolve; str|OciReference; typed failure
# --------------------------------------------------------------------------- #


def test_pull_runs_podman_pull_then_resolves():
    # S2 (Result sweep): pull() returns Ok(OciImage); internal resolve is the
    # RAISING _resolve (kind fidelity), so patch _resolve.
    ref = _ref("ubuntu:latest")
    sentinel = _resolved_img(ref)
    with patch("kento.subprocess_util.subprocess.run",
               side_effect=lambda *a, **k: _ok(a[0])) as m_run, \
         patch.object(OciImage, "_resolve", return_value=sentinel) as m_res:
        result = OciImage.pull(ref)

    # Delegation: podman pull <rendered ref>, then _resolve(ref).
    pull_cmds = [c.args[0] for c in m_run.call_args_list]
    assert ["podman", "pull", ref.render()] in pull_cmds
    m_res.assert_called_once_with(ref)
    assert result.unwrap() is sentinel


def test_pull_accepts_str_and_parses_before_shelling_out():
    # A str ref is parsed/validated through OciReference.parse BEFORE podman.
    with patch("kento.subprocess_util.subprocess.run",
               side_effect=lambda *a, **k: _ok(a[0])) as m_run, \
         patch.object(OciImage, "_resolve",
                      side_effect=lambda oci: _resolved_img(oci)) as m_res:
        img = OciImage.pull("ubuntu:latest").unwrap()

    # _resolve() received a parsed OciReference, not the raw string.
    (called_ref,), _ = m_res.call_args
    assert isinstance(called_ref, OciReference)
    assert isinstance(img, OciImage)
    assert ["podman", "pull", called_ref.render()] in \
        [c.args[0] for c in m_run.call_args_list]


def test_pull_malformed_str_returns_error_before_any_podman_call():
    # S2: a malformed ref is caught at pull's boundary -> Error (the parse
    # boundary maps it to MALFORMED_REFERENCE, which is a KentoError -> the
    # fallback INTERNAL kind in _error_from, but the point here is it is a
    # PREDICTABLE Error returned BEFORE any podman call, not a raise).
    with patch("kento.subprocess_util.subprocess.run") as m_run:
        result = OciImage.pull("UPPER/case@bad")  # invalid ref grammar
    assert isinstance(result, kento.Error)
    m_run.assert_not_called()  # never reached the shell
    # .unwrap() still raises a KentoError (behavior-preserving for the CLI).
    with pytest.raises(KentoError):
        result.unwrap()


def test_pull_no_force_param():
    import inspect

    sig = inspect.signature(OciImage.pull)
    assert "force" not in sig.parameters  # M19: force DROPPED


def test_pull_returns_error_subprocess_on_podman_failure():
    # S2: a pull failure -> Error(SUBPROCESS_FAILED), returncode in context.
    ref = _ref("ghost:latest")

    def _fail(*a, **k):
        return subprocess.CompletedProcess(a[0], 125, stdout="",
                                           stderr="manifest unknown")

    with patch("kento.subprocess_util.subprocess.run", side_effect=_fail):
        result = OciImage.pull(ref)
    assert isinstance(result, kento.Error)
    cond = result.conditions[0]
    assert cond.kind is kento.ConditionKind.SUBPROCESS_FAILED
    assert cond.context["returncode"] == 125


# --------------------------------------------------------------------------- #
# get (M20) — resolve a local image; no network; absent raises
# --------------------------------------------------------------------------- #


def test_get_delegates_to_resolve_no_pull():
    # S2: get() returns Ok(OciImage); internal resolve is the RAISING _resolve.
    ref = _ref("ubuntu:latest")
    sentinel = _resolved_img(ref)
    with patch("kento.subprocess_util.subprocess.run") as m_run, \
         patch.object(OciImage, "_resolve", return_value=sentinel) as m_res:
        result = OciImage.get(ref)

    m_res.assert_called_once_with(ref)
    assert result.unwrap() is sentinel
    # get is read-only — it must NOT shell out to `podman pull` itself.
    for call in m_run.call_args_list:
        assert "pull" not in call.args[0]


def test_get_accepts_str():
    with patch.object(OciImage, "_resolve",
                      side_effect=lambda oci: _resolved_img(oci)) as m_res:
        OciImage.get("ubuntu:latest").unwrap()
    (called_ref,), _ = m_res.call_args
    assert isinstance(called_ref, OciReference)


def test_get_absent_returns_error_not_fabricated():
    # S2: _resolve raises ImageNotFoundError when the image isn't local — get
    # catches it at its boundary -> Error(IMAGE_NOT_FOUND); never a handle.
    ref = _ref("ghost:latest")
    with patch.object(OciImage, "_resolve",
                      side_effect=ImageNotFoundError("absent")):
        result = OciImage.get(ref)
    assert isinstance(result, kento.Error)
    assert result.conditions[0].kind is kento.ConditionKind.IMAGE_NOT_FOUND


def test_get_absent_error_through_the_REAL_resolve_path():
    # Anchor get()'s absent path on the REAL resolve chain, not a mocked one.
    # layers.resolve_layers raises ImageNotFoundError; _resolve composes it;
    # get() catches it at its boundary and returns Error(IMAGE_NOT_FOUND). The
    # kind survives the deep raise -> public boundary (KIND-FIDELITY).
    ref = _ref("ghost:latest")
    with patch("kento.layers.resolve_layers",
               side_effect=ImageNotFoundError("not in local store")):
        result = OciImage.get(ref)
    assert isinstance(result, kento.Error)
    assert result.conditions[0].kind is kento.ConditionKind.IMAGE_NOT_FOUND


def test__get_RAISES_kind_fidelity():
    # KIND-FIDELITY mutation guard: the raising _get STAYS RAISING so a deep
    # ImageNotFoundError reaches a still-raising caller (ImageRecord.resolve's
    # body) with its REAL type. If _get .unwrap()'d a Result-returning resolve
    # instead, the type would become ResultError and this reddens.
    ref = _ref("ghost:latest")
    with patch("kento.layers.resolve_layers",
               side_effect=ImageNotFoundError("not in local store")):
        with pytest.raises(ImageNotFoundError):
            OciImage._get(ref)


def test_get_absent_error_when_resolve_id_returns_empty_REAL_path():
    # The OTHER real path: resolve_layers succeeds but resolve_image_id returns
    # "" (error-as-data) — resolve_id must RAISE ImageNotFoundError; get() maps
    # it to Error(IMAGE_NOT_FOUND). Mutating resolve_id to fabricate would make
    # this go from Error to a silent Ok, catching the gap.
    ref = _ref("ubuntu:latest")
    abs_layers = "/store/overlay/ID1/diff"
    with patch("kento.layers.resolve_layers", return_value=abs_layers), \
         patch("kento.layers.to_overlay_lowerdir",
               return_value=("/store/overlay", "ID1/diff")), \
         patch("kento.layers.resolve_image_id", return_value=""):
        result = OciImage.get(ref)
    assert isinstance(result, kento.Error)
    assert result.conditions[0].kind is kento.ConditionKind.IMAGE_NOT_FOUND


def test_pull_absent_after_pull_returns_error_through_REAL_resolve_path():
    # After a (mocked-successful) podman pull, the post-pull _resolve runs for
    # real; if the image still isn't resolvable, pull catches it at its boundary
    # -> Error(IMAGE_NOT_FOUND), never a fabricated handle.
    ref = _ref("ghost:latest")
    with patch("kento.subprocess_util.subprocess.run",
               side_effect=lambda *a, **k: _ok(a[0])), \
         patch("kento.layers.resolve_layers",
               side_effect=ImageNotFoundError("still absent")):
        result = OciImage.pull(ref)
    assert isinstance(result, kento.Error)
    assert result.conditions[0].kind is kento.ConditionKind.IMAGE_NOT_FOUND


# --------------------------------------------------------------------------- #
# list (M21) — podman images query + resolve each; total over the store
# --------------------------------------------------------------------------- #


def _images_query_run(stdout):
    """side_effect: answer the `podman images` enumeration query."""
    def _run(*a, **k):
        return subprocess.CompletedProcess(a[0], 0, stdout=stdout, stderr="")
    return _run


def test_list_enumerates_and_resolves_each():
    # S2: list() returns Ok(list[OciImage]); internal resolve is RAISING _resolve.
    out = "docker.io/library/ubuntu:latest\nquay.io/fedora:39\n"
    with patch("kento.subprocess_util.subprocess.run",
               side_effect=_images_query_run(out)) as m_run, \
         patch.object(OciImage, "_resolve",
                      side_effect=lambda oci: _resolved_img(oci)) as m_res:
        imgs = OciImage.list().unwrap()

    # Delegation: a `podman images` query (NOT images.list_images()).
    query = m_run.call_args_list[0].args[0]
    assert query[:2] == ["podman", "images"]
    assert len(imgs) == 2
    assert m_res.call_count == 2
    assert all(isinstance(i, OciImage) for i in imgs)


def test_list_skips_dangling_none_entries():
    out = "<none>:<none>\ndocker.io/library/ubuntu:latest\n"
    with patch("kento.subprocess_util.subprocess.run",
               side_effect=_images_query_run(out)), \
         patch.object(OciImage, "_resolve",
                      side_effect=lambda oci: _resolved_img(oci)) as m_res:
        imgs = OciImage.list().unwrap()
    assert len(imgs) == 1  # the <none>:<none> dangling entry is skipped
    assert m_res.call_count == 1


def test_list_total_over_store_skips_unresolvable_entry():
    # Disclosed policy: one entry that fails to resolve mid-list is SKIPPED
    # WITH A LOG, never fatal — list() stays total over the store (§2 / §7.2).
    # The per-entry _resolve raise is caught INSIDE the list loop (skip), NOT at
    # the outer boundary, so the overall result is still Ok.
    out = "good/image:latest\nraced/image:latest\n"

    def _resolve(oci):
        if oci.name == "image" and oci.path == "raced":
            raise ImageNotFoundError("raced a removal")
        return _resolved_img(oci)

    with patch("kento.subprocess_util.subprocess.run",
               side_effect=_images_query_run(out)), \
         patch.object(OciImage, "_resolve", side_effect=_resolve):
        result = OciImage.list()
    assert isinstance(result, kento.Ok)
    imgs = result.unwrap()
    # The good image survives; the unresolvable one is dropped, not raised.
    assert len(imgs) == 1
    assert imgs[0].source.name == "image"
    assert imgs[0].source.path == "good"


def test_list_returns_error_when_enumeration_query_itself_fails():
    # A failure of the `podman images` query is the WHOLE listing failing —
    # that becomes Error(SUBPROCESS_FAILED) (distinct from one skipped entry,
    # which stays an Ok with the entry dropped).
    def _fail(*a, **k):
        return subprocess.CompletedProcess(a[0], 1, stdout="",
                                           stderr="podman down")

    with patch("kento.subprocess_util.subprocess.run", side_effect=_fail):
        result = OciImage.list()
    assert isinstance(result, kento.Error)
    assert result.conditions[0].kind is kento.ConditionKind.SUBPROCESS_FAILED


def test_list_empty_store_returns_ok_empty_list():
    with patch("kento.subprocess_util.subprocess.run",
               side_effect=_images_query_run("")):
        result = OciImage.list()
    assert isinstance(result, kento.Ok)
    assert result.unwrap() == []


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
# remove / _is_held — BARE-HEX hold comparison (the real-podman shape).
#
# REGRESSION: create_image_hold stores resolve_image_id's output in its
# io.kento.hold-image-id label, and real podman {{.Id}}/{{.Image}} are a BARE
# 64-hex (NO "sha256:" prefix). self.id.render() is "sha256:<hex>", so the old
# `self.id.render() == <bare hex>` compare NEVER matched → a held image became
# removable WITHOUT force (silent M23 break in the real env). _is_held must
# compare in podman's native bare-hex space (self.id.encoded). _img()'s id is
# Digest.parse(sha256:aaa...), so BARE_HEX below is the realistic hold value.
# --------------------------------------------------------------------------- #

BARE_HEX = SHA  # "a"*64 — exactly self.id.encoded for _img(); a real {{.Id}}


def test_remove_refuses_when_held_by_BARE_HEX_label():
    # The real shape: the io.kento.hold-image-id label is the BARE hex.
    img = _img()
    assert img.id.encoded == BARE_HEX  # guards the fixture assumption
    with patch("kento.images._hold_image_ids",
               return_value={"guest-a": BARE_HEX}), \
         patch("kento.images._holds", return_value=[("guest-a", BARE_HEX)]), \
         patch("kento.subprocess_util.subprocess.run") as m_run:
        with pytest.raises(StateError, match="held"):
            img.remove()
    m_run.assert_not_called()  # refused BEFORE any podman rmi


def test_remove_refuses_when_held_by_BARE_HEX_holds_field_no_label():
    # Modern hold pinning the bare hex via _holds() {{.Image}}, empty label map.
    img = _img()
    with patch("kento.images._hold_image_ids", return_value={}), \
         patch("kento.images._holds", return_value=[("guest-a", BARE_HEX)]), \
         patch("kento.subprocess_util.subprocess.run") as m_run:
        with pytest.raises(StateError, match="held"):
            img.remove()
    m_run.assert_not_called()


def test_remove_not_held_when_BARE_HEX_pins_a_different_image():
    # A hold pinning a DIFFERENT bare hex must NOT block removal of this image.
    img = _img()
    other_hex = "b" * 64
    with patch("kento.images._hold_image_ids",
               return_value={"guest-x": other_hex}), \
         patch("kento.images._holds", return_value=[("guest-x", other_hex)]), \
         patch("kento.subprocess_util.subprocess.run",
               side_effect=lambda *a, **k: _ok(a[0])) as m_run:
        img.remove()
    rmi_cmds = [c.args[0] for c in m_run.call_args_list]
    assert ["podman", "rmi", img.source.render()] in rmi_cmds


def test_is_held_matches_bare_hex_and_prefixed_label():
    # Direct _is_held unit: True for BOTH the real bare-hex label AND a
    # defensively-prefixed "sha256:<hex>" label; False for a different id.
    img = _img()
    rendered = img.source.render()
    with patch("kento.images._holds", return_value=[]):
        with patch("kento.images._hold_image_ids",
                   return_value={"g": BARE_HEX}):
            assert img._is_held(rendered) is True       # bare hex (real)
        with patch("kento.images._hold_image_ids",
                   return_value={"g": f"sha256:{BARE_HEX}"}):
            assert img._is_held(rendered) is True       # prefixed (defensive)
        with patch("kento.images._hold_image_ids",
                   return_value={"g": "c" * 64}):
            assert img._is_held(rendered) is False      # different id


# --------------------------------------------------------------------------- #
# No forked podman/hold logic — ops compose existing functions only
# --------------------------------------------------------------------------- #


def test_ops_query_podman_images_directly():
    # list() composes the `podman images` enumeration directly. (The former
    # string-returning images.list_images() it was once asserted NOT to wrap was
    # REMOVED in SD3 — the library no longer renders a display table; the typed
    # ImageRecord ledger replaced it. So the guarantee is now structural: there
    # is no display-table builder to wrap.)
    out = "docker.io/library/ubuntu:latest\n"
    with patch("kento.subprocess_util.subprocess.run",
               side_effect=_images_query_run(out)) as m_run, \
         patch.object(OciImage, "_resolve",
                      side_effect=lambda oci: _resolved_img(oci)):
        OciImage.list().unwrap()
    assert not hasattr(__import__("kento.images", fromlist=["x"]), "list_images")
    assert m_run.call_args_list[0].args[0][:2] == ["podman", "images"]


# --------------------------------------------------------------------------- #
# prune (M22) — reclaim DANGLING images; NEVER a held one; surface refusals.
# Block 07. Classmethod (store-level GC), locked sig scope=PruneScope.DANGLING,
# no dry_run, ReclaimReport(dry_run=False, ...).
# --------------------------------------------------------------------------- #

ID_A = f"sha256:{'a' * 64}"
ID_B = f"sha256:{'b' * 64}"
ID_C = f"sha256:{'c' * 64}"


def _dispatch_run(*, dangling, rmi_fail=()):
    """side_effect dispatching the dangling-images query and per-id rmi calls.

    ``dangling`` = the {{.Id}} lines podman returns; ``rmi_fail`` = a set of ids
    whose ``podman rmi`` returns non-zero (a refusal surfaced in ``failed``).
    """
    rmi_fail = set(rmi_fail)

    def _run(*a, **k):
        argv = a[0]
        if "images" in argv and "dangling=true" in argv:
            body = "".join(f"{i}\n" for i in dangling)
            return subprocess.CompletedProcess(argv, 0, stdout=body, stderr="")
        if "rmi" in argv:
            target = argv[-1]
            if target in rmi_fail:
                return subprocess.CompletedProcess(
                    argv, 2, stdout="", stderr="image is in use")
            return _ok(argv)
        return _ok(argv)

    return _run


def test_prune_signature_is_locked_scope_only_no_dry_run():
    import inspect

    sig = inspect.signature(OciImage.prune)
    # Exactly one param `scope`, keyword-only, default PruneScope.DANGLING.
    assert list(sig.parameters) == ["scope"]
    p = sig.parameters["scope"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY
    from kento import PruneScope

    assert p.default is PruneScope.DANGLING
    # No un-spec'd dry_run / yes parameter.
    assert "dry_run" not in sig.parameters
    assert "yes" not in sig.parameters
    # It is a classmethod (store-level GC), mirroring pull/get/list.
    assert isinstance(inspect.getattr_static(OciImage, "prune"), classmethod)


def test_prune_removes_dangling_and_reports_dry_run_false():
    from kento import ReclaimReport

    with patch("kento.images._hold_image_ids", return_value={}), \
         patch("kento.images._holds", return_value=[]), \
         patch("kento.subprocess_util.subprocess.run",
               side_effect=_dispatch_run(dangling=[ID_A, ID_B])) as m_run:
        report = OciImage.prune().unwrap()

    assert isinstance(report, ReclaimReport)
    # prune EXECUTES — never a dry run (locked sig has no dry_run param).
    assert report.dry_run is False
    assert set(report.reclaimed) == {ID_A, ID_B}
    assert report.failed == ()
    assert report.ok is True
    # Delegation: a `podman images --filter dangling=true` enumeration, then a
    # `podman rmi <id>` per candidate.
    cmds = [c.args[0] for c in m_run.call_args_list]
    assert any("images" in c and "dangling=true" in c for c in cmds)
    assert ["podman", "rmi", ID_A] in cmds
    assert ["podman", "rmi", ID_B] in cmds


def test_prune_empty_store_returns_empty_report():
    from kento import ReclaimReport

    with patch("kento.images._hold_image_ids", return_value={}), \
         patch("kento.images._holds", return_value=[]), \
         patch("kento.subprocess_util.subprocess.run",
               side_effect=_dispatch_run(dangling=[])) as m_run:
        report = OciImage.prune().unwrap()

    assert report == ReclaimReport(dry_run=False, reclaimed=(), failed=())
    # No rmi attempted on an empty dangling set.
    assert all("rmi" not in c.args[0] for c in m_run.call_args_list)


def test_prune_NEVER_touches_a_held_image_even_if_listed_via_label():
    # THE spec invariant: a held image that somehow appears as dangling must be
    # skipped. Hold identified by CONTENT ID (io.kento.hold-image-id label),
    # matched against the dangling {{.Id}} — guaranteed, not trusting podman's
    # filter to have excluded it.
    with patch("kento.images._hold_image_ids",
               return_value={"guest-a": ID_A}), \
         patch("kento.images._holds", return_value=[("guest-a", ID_A)]), \
         patch("kento.subprocess_util.subprocess.run",
               side_effect=_dispatch_run(dangling=[ID_A, ID_B])) as m_run:
        report = OciImage.prune().unwrap()

    # ID_A is held → NEVER removed; only the genuinely-unreferenced ID_B is.
    assert set(report.reclaimed) == {ID_B}
    rmi_cmds = [c.args[0] for c in m_run.call_args_list if "rmi" in c.args[0]]
    assert ["podman", "rmi", ID_A] not in rmi_cmds  # held image untouched
    assert ["podman", "rmi", ID_B] in rmi_cmds


def test_prune_held_skip_via_holds_image_field_no_label():
    # A modern hold whose {{.Image}} is the content id but the label map missed
    # it — the _holds() fallback must still pin (exclude) it.
    with patch("kento.images._hold_image_ids", return_value={}), \
         patch("kento.images._holds", return_value=[("guest-a", ID_A)]), \
         patch("kento.subprocess_util.subprocess.run",
               side_effect=_dispatch_run(dangling=[ID_A, ID_B])) as m_run:
        report = OciImage.prune().unwrap()

    assert set(report.reclaimed) == {ID_B}
    rmi_cmds = [c.args[0] for c in m_run.call_args_list if "rmi" in c.args[0]]
    assert ["podman", "rmi", ID_A] not in rmi_cmds


def test_prune_reuses_existing_hold_knowledge_no_forked_logic():
    # Held-detection composes the existing images._hold_image_ids/_holds — it
    # does NOT re-query holds itself.
    with patch("kento.images._hold_image_ids", return_value={}) as m_ids, \
         patch("kento.images._holds", return_value=[]) as m_holds, \
         patch("kento.subprocess_util.subprocess.run",
               side_effect=_dispatch_run(dangling=[ID_A])):
        OciImage.prune().unwrap()
    assert m_ids.called
    assert m_holds.called


def test_prune_surfaces_rmi_refusal_in_failed_not_swallowed():
    # A `podman rmi` refusal is surfaced as a (id, reason) pair in `failed`,
    # never swallowed (1.6.2 contract); the batch does NOT abort — the other
    # candidate still gets removed.
    with patch("kento.images._hold_image_ids", return_value={}), \
         patch("kento.images._holds", return_value=[]), \
         patch("kento.subprocess_util.subprocess.run",
               side_effect=_dispatch_run(dangling=[ID_A, ID_B],
                                         rmi_fail=[ID_A])):
        report = OciImage.prune().unwrap()

    assert report.reclaimed == (ID_B,)            # batch continued past ID_A
    assert len(report.failed) == 1
    target, reason = report.failed[0]
    assert target == ID_A
    assert reason                                  # a non-empty reason string
    assert report.ok is False                      # derived from failed


def test_prune_returns_error_when_enumeration_query_fails():
    # S2: a failure of the dangling-images enumeration is the WHOLE prune failing
    # -> Error(SUBPROCESS_FAILED) (distinct from a per-image refusal, which stays
    # in the Ok report's `failed`).
    def _fail(*a, **k):
        return subprocess.CompletedProcess(a[0], 1, stdout="",
                                           stderr="podman down")

    with patch("kento.images._hold_image_ids", return_value={}), \
         patch("kento.images._holds", return_value=[]), \
         patch("kento.subprocess_util.subprocess.run", side_effect=_fail):
        result = OciImage.prune()
    assert isinstance(result, kento.Error)
    assert result.conditions[0].kind is kento.ConditionKind.SUBPROCESS_FAILED


def test_prune_dedupes_repeated_dangling_ids():
    # A defensively-deduped enumeration: a repeated id is removed once.
    with patch("kento.images._hold_image_ids", return_value={}), \
         patch("kento.images._holds", return_value=[]), \
         patch("kento.subprocess_util.subprocess.run",
               side_effect=_dispatch_run(dangling=[ID_A, ID_A])) as m_run:
        report = OciImage.prune().unwrap()

    assert report.reclaimed == (ID_A,)
    rmi_calls = [c for c in m_run.call_args_list
                 if "rmi" in c.args[0] and c.args[0][-1] == ID_A]
    assert len(rmi_calls) == 1


def test_prune_rejects_unsupported_scope_returns_error():
    # S2: DANGLING is the only 1.0 value. A future/other scope value cannot occur
    # today, but must fail loudly -> Error(VALIDATION) rather than silently
    # pruning DANGLING anyway (gate C). Simulate one with a stand-in member.
    from enum import Enum

    class _FakeScope(str, Enum):
        OTHER = "other"

    with patch("kento.images._hold_image_ids", return_value={}), \
         patch("kento.images._holds", return_value=[]), \
         patch("kento.subprocess_util.subprocess.run",
               side_effect=_dispatch_run(dangling=[ID_A])) as m_run:
        result = OciImage.prune(scope=_FakeScope.OTHER)
    assert isinstance(result, kento.Error)
    assert result.conditions[0].kind is kento.ConditionKind.VALIDATION
    # Rejected BEFORE any podman call.
    m_run.assert_not_called()


# --------------------------------------------------------------------------- #
# Block 10 — image.diagnose() (§11.8 D3 b). ADDITIVE wrapper over
# diagnose.run_diagnostics; returns the COLLECTION-level IMAGE-domain findings
# (hold health). NOT per-image (disclosed: no clean per-image subject today).
# run_diagnostics is reached via the diagnose SUBMODULE (not the kento.diagnose
# FUNCTION) — the patch target is kento.diagnose.run_diagnostics.
# --------------------------------------------------------------------------- #

from kento import Diagnosis, DiagnosisDomain  # noqa: E402


def _diag_report(*findings):
    return {"checks": list(findings),
            "problem_count": sum(1 for f in findings
                                 if f["severity"] in ("warn", "error")),
            "instances_scanned": 0}


def _df(category, severity, scope, message="m", remediation=None):
    return {"category": category, "severity": severity, "scope": scope,
            "message": message, "remediation": remediation}


def test_image_diagnose_returns_image_domain_findings_only():
    report = _diag_report(
        _df("hold", "ok", "host", "no stale holds"),
        _df("apparmor", "ok", "host"),          # HOST — dropped
        _df("status", "ok", "mybox"),           # INSTANCE — dropped
        _df("orphan", "warn", "ghost"),         # HOST — dropped
    )
    img = _resolved_img(_ref())
    with patch("kento.diagnose.run_diagnostics",
               return_value=report) as mock_run:
        result = img.diagnose()
    # Global scan (None), then project to the IMAGE domain.
    mock_run.assert_called_once_with(None)
    assert isinstance(result, Diagnosis)
    assert [f.check for f in result.findings] == ["hold"]
    assert all(f.domain is DiagnosisDomain.IMAGE for f in result.findings)


def test_image_diagnose_collection_level_not_per_image():
    # Two holds, neither attributed to this specific image (scope is "host",
    # subject None) — both surface (collection-level), proving it is NOT a
    # per-image filter on self.
    report = _diag_report(
        _df("hold", "warn", "host", "stale hold for guest-a", "kento prune"),
        _df("hold", "warn", "host", "drift for guest-b", "kento scrub guest-b"),
    )
    img = _resolved_img(_ref("docker.io/library/alpine:3"))
    with patch("kento.diagnose.run_diagnostics", return_value=report):
        result = img.diagnose()
    assert len(result.findings) == 2
    assert all(f.domain is DiagnosisDomain.IMAGE for f in result.findings)
    assert all(f.subject is None for f in result.findings)  # no per-image subj
    assert result.ok is False


def test_image_diagnose_is_instance_method_on_layeredimage():
    assert "diagnose" in OciImage.__dict__
