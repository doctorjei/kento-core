"""Test-wide fixtures.

Redirects the kento cross-process lock to a writable tmpdir so unit tests
that call `create()` don't need root / /run write access. The lock itself
is still a real flock — it just lives in a temp path per pytest session.
"""

from pathlib import Path

import pytest

from kento import create, locking


@pytest.fixture(scope="session")
def _kento_lock_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("kento-lock")


@pytest.fixture(autouse=True)
def _kento_lock_in_tmp(_kento_lock_dir, monkeypatch):
    """Point kento.locking at a session-tmp lockfile for every test.

    Without this, create() would try /run/kento.lock (root-only) and
    /var/lib/kento/.lock (also root-only) and sys.exit(1) when both fail.
    Individual tests in test_locking.py that override these paths still
    work — monkeypatch restores the tmp pointer after each test.
    """
    monkeypatch.setattr(
        locking, "_PRIMARY_LOCK", Path(_kento_lock_dir) / "kento.lock"
    )
    monkeypatch.setattr(
        locking,
        "_FALLBACK_LOCK",
        Path(_kento_lock_dir) / "kento.lock.fallback",
    )


@pytest.fixture(autouse=True)
def _neutralize_apparmor_detection(monkeypatch):
    """Make the apparmor `generated` pre-flight deterministic for every test.

    create.generate_config() fail-closes when the host kernel has AppArmor
    active AND apparmor_parser is absent. The unit suite must not depend on
    the test host's real LSM state, so by default we report apparmor as
    inactive (the pre-flight is then a no-op). Tests exercising the guard
    re-patch these helpers explicitly; the later monkeypatch.setattr wins.
    """
    monkeypatch.setattr(create, "_apparmor_active", lambda: False)
    monkeypatch.setattr(create, "_apparmor_parser_present", lambda: True)
