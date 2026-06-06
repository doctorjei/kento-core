"""Tests for kento images / kento prune (image listing + safe GC)."""

import subprocess
from unittest.mock import patch

from kento.images import list_images, prune


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


# --- list_images ---------------------------------------------------------


def test_list_in_use_and_orphaned(tmp_path, capsys):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    _mk_guest(lxc, "box", "imageA:latest", name="box")
    # Hold for A (guest exists) + orphaned hold for B (no guest).
    holds = [("box", "imageA:latest"), ("ghost", "imageB:latest")]

    with patch("kento.images.LXC_BASE", lxc), \
         patch("kento.images.VM_BASE", vm), \
         patch("kento.images.subprocess.run", side_effect=_holds_mock(holds)):
        list_images()

    out = capsys.readouterr().out
    assert "imageA:latest" in out
    assert "imageB:latest" in out
    assert "in-use" in out
    assert "orphaned" in out
    # A has a hold and a guest; B is orphaned.
    lines = out.strip().split("\n")
    a_line = next(l for l in lines if "imageA" in l)
    b_line = next(l for l in lines if "imageB" in l)
    assert "in-use" in a_line and "yes" in a_line and "box" in a_line
    assert "orphaned" in b_line and "yes" in b_line


def test_list_in_use_filter_hides_orphaned(tmp_path, capsys):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    _mk_guest(lxc, "box", "imageA:latest", name="box")
    holds = [("box", "imageA:latest"), ("ghost", "imageB:latest")]

    with patch("kento.images.LXC_BASE", lxc), \
         patch("kento.images.VM_BASE", vm), \
         patch("kento.images.subprocess.run", side_effect=_holds_mock(holds)):
        list_images(in_use_only=True)

    out = capsys.readouterr().out
    assert "imageA:latest" in out
    assert "imageB:latest" not in out


def test_list_no_managed_images(tmp_path, capsys):
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()

    with patch("kento.images.LXC_BASE", lxc), \
         patch("kento.images.VM_BASE", vm), \
         patch("kento.images.subprocess.run", side_effect=_holds_mock([])):
        list_images()

    out = capsys.readouterr().out
    assert "No kento-managed images." in out


def test_list_image_referenced_no_hold(tmp_path, capsys):
    """A guest-referenced image with no hold still shows, HOLD=no, in-use."""
    lxc = tmp_path / "lxc"
    vm = tmp_path / "vm"
    lxc.mkdir()
    vm.mkdir()
    _mk_guest(lxc, "box", "imageA:latest", name="box")

    with patch("kento.images.LXC_BASE", lxc), \
         patch("kento.images.VM_BASE", vm), \
         patch("kento.images.subprocess.run", side_effect=_holds_mock([])):
        list_images()

    out = capsys.readouterr().out
    line = next(l for l in out.strip().split("\n") if "imageA" in l)
    assert "no" in line and "in-use" in line


# --- prune dry-run -------------------------------------------------------


def test_prune_dry_run_makes_no_destructive_calls(tmp_path, capsys):
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
        prune()

    out = capsys.readouterr().out
    assert "kento-hold.ghost" in out
    assert "imageB:latest" in out
    assert "kento prune --yes" in out
    # No removal helper, no rm / image rm subprocess calls.
    mock_rm.assert_not_called()
    for call in mock_run.call_args_list:
        argv = call.args[0] if call.args else call.kwargs.get("args", [])
        assert "rm" not in argv, f"prune dry-run must not call rm: {argv}"


# --- prune --yes ---------------------------------------------------------


def test_prune_yes_removes_orphaned_only(tmp_path, capsys):
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
        prune(yes=True)

    out = capsys.readouterr().out
    # Only the orphaned hold removed.
    mock_rm.assert_called_once_with("ghost")
    # imageB freed (no guest, no surviving hold) -> image rm attempted.
    img_rm_calls = [
        c for c in mock_run.call_args_list
        if (c.args and "image" in c.args[0] and "rm" in c.args[0])
    ]
    assert len(img_rm_calls) == 1
    assert "imageB:latest" in img_rm_calls[0].args[0]
    assert "Removed 1 orphaned hold(s), 1 image(s)." in out


def test_prune_yes_tolerates_image_rm_failure(tmp_path, capsys):
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
        prune(yes=True)

    out = capsys.readouterr().out
    # Hold removed (1), image refused (0).
    assert "Removed 1 orphaned hold(s), 0 image(s)." in out


def test_prune_image_pinned_by_surviving_hold_not_removed(tmp_path, capsys):
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
        prune(yes=True)

    out = capsys.readouterr().out
    mock_rm.assert_called_once_with("ghost")
    img_rm_calls = [
        c for c in mock_run.call_args_list
        if (c.args and "image" in c.args[0] and "rm" in c.args[0])
    ]
    assert img_rm_calls == []
    assert "Removed 1 orphaned hold(s), 0 image(s)." in out


def test_prune_nothing_to_do(tmp_path, capsys):
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
        prune(yes=True)

    out = capsys.readouterr().out
    assert "Nothing to prune." in out
    mock_rm.assert_not_called()
