"""The ``Image`` family — resolved, mountable/bootable base artifacts.

A ``SourceReference`` (§3.8, a *locator* — unresolved) **resolves to** an
``Image`` (this module — the *resolved* base artifact an instance boots from).
The family is keyed by **representation** (how the base is stored & overlaid),
orthogonal to the locator's scheme:

* ``LayeredImage`` — a union of read-only filesystem layers (overlayfs). The
  1.0, OCI-backed representation kento ships today.
* ``VolumeImage`` — one partition (single fs) over block-overlay. The first
  post-1.0 feature; declared here as a **documented stub** with NO lifecycle.
* ``CompositeImage`` — Images composited at mount points (the image-lifecycle
  EPIC's mount facility). **Plan only**, NOT built up front; a documented stub.

Two layers, two rules (§2 principle 2). The ``Image`` *dataclass fields* are
inert, frozen value data (``source`` / ``id`` / ``kernel`` / ``initramfs``);
they carry no I/O. The **runtime-lifecycle methods** are the named, enumerable
moments where the handle reaches the outside world — ``prepare`` / ``mount`` /
``unmount`` / ``release`` (§4.4). These are ADDITIVE wrappers (Phase 2): they
**delegate** to the existing procedural functions in ``kento.layers`` and
``kento.vm`` and do NOT reimplement their logic or touch their live callers
(``create.py`` / ``vm.py`` / ``hook.py`` / the CLI) — that re-point is Phase 6.

The public surface (``Image``, ``LayeredImage``, ``Layer``, ``DiskFormat``,
``VolumeImage``, ``CompositeImage``) is re-exported flat from ``kento`` — refer
to ``kento.LayeredImage``, not ``kento._images.LayeredImage``.

Spec: ``~/workspace/kento-core-api-design.md`` §2, §4 (the ``Image`` family),
§3.8 (``Digest`` is the content id; ``Image.id`` is a ``Digest`` and is **not**
the locator's OCI-only ``digest`` pin — §4.3).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from kento._references import Digest, OciReference, SourceReference
from kento.errors import ImageNotFoundError

__all__ = [
    "DiskFormat",
    "Layer",
    "Image",
    "LayeredImage",
    "VolumeImage",
    "CompositeImage",
]


# --------------------------------------------------------------------------- #
# DiskFormat — the disk-file container format (§4.1).
#
# ``str, Enum`` so members compare/serialize as their wire value
# (``DiskFormat.QCOW2 == "qcow2"``) — the library's idiom for closed value sets
# (NetworkMode / Status / StorageMode). Used by the (post-1.0) VolumeImage.
# --------------------------------------------------------------------------- #


class DiskFormat(str, Enum):
    """A single-partition disk-file container format (§4.1). Extensible."""

    RAW = "raw"
    QCOW2 = "qcow2"


# --------------------------------------------------------------------------- #
# Layer — one read-only store layer (§4.1).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Layer:
    """One read-only podman-store layer of a ``LayeredImage`` (§4.1).

    A pure value (no I/O). ``id`` is the podman internal layer id — the
    ``<id>`` in ``<overlay_root>/<id>/diff``. ``short_link`` is the ``l/<short>``
    symlink podman keeps for that layer (the short-form lowerdir entry that
    keeps the mount(2) options under the kernel's 4096-byte page limit). An
    empty ``short_link`` means the store had no link file for the layer, so a
    mount falls back to the longer ``<id>/diff`` form (mirrors
    ``layers.to_overlay_lowerdir``'s per-layer fallback — see that function).
    """

    id: str
    short_link: str


# --------------------------------------------------------------------------- #
# Image — the abstract resolved-artifact base (§4.1).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, kw_only=True)
class Image(ABC):
    """A resolved, mountable/bootable base artifact — the family base (§4.1).

    Frozen, inert value fields (§2 principle 2 — a property never performs I/O):

    * ``source`` — the ``SourceReference`` locator this resolved from (narrowed
      to ``OciReference`` on ``LayeredImage``).
    * ``id`` — the content **identity**, a ``Digest`` (§4.3). This is the
      *resolved* identity, **not** the locator's OCI-only ``digest`` pin — do
      not conflate the two (§4.3 last bullet).
    * ``kernel`` / ``initramfs`` — OPTIONAL off-image kernel override for
      Linux/VM direct-kernel-boot, on ANY image (§4.3). ``None`` means use the
      in-image ``/boot`` (Layered) or the gemet default; ignored for LXC and
      the future firmware/non-Linux path.

    The **runtime lifecycle** is the abstract contract every concrete image
    implements (§4.4) — two primitives plus their inverses, the named moments
    an image is made usable and then torn down:

    * ``prepare`` — materialize the base (assemble the overlay / set up a
      backing chain / create the writable upper). **No host mount.**
    * ``mount`` → host dir — host-side mount; **skipped** when the backend takes
      a block device directly (VM + ``VolumeImage``).
    * ``unmount`` / ``release`` — the inverses, run in reverse order on
      teardown.

    They are declared ``abstractmethod`` here so ``Image`` is genuinely
    abstract (cannot be instantiated) and so a concrete subclass that forgets a
    primitive fails loudly at definition rather than silently no-op'ing (gate
    C). ``LayeredImage`` implements all four; ``VolumeImage`` / ``CompositeImage``
    are documented stubs that deliberately leave them unimplemented and so stay
    abstract/uninstantiable until they are built (§4.5).

    These methods perform I/O (principle 2) and are kept **total functions of
    their explicit arguments** plus the image's own resolved value: a frozen
    value cannot carry mutable mount state, so the host directory and the
    writable state directory are passed in by the caller (kento's create/start
    routine, which owns that on-disk layout) rather than stashed on the image.
    """

    source: SourceReference
    id: Digest
    kernel: Path | None = None
    initramfs: Path | None = None

    # ----- runtime-lifecycle contract (§4.4) — abstract on the base -----
    @abstractmethod
    def prepare(self, state_dir: Path) -> None:
        """Materialize the base for use; NO host mount (§4.4).

        ``state_dir`` is the per-instance writable-state base (where the
        overlay upper/work live, or a future block device is staged). Raises a
        typed ``KentoError`` on failure (never returns an error sentinel,
        principle 5).
        """

    @abstractmethod
    def mount(self, host_dir: Path, state_dir: Path) -> None:
        """Host-side mount the prepared base under ``host_dir`` (§4.4).

        Skipped by recipes that hand the backend a block device directly (VM +
        ``VolumeImage``); those subclasses make ``mount`` a no-op or omit the
        cell. ``state_dir`` carries the writable upper/work produced by
        ``prepare``.
        """

    @abstractmethod
    def unmount(self, host_dir: Path) -> None:
        """Inverse of ``mount`` — unmount the base from ``host_dir`` (§4.4)."""

    @abstractmethod
    def release(self, state_dir: Path) -> None:
        """Inverse of ``prepare`` — release materialized state (§4.4).

        Run AFTER ``unmount`` on teardown (reverse order). For a ``LayeredImage``
        the materialized state is the writable upper/work tree under
        ``state_dir``; releasing it is the caller's storage policy (it may
        persist across restarts), so the base contract does not destroy it.
        """


# --------------------------------------------------------------------------- #
# LayeredImage — the 1.0, OCI-backed overlayfs representation (§4.1, §4.4).
#
# The only representation with an implemented runtime lifecycle in 1.0. Every
# lifecycle method is an ADDITIVE wrapper over the existing procedural functions
# (kento.layers / kento.vm) — it delegates, it does not fork their logic.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, kw_only=True)
class LayeredImage(Image):
    """A union of read-only filesystem layers (overlayfs); OCI-only for 1.0.

    ``source`` is narrowed to an ``OciReference``. ``layers`` are the ordered
    lowerdirs (top→bottom; each ``Layer.id`` is the podman ``<id>`` in
    ``<overlay_root>/<id>/diff``, §4.1) and ``overlay_root`` is the shared
    podman-store base — the directory a mount ``chdir``s into so the short
    ``l/<short>`` lowerdir entries resolve. Both are MANDATORY (§4.1 verbatim —
    no defaults): a ``LayeredImage`` is by definition a populated union, and a
    layer-less / rootless one would be a degenerate value whose mount renders
    ``""`` (gate C). Construct via :meth:`resolve`, or supply both fields
    explicitly.

    The absolute lowerdir is the uniform join ``<overlay_root>/<id>/diff`` over
    ``layers``. ``overlay_root`` is a real ``Path`` per §4.1 (never
    ``None``/``""``): for a podman OCI image every GraphDriver layer shares the
    store root by construction, so :meth:`resolve` always derives one; if the
    store layout is genuinely unexpected (no shared root derivable) that is a
    typed ``StateError``, not a fabricated degenerate value. They are cached
    snapshot data (principle 2 — a property never re-queries), and the mount
    renders the lowerdir string at the boundary (principle 3).
    """

    source: OciReference
    layers: tuple[Layer, ...]
    overlay_root: Path

    def __post_init__(self) -> None:
        # Freeze an iterable ``layers`` argument to a tuple (the frozen-dataclass
        # coercion idiom — a frozen dataclass freezes the *binding*, not a
        # mutable list behind it). Mirrors PlatformProfile / ReclaimReport.
        if not isinstance(self.layers, tuple):
            object.__setattr__(self, "layers", tuple(self.layers))

    # ------------------------------------------------------------------- #
    # Resolution — snapshot-load layers + overlay_root + content id (§4.3).
    #
    # The typed entry point. Wraps the procedural resolvers in kento.layers
    # and the typed Digest resolver below; builds a fully-populated, frozen
    # LayeredImage. This is the boundary where a string ref becomes a value.
    # ------------------------------------------------------------------- #
    @classmethod
    def resolve(cls, source: OciReference) -> "LayeredImage":
        """Resolve an ``OciReference`` to a populated ``LayeredImage`` (§4.3).

        Delegates to ``layers.resolve_layers`` (podman query — raises
        ``ImageNotFoundError`` when the image is not in the local store) and
        ``layers.to_overlay_lowerdir`` (the short-link store decomposition),
        and to :meth:`resolve_id` for the typed content ``Digest``. Does NOT
        reimplement any of that logic — it composes the existing functions
        (build-on-what-exists, standards §07.5).
        """
        from kento import layers as _layers

        ref = source.render()
        lowerdir = _layers.resolve_layers(ref)
        overlay_root, _rel = _layers.to_overlay_lowerdir(lowerdir)
        root, layer_tuple = _decompose_layers(lowerdir, overlay_root)
        digest = cls.resolve_id(ref)
        return cls(
            source=source,
            id=digest,
            layers=layer_tuple,
            overlay_root=root,
        )

    @staticmethod
    def resolve_id(ref: str) -> Digest:
        """Resolve an image reference to its content ``Digest``. TOTAL (§4.3).

        Wraps ``layers.resolve_image_id`` — which returns ``""`` on any failure
        (the internal-tool error-as-data shortcut, §2 principle 5). The typed
        surface **raises** ``ImageNotFoundError`` on that empty result instead
        of yielding a sentinel, then parses the ``sha256:...`` string through
        ``Digest.parse`` (which itself raises ``MalformedReference`` on a
        malformed id). No ``""``/``None`` ever escapes. ``resolve_image_id``
        itself is unchanged (its ``""``-tolerant callers stay until Phase 6).
        """
        from kento import layers as _layers

        raw = _layers.resolve_image_id(ref)
        if not raw:
            raise ImageNotFoundError(
                f"could not resolve content id for image: {ref}"
                f" — is it present in the local store?  kento pull {ref}"
            )
        return Digest.parse(raw)

    # ------------------------------------------------------------------- #
    # Runtime lifecycle (§4.4) — ADDITIVE wrappers over kento.layers/kento.vm.
    # ------------------------------------------------------------------- #
    def prepare(self, state_dir: Path) -> None:
        """Materialize the overlay base; NO host mount (§4.4).

        Runs the fail-closed count/byte preflight (``layers.preflight_overlay_layers``
        — refuses an image whose lowerdir would overrun the mount(2) page) and
        creates the writable ``upper``/``work`` dirs the overlay mount needs.
        Wraps the existing functions; reimplements none of their logic.
        """
        from kento import layers as _layers

        lowerdir = self._render_lowerdir_abs()
        _layers.preflight_overlay_layers(lowerdir, state_dir)
        (state_dir / "upper").mkdir(parents=True, exist_ok=True)
        (state_dir / "work").mkdir(parents=True, exist_ok=True)

    def mount(self, host_dir: Path, state_dir: Path) -> None:
        """Overlay-mount the prepared base at ``host_dir/rootfs`` (§4.4).

        Delegates verbatim to ``vm.mount_rootfs`` (the host overlay-mount logic
        kento uses today) — same behavior, same options, same short-link
        rendering. ``mount_rootfs`` itself re-derives the lowerdir from the
        absolute layers string via ``to_overlay_lowerdir`` (principle 3:
        render the lowerdir at the boundary), so we hand it the absolute form.
        """
        from kento import vm as _vm

        _vm.mount_rootfs(host_dir, self._render_lowerdir_abs(), state_dir)

    def unmount(self, host_dir: Path) -> None:
        """Inverse of ``mount`` — unmount ``host_dir/rootfs`` (§4.4).

        Delegates to ``vm.unmount_rootfs``.
        """
        from kento import vm as _vm

        _vm.unmount_rootfs(host_dir)

    def release(self, state_dir: Path) -> None:
        """Inverse of ``prepare`` (§4.4).

        The writable upper/work tree is per-instance state the caller's storage
        policy owns (it may persist across restarts), so releasing it is not
        the image's call to make: this is a no-op for the overlay representation
        and exists to satisfy the four-primitive contract symmetrically. The
        instance's create/destroy routine removes ``state_dir`` when its policy
        says to (Phase 6 wiring), not the image.
        """
        return None

    # ----- internal: render the absolute lowerdir for the wrapped funcs -----
    def _render_lowerdir_abs(self) -> str:
        """Render the absolute ``<root>/<id>/diff`` colon-joined lowerdir.

        The boundary renderer (principle 3): turns the cached ``layers`` +
        ``overlay_root`` snapshot back into the absolute colon-joined string the
        wrapped ``layers``/``vm`` functions consume — a single uniform
        ``<overlay_root>/<id>/diff`` join (every ``Layer.id`` is a podman id,
        §4.1; ``overlay_root`` is always a real ``Path``). ``mount_rootfs`` /
        ``preflight_overlay_layers`` re-derive the short ``l/<short>`` form from
        this via ``to_overlay_lowerdir`` themselves, so we render the absolute
        form here — keeping the wrap a true delegation with no forked logic.
        """
        root = self.overlay_root
        return ":".join(str(root / layer.id / "diff") for layer in self.layers)


def _decompose_layers(
    abs_lowerdir: str, overlay_root: str,
) -> tuple[Path, tuple[Layer, ...]]:
    """Decompose the resolved lowerdir into ``(overlay_root, layers)`` (§4.1).

    Sources, both already obtained via the wrapped ``kento.layers`` functions
    (no forked logic):

    * ``abs_lowerdir`` — the absolute ``<root>/<id>/diff:...`` string from
      ``resolve_layers``. This is the unambiguous source of each layer's podman
      ``<id>`` (§4.1: ``Layer.id`` IS that ``<id>``), so we read ids from here
      rather than from ``to_overlay_lowerdir``'s lossy ``l/<short>`` form.
    * ``overlay_root`` — ``to_overlay_lowerdir``'s shared store base. Used
      directly when non-empty.

    ``Layer.short_link`` is read from each layer's ``<root>/<id>/link`` file —
    the same source ``to_overlay_lowerdir`` uses — and is ``""`` when absent
    (the mount then falls back to ``<id>/diff``, matching that function).

    For a podman OCI image every GraphDriver layer shares the store root, so a
    root is always derivable: prefer ``to_overlay_lowerdir``'s, else strip
    ``/<id>/diff`` from the absolute entries. The §4.1 ``overlay_root: Path`` is
    thus always a real path. A genuinely unexpected store layout (no shared
    ``<root>/<id>/diff`` shape) is a typed ``StateError`` — never a fabricated
    rootless/degenerate value (gate C, principle 5).
    """
    from kento.errors import StateError

    entries = [e for e in abs_lowerdir.split(":") if e]
    if not entries:
        raise StateError(
            f"image resolved to no overlay layers: {abs_lowerdir!r}"
        )

    paths = [Path(e) for e in entries]
    # Every entry must be the <root>/<id>/diff shape so we can name the podman
    # id and a shared root (the podman GraphDriver invariant for OCI images).
    for p in paths:
        if p.name != "diff" or p.parent == p.parent.parent:
            raise StateError(
                f"unexpected overlay store layout (entry is not "
                f"<root>/<id>/diff): {p}"
            )

    roots = {str(p.parent.parent) for p in paths}
    if overlay_root:
        root = Path(overlay_root)
    elif len(roots) == 1:
        root = Path(next(iter(roots)))
    else:
        raise StateError(
            "overlay layers do not share a common store root: "
            + ", ".join(sorted(roots))
        )

    layers: list[Layer] = []
    for p in paths:
        layer_id = p.parent.name  # the <id> in <root>/<id>/diff
        short_link = _read_layer_short_link(p.parent)
        layers.append(Layer(id=layer_id, short_link=short_link))
    return root, tuple(layers)


def _read_layer_short_link(layer_dir: Path) -> str:
    """Read the ``l/<short>`` token from ``<layer_dir>/link``; ``""`` if absent.

    Mirrors ``to_overlay_lowerdir``'s per-layer ``link``-file read and its
    graceful degradation: an unreadable/missing link file yields ``""`` (the
    mount then uses the ``<id>/diff`` form). Never raises.
    """
    try:
        return (layer_dir / "link").read_text().strip()
    except OSError:
        return ""


# --------------------------------------------------------------------------- #
# VolumeImage — DOCUMENTED STUB (first post-1.0 feature; §4.1, §4.5).
#
# Declares the fields per §4.1; declares NO runtime lifecycle. Because it leaves
# Image's abstract methods unimplemented it stays abstract and is NOT
# instantiable — by design, until the feature is built. Not wired into
# resolution.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, kw_only=True)
class VolumeImage(Image):
    """ONE partition (single fs) over block-overlay — **first post-1.0 feature**.

    **STUB — NOT BUILT IN 1.0 (§4.5).** The fields are declared per §4.1 so the
    family shape is visible and the ``Image`` ABC is shown to accommodate the
    block representation non-breakingly (principle 7), but **no runtime
    lifecycle is implemented**: ``VolumeImage`` therefore inherits ``Image``'s
    abstract ``prepare``/``mount``/``unmount``/``release`` unimplemented and is
    consequently **abstract / uninstantiable** until the feature lands. Do not
    construct it; do not wire it into resolution.

    Fields (§4.1):

    * ``path`` — the resolved read-only base partition image.
    * ``format`` — ``DiskFormat`` (RAW | QCOW2).
    * ``backing`` — block backing chain (qcow2); the base may itself be a chain.

    The per-instance writable UPPER is created at ``prepare`` and governed by
    ``StorageMode`` — it is instance/runtime state, NOT a field here (§4.3).
    The VM recipe for a ``VolumeImage`` is **prepare-only** → block device, no
    ``mount`` (§4.4 matrix); the ``Image`` lifecycle split (prepare vs mount)
    already accommodates that cell without rework when this is built.
    """

    path: Path
    format: DiskFormat
    backing: "VolumeImage | None"


# --------------------------------------------------------------------------- #
# CompositeImage — DOCUMENTED STUB / PLAN ONLY (§4.1, §4.5).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, kw_only=True)
class CompositeImage(Image):
    """Images composited at mount points — **PLAN ONLY, not built up front**.

    **STUB — NOT BUILT (§4.5).** ``mounts`` maps a mountpoint to an ``Image``,
    e.g. ``{"/": LayeredImage, "/home": VolumeImage}`` — this is the
    image-lifecycle EPIC's mount facility / per-path persistence ("persistent
    ``/home`` over an ephemeral root"); the root mount (``"/"``) is the bootable
    one. Declared with its field per §4.1 so the ``Image`` ABC is shown to
    accommodate composition non-breakingly (principle 7), but **no runtime
    lifecycle is implemented** — it inherits ``Image``'s abstract primitives
    unimplemented and is therefore **abstract / uninstantiable** until it is
    built. Do not construct it; do not wire it into resolution.
    """

    mounts: dict[str, Image]
