"""Spec suite for the platform & lifecycle-status value types (Block 03).

Covers enum membership + exact wire string values (``PlatformMode``, ``Status``),
``PlatformProfile`` construction, the §6.2 cross-field coherence invariant
(tested for what it accepts AND what it rejects — the "both ways it can go"),
frozen immutability, and the flat-public re-export from ``kento``.

Spec: ~/workspace/kento-core-api-design.md §2, §6, §7.
"""

import dataclasses

import pytest

from kento import PlatformMode, PlatformProfile, Status, ValidationError


# --------------------------------------------------------------------------- #
# Flat public re-export (Block 03 surface).
# --------------------------------------------------------------------------- #


def test_public_names_reexported_flat():
    import kento

    for name in ("PlatformMode", "PlatformProfile", "Status"):
        assert name in kento.__all__, f"{name} missing from kento.__all__"
        assert getattr(kento, name) is not None


def test_no_module_stutter():
    # Canonical path is kento.X, not kento._platform.X (importable internally,
    # but the public name is flat).
    import kento
    import kento._platform as impl

    assert kento.PlatformMode is impl.PlatformMode
    assert kento.PlatformProfile is impl.PlatformProfile
    assert kento.Status is impl.Status


# --------------------------------------------------------------------------- #
# PlatformMode — membership + exact wire values (§6.1).
# --------------------------------------------------------------------------- #


def test_platformmode_members_and_values():
    assert {m.name for m in PlatformMode} == {"STANDARD", "PVE"}
    assert PlatformMode.STANDARD.value == "standard"
    assert PlatformMode.PVE.value == "pve"


def test_platformmode_is_str_enum():
    # str-backed: members compare/serialize as their wire string.
    assert isinstance(PlatformMode.PVE, str)
    assert PlatformMode.PVE == "pve"
    assert PlatformMode("standard") is PlatformMode.STANDARD


# --------------------------------------------------------------------------- #
# Status — membership + exact wire values (§7.1).
# --------------------------------------------------------------------------- #


def test_status_members_and_values():
    assert {s.name for s in Status} == {
        "RUNNING", "STOPPED", "SUSPENDED", "ORPHAN", "UNKNOWN",
    }
    assert Status.RUNNING.value == "running"
    assert Status.STOPPED.value == "stopped"
    assert Status.SUSPENDED.value == "suspended"
    assert Status.ORPHAN.value == "orphan"
    assert Status.UNKNOWN.value == "unknown"


def test_status_is_str_enum():
    assert isinstance(Status.RUNNING, str)
    assert Status.RUNNING == "running"
    assert Status("unknown") is Status.UNKNOWN


# --------------------------------------------------------------------------- #
# PlatformProfile — construction of COHERENT profiles (§6.1/§6.2).
# --------------------------------------------------------------------------- #


def test_standard_profile_construct():
    p = PlatformProfile(mode=PlatformMode.STANDARD, mid=None)
    assert p.mode is PlatformMode.STANDARD
    assert p.mid is None
    assert p.extra_args == ()


def test_standard_profile_extra_args_defaults_empty():
    # extra_args is optional and defaults to () — a bare STANDARD profile is
    # valid without passing it.
    p = PlatformProfile(PlatformMode.STANDARD, None)
    assert p.extra_args == ()


def test_pve_profile_construct():
    p = PlatformProfile(mode=PlatformMode.PVE, mid=100)
    assert p.mode is PlatformMode.PVE
    assert p.mid == 100
    assert p.extra_args == ()


def test_pve_profile_with_extra_args():
    p = PlatformProfile(
        mode=PlatformMode.PVE, mid=150, extra_args=("--foo", "bar"),
    )
    assert p.mid == 150
    assert p.extra_args == ("--foo", "bar")


def test_pve_profile_at_floor():
    # 100 is the inclusive floor (PVE reserves 1-99).
    assert PlatformProfile(PlatformMode.PVE, 100).mid == 100


def test_extra_args_frozen_to_tuple():
    # A list argument is accepted and frozen to a tuple so the value stays
    # genuinely immutable (the Diagnosis/ReclaimReport idiom).
    p = PlatformProfile(PlatformMode.PVE, 100, extra_args=["--a", "--b"])
    assert p.extra_args == ("--a", "--b")
    assert isinstance(p.extra_args, tuple)


# --------------------------------------------------------------------------- #
# PlatformProfile — the §6.2 coherence invariant REJECTS incoherent profiles.
# (gate C: an incoherent profile is unrepresentable, not a latent foot-gun.)
# --------------------------------------------------------------------------- #


def test_standard_with_mid_rejected():
    with pytest.raises(ValidationError):
        PlatformProfile(mode=PlatformMode.STANDARD, mid=100)


def test_standard_with_extra_args_rejected():
    with pytest.raises(ValidationError):
        PlatformProfile(
            mode=PlatformMode.STANDARD, mid=None, extra_args=("--pve-arg",),
        )


def test_pve_with_none_mid_rejected():
    with pytest.raises(ValidationError):
        PlatformProfile(mode=PlatformMode.PVE, mid=None)


def test_pve_with_reserved_mid_rejected():
    # vmids 1-99 are reserved by Proxmox.
    with pytest.raises(ValidationError):
        PlatformProfile(mode=PlatformMode.PVE, mid=99)


def test_pve_with_zero_mid_rejected():
    with pytest.raises(ValidationError):
        PlatformProfile(mode=PlatformMode.PVE, mid=0)


def test_pve_with_negative_mid_rejected():
    with pytest.raises(ValidationError):
        PlatformProfile(mode=PlatformMode.PVE, mid=-1)


def test_pve_with_bool_mid_rejected():
    # bool is an int subclass in Python; True must NOT masquerade as vmid 1.
    # Pin the *bool guard specifically*: assert on its distinct message ("int
    # mid"). Without the guard, mid=True (==1) would instead fall to the floor
    # check and raise the ">= 100" message — so matching here goes red if the
    # bool guard is deleted, rather than being silently floor-covered.
    with pytest.raises(ValidationError, match="int mid"):
        PlatformProfile(mode=PlatformMode.PVE, mid=True)


def test_non_member_mode_rejected():
    # A raw string can't smuggle past the discriminator check.
    with pytest.raises(ValidationError):
        PlatformProfile(mode="pve", mid=100)


# --------------------------------------------------------------------------- #
# PlatformProfile — frozen / immutable (§2 principle 2, inert value).
# --------------------------------------------------------------------------- #


def test_profile_is_frozen():
    p = PlatformProfile(PlatformMode.PVE, 100)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.mid = 200  # type: ignore[misc]


def test_profile_equality_and_hashable():
    a = PlatformProfile(PlatformMode.PVE, 100, ("--x",))
    b = PlatformProfile(PlatformMode.PVE, 100, ("--x",))
    assert a == b
    assert hash(a) == hash(b)
    # Frozen + hashable -> usable as a dict key / set member.
    assert len({a, b}) == 1
