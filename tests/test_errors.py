import pytest
from kento.errors import (
    KentoError, ValidationError, InstanceNotFoundError, InstanceExistsError,
    ImageNotFoundError, ModeError, StateError, SubprocessError,
)

ALL_SUBCLASSES = [
    ValidationError, InstanceNotFoundError, InstanceExistsError,
    ImageNotFoundError, ModeError, StateError, SubprocessError,
]

@pytest.mark.parametrize("cls", ALL_SUBCLASSES)
def test_every_error_is_a_kento_error(cls):
    assert issubclass(cls, KentoError)
    assert issubclass(cls, Exception)

def test_kento_error_carries_message():
    e = ValidationError("bad name")
    assert str(e) == "bad name"

def test_subprocess_error_carries_cmd_and_returncode():
    e = SubprocessError("pct stop failed", cmd=["pct", "stop", "101"], returncode=2)
    assert e.cmd == ["pct", "stop", "101"]
    assert e.returncode == 2
    assert "pct stop failed" in str(e)

def test_subprocess_error_defaults():
    e = SubprocessError("boom")
    assert e.cmd is None
    assert e.returncode is None
