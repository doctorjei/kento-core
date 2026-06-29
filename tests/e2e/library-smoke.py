#!/usr/bin/env python3
"""Library-consumer smoke test for the typed kento-core public API.

Runs on a REAL host (podman store + real images + root for overlay mounts) and
exercises the typed surface the way a non-kento-cli consumer would (`import
kento`), covering the I/O-wrapping ops that unit tests (mocked subprocess)
cannot: OciImage.list/get/resolve_id, a pull+remove round-trip, and the
real-overlay runtime lifecycle prepare/mount/unmount/release.

Self-contained + cleans up what it creates. Exit 0 = all pass; non-zero on any
failure. Usage:  <tool-venv-python> library-smoke.py [rootfs_image] [pull_image]
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOTFS_IMAGE = sys.argv[1] if len(sys.argv) > 1 else "ghcr.io/doctorjei/droste-hair:latest"
PULL_IMAGE = sys.argv[2] if len(sys.argv) > 2 else "docker.io/library/busybox:latest"

_passed = 0
_failed = 0


def _raises(fn, exc) -> bool:
    try:
        fn()
        return False
    except exc:
        return True


def check(name: str, fn) -> None:
    global _passed, _failed
    try:
        detail = fn()
        _passed += 1
        print(f"  ok   {name}" + (f"  — {detail}" if detail else ""))
    except Exception as exc:  # noqa: BLE001 — smoke test: report any failure
        _failed += 1
        print(f"  FAIL {name}  — {type(exc).__name__}: {exc}")


def section(title: str) -> None:
    print(f"\n# {title}")


# --------------------------------------------------------------------------- #
section("A. import + flat public surface")
import kento  # noqa: E402

check("import kento + __version__", lambda: kento.__version__)
check("flat surface present", lambda: (
    f"{len(kento.__all__)} names"
    if all(hasattr(kento, n) for n in (
        "OciReference", "NetworkConnection", "PlatformProfile", "Status",
        "StorageMode", "Image", "LayeredImage", "OciImage", "Layer", "Diagnosis",
        "ReclaimReport"))
    else (_ for _ in ()).throw(AssertionError("missing a public name"))))

# --------------------------------------------------------------------------- #
section("B. pure value types (as a consumer would call them)")
check("OciReference.parse + render round-trip", lambda: (
    kento.OciReference.parse("ghcr.io/doctorjei/droste-hair:latest").render()))
check("OciReference.normalize (docker-conv)", lambda: (
    kento.OciReference.parse("busybox").normalize().render()))
check("Digest.parse", lambda: kento.Digest.parse(
    "sha256:" + "a" * 64).render()[:14] + "…")
check("parse_forward_spec (ssh/docker grammar)", lambda: str(
    kento.parse_forward_spec("8080:80")))
check("parse_cidr", lambda: str(kento.parse_cidr("10.0.3.5/24")))
check("PlatformProfile coherence (PVE)", lambda: (
    kento.PlatformProfile(mode=kento.PlatformMode.PVE, mid=100).mode.value))
check("PlatformProfile coherence rejects bad", lambda: (
    "rejected" if _raises(lambda: kento.PlatformProfile(
        mode=kento.PlatformMode.STANDARD, mid=100), kento.ValidationError)
    else (_ for _ in ()).throw(AssertionError("did not reject"))))
check("Status / StorageMode enums", lambda: (
    f"{kento.Status.RUNNING.value}/{kento.StorageMode.EPHEMERAL_IMAGE.value}"))

# --------------------------------------------------------------------------- #
section("C. Image read ops against the REAL podman store")
_local = subprocess.run(
    ["podman", "images", "--format", "{{.Repository}}:{{.Tag}}"],
    capture_output=True, text=True).stdout

def _list_nonempty():
    imgs = kento.OciImage.list()
    assert isinstance(imgs, list)
    # bifrost has tagged images present; an empty list here means every entry
    # silently failed to resolve (the totality guard masking a resolve bug).
    assert imgs, "list() returned 0 — every image failed to resolve (masked bug)"
    assert all(isinstance(i, kento.OciImage) for i in imgs)
    assert all(isinstance(i.id, kento.Digest) for i in imgs)
    return f"{len(imgs)} images, all with Digest ids"


check("OciImage.list() resolves real images", _list_nonempty)


def _get_rootfs():
    img = kento.OciImage.get(ROOTFS_IMAGE)
    assert isinstance(img, kento.OciImage)
    assert isinstance(img.id, kento.Digest), "id is not a Digest"
    assert img.layers, "no layers resolved"
    assert isinstance(img.overlay_root, Path), "overlay_root not a Path"
    return f"{len(img.layers)} layers, id={img.id.render()[:19]}…"


check(f"OciImage.get({ROOTFS_IMAGE!r})", _get_rootfs)
check("OciImage.resolve_id -> Digest", lambda: (
    kento.OciImage.resolve_id(ROOTFS_IMAGE).render()[:19] + "…"))
check("get(absent) raises ImageNotFoundError", lambda: (
    "raised" if _raises(
        lambda: kento.OciImage.get("localhost/does-not-exist:nope"),
        kento.ImageNotFoundError)
    else (_ for _ in ()).throw(AssertionError("did not raise"))))

# --------------------------------------------------------------------------- #
section("D. pull + remove round-trip (self-contained)")


def _pull_remove():
    img = kento.OciImage.pull(PULL_IMAGE)
    assert isinstance(img, kento.OciImage)
    rid = img.id.render()
    # remove the handle we just pulled (not held -> should succeed)
    img.remove()
    still = subprocess.run(
        ["podman", "image", "exists", PULL_IMAGE]).returncode == 0
    return f"pulled {rid[:19]}…, removed (exists-after={still})"


check(f"pull({PULL_IMAGE!r}) + remove() round-trip", _pull_remove)

# --------------------------------------------------------------------------- #
section("E. runtime lifecycle on a REAL overlay (root): prepare/mount/unmount/release")


def _lifecycle():
    img = kento.OciImage.get(ROOTFS_IMAGE)
    state = Path(tempfile.mkdtemp(prefix="kento-smoke-state-"))
    host = Path(tempfile.mkdtemp(prefix="kento-smoke-host-"))
    mounted = False
    try:
        img.prepare(state)
        assert (state / "upper").is_dir() and (state / "work").is_dir(), \
            "prepare did not create upper/work"
        # The create/start ROUTINE owns the host dir layout (§4.4 — the routine
        # composes the primitives); create.py creates container_dir/rootfs before
        # mounting. We act as that routine here. (Open Phase-3/4 contract Q:
        # should mount() mkdir its own target, symmetric with prepare/upper-work?)
        (host / "rootfs").mkdir(parents=True, exist_ok=True)
        img.mount(host, state)
        mounted = True
        rootfs = host / "rootfs"
        # a real rootfs overlay should expose recognizable top-level dirs
        entries = sorted(p.name for p in rootfs.iterdir())
        assert any(d in entries for d in ("etc", "usr", "bin")), \
            f"overlay rootfs looks empty: {entries[:6]}"
        return f"mounted {len(entries)} top-level entries"
    finally:
        if mounted:
            try:
                img.unmount(host)
            except Exception as exc:  # noqa: BLE001
                print(f"    (cleanup) unmount: {exc}")
        try:
            img.release(state)
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(state, ignore_errors=True)
        shutil.rmtree(host, ignore_errors=True)


check("prepare + mount + unmount + release", _lifecycle)

# --------------------------------------------------------------------------- #
print(f"\n=== library smoke: {_passed} passed, {_failed} failed ===")
sys.exit(1 if _failed else 0)
