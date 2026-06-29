"""Tests for kento image GC (``kento prune`` orphaned-hold collector).

The former string-returning ``list_images()`` was REMOVED in SD3 (the library no
longer renders the ``kento images`` table; the typed ``ImageRecord`` ledger
replaced it — see ``test_image_record.py``). This file now covers ``prune()``
(the orphaned-hold GC, Delta-1 backlog) plus a guard that the string surface is
gone.
"""

import subprocess
from unittest.mock import patch

import kento.images as images_mod
from kento.images import prune


def test_list_images_string_surface_removed():
    """SD3: the string-returning ``list_images`` is gone (classes-only seam)."""
    assert not hasattr(images_mod, "list_images")


def _mk_guest(base, dir_name, image, name=None):
    d = base / dir_name
    d.mkdir(parents=True, exist_ok=True)
    (d / "kento-image").write_text(image + "\n")
    if name is not None:
        (d / "kento-name").write_text(name + "\n")
    return d


def _holds_mock(holds):
    """Build a subprocess.run side_effect that answers the hold-enum query.

    holds: list of (held_for_name, image) tuples.
    Any other podman invocation returns rc=0 with empty output.
    """
    def _run(args, **kwargs):
        result = subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if "ps" in args and any("io.kento.hold-for" in a for a in args):
            result.stdout = "".join(f"{n}\t{img}\n" for n, img in holds)
        return result
    return _run


# --- prune dry-run -------------------------------------------------------


def test_prune_dry_run_makes_no_destructive_calls(tmp_path):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    _mk_guest(lxc, "box", "imageA:latest", name="box")
    holds = [("box", "imageA:latest"), ("ghost", "imageB:latest")]

    with patch("kento.images.LXC_BASE", lxc), \
         patch("kento.images.VM_BASE", vm), \
         patch("kento.images.subprocess.run",
               side_effect=_holds_mock(holds)) as mock_run, \
         patch("kento.images.remove_image_hold") as mock_rm:
        result, failed = prune()

    assert failed == 0
    assert "kento-hold.ghost" in result
    assert "imageB:latest" in result
    assert "kento prune --yes" in result
    # No removal helper, no rm / image rm subprocess calls.
    mock_rm.assert_not_called()
    for call in mock_run.call_args_list:
        argv = call.args[0] if call.args else call.kwargs.get("args", [])
        assert "rm" not in argv, f"prune dry-run must not call rm: {argv}"


# --- prune --yes ---------------------------------------------------------


def test_prune_yes_removes_orphaned_only(tmp_path):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    _mk_guest(lxc, "box", "imageA:latest", name="box")
    # box's guest exists -> its hold must survive; ghost is orphaned.
    holds = [("box", "imageA:latest"), ("ghost", "imageB:latest")]

    def _run(args, **kwargs):
        result = subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if "ps" in args and any("io.kento.hold-for" in a for a in args):
            result.stdout = "".join(f"{n}\t{img}\n" for n, img in holds)
        return result

    with patch("kento.images.LXC_BASE", lxc), \
         patch("kento.images.VM_BASE", vm), \
         patch("kento.images.subprocess.run", side_effect=_run) as mock_run, \
         patch("kento.images.remove_image_hold") as mock_rm:
        result, failed = prune(yes=True)

    # Only the orphaned hold removed.
    mock_rm.assert_called_once_with("ghost")
    # imageB freed (no guest, no surviving hold) -> image rm attempted.
    img_rm_calls = [
        c for c in mock_run.call_args_list
        if (c.args and "image" in c.args[0] and "rm" in c.args[0])
    ]
    assert len(img_rm_calls) == 1
    assert "imageB:latest" in img_rm_calls[0].args[0]
    assert "Removed 1 orphaned hold(s), 1 image(s)." in result
    assert failed == 0


def test_prune_yes_tolerates_image_rm_failure(tmp_path, caplog):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    holds = [("ghost", "imageB:latest")]

    def _run(args, **kwargs):
        if "ps" in args and any("io.kento.hold-for" in a for a in args):
            return subprocess.CompletedProcess(
                args, 0, stdout="ghost\timageB:latest\n", stderr="")
        if "image" in args and "rm" in args:
            return subprocess.CompletedProcess(
                args, 2, stdout="", stderr="image is in use by a container")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    with patch("kento.images.LXC_BASE", lxc), \
         patch("kento.images.VM_BASE", vm), \
         patch("kento.images.subprocess.run", side_effect=_run), \
         patch("kento.images.remove_image_hold"):
        result, failed = prune(yes=True)

    # Hold removed (1), image refused (0) -> reported, not swallowed.
    assert "Removed 1 orphaned hold(s), 0 image(s)." in result
    # The failing image and its reason are surfaced in the summary text,
    # and the failure count is non-zero so the CLI can exit non-zero.
    assert "Failed to remove 1 image(s)" in result
    assert "imageB:latest" in result
    assert "image is in use by a container" in result
    assert failed == 1


def test_prune_image_pinned_by_surviving_hold_not_removed(tmp_path):
    """An orphaned hold's image is NOT removed if another hold (surviving) pins it."""
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    _mk_guest(lxc, "box", "shared:latest", name="box")
    # box (alive) and ghost (orphaned) both pin shared:latest.
    holds = [("box", "shared:latest"), ("ghost", "shared:latest")]

    def _run(args, **kwargs):
        result = subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if "ps" in args and any("io.kento.hold-for" in a for a in args):
            result.stdout = "".join(f"{n}\t{img}\n" for n, img in holds)
        return result

    with patch("kento.images.LXC_BASE", lxc), \
         patch("kento.images.VM_BASE", vm), \
         patch("kento.images.subprocess.run", side_effect=_run) as mock_run, \
         patch("kento.images.remove_image_hold") as mock_rm:
        result, failed = prune(yes=True)

    mock_rm.assert_called_once_with("ghost")
    img_rm_calls = [
        c for c in mock_run.call_args_list
        if (c.args and "image" in c.args[0] and "rm" in c.args[0])
    ]
    assert img_rm_calls == []
    assert "Removed 1 orphaned hold(s), 0 image(s)." in result
    assert failed == 0


def test_prune_nothing_to_do(tmp_path):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    _mk_guest(lxc, "box", "imageA:latest", name="box")
    holds = [("box", "imageA:latest")]

    with patch("kento.images.LXC_BASE", lxc), \
         patch("kento.images.VM_BASE", vm), \
         patch("kento.images.subprocess.run", side_effect=_holds_mock(holds)), \
         patch("kento.images.remove_image_hold") as mock_rm:
        result, failed = prune(yes=True)

    assert "Nothing to prune." in result
    assert failed == 0
    mock_rm.assert_not_called()
