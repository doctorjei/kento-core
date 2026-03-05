"""Tests for kento __init__ shared utilities."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from kento import require_root, upper_base, LXC_BASE


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
