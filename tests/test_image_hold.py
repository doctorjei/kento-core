"""Tests for image hold (prune protection) via podman containers."""

from unittest.mock import patch
from kento.layers import create_image_hold, remove_image_hold


class TestCreateImageHold:
    def test_creates_hold_from_id_with_image_id_label(self):
        """The hold pins the resolved content-ID (not the tag) and records
        it in an io.kento.hold-image-id label."""
        with patch("kento.layers.resolve_image_id",
                   return_value="sha256:abc123"), \
             patch("kento.layers.subprocess.run") as mock_run:
            create_image_hold("docker.io/library/debian:12", "mybox")
            mock_run.assert_called_once_with(
                ["podman", "create", "--name", "kento-hold.mybox",
                 "--label", "io.kento.hold-for=mybox",
                 "--label", "io.kento.hold-image-id=sha256:abc123",
                 "sha256:abc123", "/bin/true"],
                capture_output=True,
            )

    def test_falls_back_to_tag_when_id_unresolvable(self):
        """Older podman / image gone -> pin by tag, omit the image-id label
        (matches the prior behavior)."""
        with patch("kento.layers.resolve_image_id", return_value=""), \
             patch("kento.layers.subprocess.run") as mock_run:
            create_image_hold("docker.io/library/debian:12", "mybox")
            mock_run.assert_called_once_with(
                ["podman", "create", "--name", "kento-hold.mybox",
                 "--label", "io.kento.hold-for=mybox",
                 "docker.io/library/debian:12", "/bin/true"],
                capture_output=True,
            )

    def test_hold_name_format(self):
        with patch("kento.layers.resolve_image_id", return_value="sha256:i"), \
             patch("kento.layers.subprocess.run") as mock_run:
            create_image_hold("img", "test-container")
            args = mock_run.call_args[0][0]
            assert args[3] == "kento-hold.test-container"

    def test_label_format(self):
        with patch("kento.layers.resolve_image_id", return_value="sha256:i"), \
             patch("kento.layers.subprocess.run") as mock_run:
            create_image_hold("img", "test-container")
            args = mock_run.call_args[0][0]
            assert args[5] == "io.kento.hold-for=test-container"

    def test_silently_continues_on_failure(self):
        """If podman create fails (e.g., hold exists), no exception raised."""
        with patch("kento.layers.resolve_image_id", return_value="sha256:i"), \
             patch("kento.layers.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            create_image_hold("img", "mybox")


class TestRemoveImageHold:
    def test_removes_hold_container(self):
        with patch("kento.layers.subprocess.run") as mock_run:
            remove_image_hold("mybox")
            mock_run.assert_called_once_with(
                ["podman", "rm", "kento-hold.mybox"],
                capture_output=True,
            )

    def test_silently_continues_if_missing(self):
        """If hold container doesn't exist, no exception raised."""
        with patch("kento.layers.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            remove_image_hold("mybox")
