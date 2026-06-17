"""Tests for layer resolution."""

from unittest.mock import patch
import subprocess

import pytest

from kento.errors import ImageNotFoundError, StateError
from kento.layers import (resolve_layers, ensure_image_hold, _podman_cmd,
                          to_overlay_lowerdir, preflight_overlay_layers,
                          resolve_image_id, repin_image_hold)


def _mock_run(args, **kwargs):
    """Mock podman subprocess calls."""
    result = subprocess.CompletedProcess(args, 0)
    if "exists" in args:
        return result
    if "UpperDir" in args[-1]:
        result.stdout = "/upper/diff\n"
    elif "LowerDir" in args[-1]:
        result.stdout = "/lower1/diff:/lower2/diff\n"
    return result


def _mock_run_single_layer(args, **kwargs):
    """Mock podman for a single-layer image."""
    result = subprocess.CompletedProcess(args, 0)
    if "exists" in args:
        return result
    if "UpperDir" in args[-1]:
        result.stdout = "/upper/diff\n"
    elif "LowerDir" in args[-1]:
        result.stdout = "<no value>\n"
    return result


def _mock_run_no_image(args, **kwargs):
    """Mock podman image exists failure."""
    if "exists" in args:
        return subprocess.CompletedProcess(args, 1)
    return subprocess.CompletedProcess(args, 0, stdout="")


@patch("kento.layers.subprocess.run")
def test_resolve_layers_multi(mock_run):
    mock_run.side_effect = _mock_run
    result = resolve_layers("myimage:latest")
    assert result == "/upper/diff:/lower1/diff:/lower2/diff"


@patch("kento.layers.subprocess.run")
def test_resolve_layers_single(mock_run):
    mock_run.side_effect = _mock_run_single_layer
    result = resolve_layers("myimage:latest")
    assert result == "/upper/diff"
    assert "<no value>" not in result


@patch("kento.layers.subprocess.run")
def test_resolve_layers_missing_image(mock_run):
    mock_run.side_effect = _mock_run_no_image
    with pytest.raises(ImageNotFoundError):
        resolve_layers("nonexistent:latest")


@patch("kento.layers.subprocess.run")
def test_resolve_layers_missing_image_hints_pull(mock_run):
    """F3: the missing-image error must point the user at `kento pull`."""
    mock_run.side_effect = _mock_run_no_image
    with pytest.raises(ImageNotFoundError, match="not found in local store"):
        resolve_layers("nonexistent:latest")
    with pytest.raises(ImageNotFoundError, match="kento pull nonexistent:latest"):
        resolve_layers("nonexistent:latest")


class TestEnsureImageHold:
    """ensure_image_hold backfills the hold container only when missing."""

    def _exists_check_cmd(self, calls):
        for c in calls:
            cmd = list(c.args[0])
            if "container" in cmd and "exists" in cmd:
                return cmd
        return None

    @patch("kento.layers.resolve_image_id", return_value="")
    @patch("kento.layers.subprocess.run")
    def test_creates_hold_when_missing(self, mock_run, mock_id):
        def side_effect(args, **kwargs):
            cmd = list(args)
            if "exists" in cmd:
                return subprocess.CompletedProcess(cmd, 1)  # missing
            return subprocess.CompletedProcess(cmd, 0)

        mock_run.side_effect = side_effect
        ensure_image_hold("myimage:latest", "mybox")

        # exists-check uses `podman container exists kento-hold.mybox`
        exists_cmd = self._exists_check_cmd(mock_run.call_args_list)
        assert exists_cmd == ["podman", "container", "exists", "kento-hold.mybox"]

        # create_image_hold was invoked (id unresolvable -> pins by tag)
        create_calls = [list(c.args[0]) for c in mock_run.call_args_list
                        if "create" in list(c.args[0])]
        assert len(create_calls) == 1
        cmd = create_calls[0]
        assert "--name" in cmd and "kento-hold.mybox" in cmd
        assert "io.kento.hold-for=mybox" in cmd
        assert "myimage:latest" in cmd

    @patch("kento.layers.subprocess.run")
    def test_no_create_when_hold_exists(self, mock_run):
        def side_effect(args, **kwargs):
            cmd = list(args)
            if "exists" in cmd:
                return subprocess.CompletedProcess(cmd, 0)  # present
            return subprocess.CompletedProcess(cmd, 0)

        mock_run.side_effect = side_effect
        ensure_image_hold("myimage:latest", "mybox")

        create_calls = [c for c in mock_run.call_args_list
                        if "create" in list(c.args[0])]
        assert create_calls == []

    @patch("kento.layers.subprocess.run", side_effect=OSError("podman gone"))
    def test_tolerates_subprocess_failure(self, mock_run):
        # best-effort: never raises even if podman blows up
        ensure_image_hold("myimage:latest", "mybox")


class TestResolveImageId:
    @patch("kento.layers.subprocess.run")
    def test_returns_stripped_id(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout="sha256:deadbeef\n")
        assert resolve_image_id("img:latest") == "sha256:deadbeef"
        cmd = mock_run.call_args[0][0]
        assert cmd == ["podman", "image", "inspect", "img:latest",
                       "--format", "{{.Id}}"]

    @patch("kento.layers.subprocess.run")
    def test_empty_on_nonzero(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess([], 1, stdout="")
        assert resolve_image_id("missing:latest") == ""

    @patch("kento.layers.subprocess.run", side_effect=OSError("boom"))
    def test_empty_on_exception(self, mock_run):
        assert resolve_image_id("img") == ""


class TestRepinImageHold:
    """repin_image_hold removes+recreates only on drift; no-op when aligned."""

    @patch("kento.layers.create_image_hold")
    @patch("kento.layers.remove_image_hold")
    @patch("kento.layers._hold_pinned_id", return_value="sha256:OLD")
    @patch("kento.layers.resolve_image_id", return_value="sha256:NEW")
    @patch("kento.layers.subprocess.run")
    def test_repins_on_drift(self, mock_run, mock_id, mock_pinned,
                             mock_remove, mock_create):
        # hold exists but pins a different id -> remove + recreate, return True.
        mock_run.return_value = subprocess.CompletedProcess([], 0)  # exists
        assert repin_image_hold("img:latest", "mybox") is True
        mock_remove.assert_called_once_with("mybox")
        mock_create.assert_called_once_with("img:latest", "mybox")

    @patch("kento.layers.create_image_hold")
    @patch("kento.layers.remove_image_hold")
    @patch("kento.layers._hold_pinned_id", return_value="sha256:SAME")
    @patch("kento.layers.resolve_image_id", return_value="sha256:SAME")
    @patch("kento.layers.subprocess.run")
    def test_noop_when_aligned(self, mock_run, mock_id, mock_pinned,
                               mock_remove, mock_create):
        mock_run.return_value = subprocess.CompletedProcess([], 0)  # exists
        assert repin_image_hold("img:latest", "mybox") is False
        mock_remove.assert_not_called()
        mock_create.assert_not_called()

    @patch("kento.layers.create_image_hold")
    @patch("kento.layers.remove_image_hold")
    @patch("kento.layers._hold_pinned_id", return_value="")
    @patch("kento.layers.resolve_image_id", return_value="sha256:NEW")
    @patch("kento.layers.subprocess.run")
    def test_repins_when_hold_missing(self, mock_run, mock_id, mock_pinned,
                                      mock_remove, mock_create):
        # container exists check returns nonzero -> hold absent -> recreate.
        mock_run.return_value = subprocess.CompletedProcess([], 1)
        assert repin_image_hold("img:latest", "mybox") is True
        mock_remove.assert_called_once_with("mybox")
        mock_create.assert_called_once_with("img:latest", "mybox")

    @patch("kento.layers.ensure_image_hold")
    @patch("kento.layers.create_image_hold")
    @patch("kento.layers.remove_image_hold")
    @patch("kento.layers.resolve_image_id", return_value="")
    def test_falls_back_to_ensure_when_id_unresolvable(
            self, mock_id, mock_remove, mock_create, mock_ensure):
        # Target id can't be resolved -> backfill (ensure), no churn, False.
        assert repin_image_hold("img:latest", "mybox") is False
        mock_ensure.assert_called_once_with("img:latest", "mybox")
        mock_remove.assert_not_called()
        mock_create.assert_not_called()


def _make_store(tmp_path, ids_and_shorts):
    """Build a fake podman overlay store under tmp_path and return
    (overlay_root, absolute_colon_joined_layers)."""
    root = tmp_path / "var" / "lib" / "containers" / "storage" / "overlay"
    paths = []
    for lid, short in ids_and_shorts:
        diff = root / lid / "diff"
        diff.mkdir(parents=True)
        if short is not None:
            (root / lid / "link").write_text(short + "\n")
        paths.append(str(diff))
    return str(root), ":".join(paths)


class TestToOverlayLowerdir:
    """layer diff-paths -> chdir-relative l/<short> form (Docker/podman parity)."""

    def test_converts_to_short_links(self, tmp_path):
        root, layers = _make_store(tmp_path, [
            ("aaaa", "SHORT1XXXXXXXXXXXXXXXXXXXXX"),
            ("bbbb", "SHORT2YYYYYYYYYYYYYYYYYYYYY"),
            ("cccc", "SHORT3ZZZZZZZZZZZZZZZZZZZZZ"),
        ])
        ov_root, rel = to_overlay_lowerdir(layers)
        assert ov_root == root
        assert rel == ("l/SHORT1XXXXXXXXXXXXXXXXXXXXX:"
                       "l/SHORT2YYYYYYYYYYYYYYYYYYYYY:"
                       "l/SHORT3ZZZZZZZZZZZZZZZZZZZZZ")

    def test_preserves_order(self, tmp_path):
        root, layers = _make_store(tmp_path, [
            ("top", "TOPSHORTxxxxxxxxxxxxxxxxxxx"),
            ("bot", "BOTSHORTyyyyyyyyyyyyyyyyyyy"),
        ])
        _, rel = to_overlay_lowerdir(layers)
        assert rel.split(":") == ["l/TOPSHORTxxxxxxxxxxxxxxxxxxx",
                                  "l/BOTSHORTyyyyyyyyyyyyyyyyyyy"]

    def test_relative_links_resolve_under_root(self, tmp_path):
        """The l/<short> relative entries must resolve to the diff dirs when
        cd'd into the overlay root, exactly as podman lays out the store."""
        root, layers = _make_store(tmp_path, [
            ("aaaa", "SHORTAAAAAAAAAAAAAAAAAAAAAA"),
            ("bbbb", "SHORTBBBBBBBBBBBBBBBBBBBBBB"),
        ])
        from pathlib import Path
        ldir = Path(root) / "l"
        ldir.mkdir()
        (ldir / "SHORTAAAAAAAAAAAAAAAAAAAAAA").symlink_to(Path("..") / "aaaa" / "diff")
        (ldir / "SHORTBBBBBBBBBBBBBBBBBBBBBB").symlink_to(Path("..") / "bbbb" / "diff")
        ov_root, rel = to_overlay_lowerdir(layers)
        import os
        cwd = os.getcwd()
        try:
            os.chdir(ov_root)
            for part in rel.split(":"):
                assert Path(part).is_dir(), part
        finally:
            os.chdir(cwd)

    def test_missing_link_file_falls_back_to_relative_diff(self, tmp_path):
        """A layer without a link file falls back to <id>/diff (still relative
        to the overlay root, so still short-ish)."""
        root, layers = _make_store(tmp_path, [
            ("aaaa", "SHORTAAAAAAAAAAAAAAAAAAAAAA"),
            ("bbbb", None),  # no link file
            ("cccc", "SHORTCCCCCCCCCCCCCCCCCCCCCC"),
        ])
        ov_root, rel = to_overlay_lowerdir(layers)
        assert ov_root == root
        assert rel.split(":") == [
            "l/SHORTAAAAAAAAAAAAAAAAAAAAAA",
            "bbbb/diff",
            "l/SHORTCCCCCCCCCCCCCCCCCCCCCC",
        ]

    def test_empty_link_file_falls_back(self, tmp_path):
        root, layers = _make_store(tmp_path, [("aaaa", "")])
        ov_root, rel = to_overlay_lowerdir(layers)
        assert ov_root == root
        assert rel == "aaaa/diff"

    def test_non_store_layout_falls_back_to_absolute(self):
        """Layers not in <root>/<id>/diff shape return the original absolute
        string + empty root (current behavior preserved)."""
        root, rel = to_overlay_lowerdir("/weird/path:/another")
        assert root == ""
        assert rel == "/weird/path:/another"

    def test_divergent_roots_fall_back_to_absolute(self, tmp_path):
        """Layers sharing no common overlay root fall back to absolute."""
        a = tmp_path / "storeA" / "aaaa" / "diff"
        b = tmp_path / "storeB" / "bbbb" / "diff"
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        layers = f"{a}:{b}"
        root, rel = to_overlay_lowerdir(layers)
        assert root == ""
        assert rel == layers

    def test_empty_layers(self):
        root, rel = to_overlay_lowerdir("")
        assert root == ""
        assert rel == ""


class TestPreflightOverlayLayers:
    """Create-time fail-closed cap + byte backstop."""

    def test_passes_at_cap(self):
        from kento.defaults import MAX_OVERLAY_LAYERS
        layers = ":".join(
            f"/var/lib/containers/storage/overlay/id{i}/diff"
            for i in range(MAX_OVERLAY_LAYERS))
        preflight_overlay_layers(layers)  # should not raise

    def test_raises_above_cap(self):
        from kento.defaults import MAX_OVERLAY_LAYERS
        n = MAX_OVERLAY_LAYERS + 1
        layers = ":".join(
            f"/var/lib/containers/storage/overlay/id{i}/diff"
            for i in range(n))
        with pytest.raises(StateError, match=str(n)):
            preflight_overlay_layers(layers)
        with pytest.raises(StateError, match=str(MAX_OVERLAY_LAYERS)):
            preflight_overlay_layers(layers)

    def test_byte_backstop_trips_when_forced(self, tmp_path):
        """A pathologically long absolute-fallback layer string must trip the
        byte backstop even under the cap."""
        # Non-store layout so it falls back to absolute (long) paths.
        layers = "/notastore/" + ("a" * 4050) + ":/notastore/b"
        with pytest.raises(StateError, match="bytes"):
            preflight_overlay_layers(layers, tmp_path)

    def test_byte_backstop_ok_for_normal_short_form(self, tmp_path):
        root, layers = _make_store(tmp_path, [
            ("aaaa", "SHORTAAAAAAAAAAAAAAAAAAAAAA"),
            ("bbbb", "SHORTBBBBBBBBBBBBBBBBBBBBBB"),
        ])
        preflight_overlay_layers(layers, tmp_path)  # should not raise


class TestPodmanCmd:
    def test_returns_podman(self):
        assert _podman_cmd() == ["podman"]

    def test_returns_podman_with_sudo_user(self):
        with patch.dict("os.environ", {"SUDO_USER": "alice"}):
            assert _podman_cmd() == ["podman"]
