"""Tests for kento __init__ shared utilities."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from kento import (
    require_root, upper_base, LXC_BASE,
    sanitize_image_name, next_instance_name, resolve_container,
)


def test_require_root_fails_non_root():
    with patch("kento.os.getuid", return_value=1000):
        with pytest.raises(SystemExit):
            require_root()


def test_require_root_passes_as_root():
    with patch("kento.os.getuid", return_value=0):
        require_root()  # should not raise


def test_upper_base_root_direct():
    with patch.dict(os.environ, {}, clear=True):
        result = upper_base("test")
    assert result == LXC_BASE / "test"


def test_upper_base_sudo_user():
    with patch.dict(os.environ, {"SUDO_USER": "alice"}), \
         patch("kento.pwd.getpwnam") as mock_pwd:
        mock_pwd.return_value.pw_dir = "/home/alice"
        result = upper_base("test")
    assert result == Path("/home/alice/.local/share/kento/test")


# --- sanitize_image_name ---


def test_sanitize_simple():
    assert sanitize_image_name("debian:12") == "debian_12"


def test_sanitize_full_reference():
    assert sanitize_image_name("docker.io/library/debian:12") == "docker.io-library-debian_12"


def test_sanitize_with_dashes():
    assert sanitize_image_name("my-registry/my-image:v1") == "my--registry-my--image_v1"


def test_sanitize_with_underscores():
    assert sanitize_image_name("my_image:latest") == "my__image_latest"


def test_sanitize_complex():
    """Test all four replacement rules together."""
    assert sanitize_image_name("my-reg/lib_img:v1.0-beta") == "my--reg-lib__img_v1.0--beta"


def test_sanitize_is_bijective():
    """Verify the transformation is reversible."""
    original = "docker.io/jrei/systemd-debian:12"
    sanitized = sanitize_image_name(original)
    # Reverse: '_' → ':', '__' → '_', '-' → '/', '--' → '-'
    r = sanitized.replace("--", "\x00")
    r = r.replace("-", "/")
    r = r.replace("\x00", "-")
    r = r.replace("__", "\x00")
    r = r.replace("_", ":")
    r = r.replace("\x00", "_")
    assert r == original


# --- next_instance_name ---


def test_next_instance_name_empty(tmp_path):
    assert next_instance_name("debian_12", tmp_path) == "debian_12-0"


def test_next_instance_name_increments(tmp_path):
    (tmp_path / "debian_12-0").mkdir()
    assert next_instance_name("debian_12", tmp_path) == "debian_12-1"


def test_next_instance_name_fills_gaps(tmp_path):
    (tmp_path / "debian_12-0").mkdir()
    (tmp_path / "debian_12-2").mkdir()
    # -1 is free
    assert next_instance_name("debian_12", tmp_path) == "debian_12-1"


def test_next_instance_name_checks_kento_name(tmp_path):
    """Names from kento-name files also block candidates."""
    vmid_dir = tmp_path / "100"
    vmid_dir.mkdir()
    (vmid_dir / "kento-name").write_text("debian_12-0\n")
    assert next_instance_name("debian_12", tmp_path) == "debian_12-1"


def test_next_instance_name_nonexistent_dir(tmp_path):
    assert next_instance_name("foo", tmp_path / "nope") == "foo-0"


# --- resolve_container ---


def test_resolve_container_direct(tmp_path):
    d = tmp_path / "mybox"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    result = resolve_container("mybox", tmp_path)
    assert result == d


def test_resolve_container_by_kento_name(tmp_path):
    d = tmp_path / "100"
    d.mkdir()
    (d / "kento-image").write_text("debian:12\n")
    (d / "kento-name").write_text("webbox\n")
    result = resolve_container("webbox", tmp_path)
    assert result == d


def test_resolve_container_not_found(tmp_path):
    with pytest.raises(SystemExit):
        resolve_container("nonexistent", tmp_path)


def test_resolve_container_prefers_direct_match(tmp_path):
    """Direct directory match takes priority over kento-name scan."""
    # Direct match
    d1 = tmp_path / "mybox"
    d1.mkdir()
    (d1 / "kento-image").write_text("debian:12\n")
    # Another container whose kento-name is also "mybox"
    d2 = tmp_path / "100"
    d2.mkdir()
    (d2 / "kento-image").write_text("debian:12\n")
    (d2 / "kento-name").write_text("mybox\n")
    result = resolve_container("mybox", tmp_path)
    assert result == d1
