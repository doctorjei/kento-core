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

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from kento._diagnosis import Diagnosis, PruneScope, ReclaimReport
from kento._references import Digest, OciReference, SourceReference
from kento.errors import ImageNotFoundError, KentoError, StateError

_images_logger = logging.getLogger("kento")

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
        of yielding a sentinel. No ``""``/``None`` ever escapes.

        **Bare-hex normalization (the podman boundary).** ``resolve_image_id``
        runs ``podman ... --format {{.Id}}``, and real podman (confirmed on
        5.4.2) returns a **bare 64-char sha256 hex with NO ``algorithm:``
        prefix**, e.g. ``279c3d3b…``. A bare hex is correctly NOT a valid
        ``Digest`` *string* (§3.8 grammar = ``algorithm:encoded``), so feeding
        it to ``Digest.parse`` would raise ``MalformedReference`` on every real
        image. The id IS a sha256 digest rendered bare, so we normalize HERE,
        at the boundary, without loosening the §3.8 grammar:

        * if the raw already carries an ``algorithm:`` prefix (some podman/docker
          builds DO prefix), parse it through ``Digest.parse`` (faithful — keeps
          a non-sha256 algorithm intact); else
        * construct ``Digest(algorithm="sha256", encoded=<hex>)`` from the bare
          form. ``Digest.__post_init__`` runs the SAME ``validate_digest``
          (sha256 => exactly 64 lowercase hex), so a garbage non-hex id still
          raises a typed ``MalformedReference`` — never a bad ``Digest``.

        ``resolve_image_id`` itself is unchanged (its ``""``-tolerant callers
        stay until Phase 6).
        """
        from kento import layers as _layers

        raw = _layers.resolve_image_id(ref)
        if not raw:
            raise ImageNotFoundError(
                f"could not resolve content id for image: {ref}"
                f" — is it present in the local store?  kento pull {ref}"
            )
        if ":" in raw:
            # Already prefixed (algorithm:encoded) — parse faithfully so a
            # non-sha256 algorithm survives. Do NOT assume sha256 here.
            return Digest.parse(raw)
        # Bare {{.Id}} hex (the real-podman shape) — it is a sha256 digest
        # rendered without its prefix. Construct the typed value; the
        # constructor validates it (a garbage non-hex id raises a typed error).
        return Digest(algorithm="sha256", encoded=raw)

    # ------------------------------------------------------------------- #
    # Content lifecycle (§4.4, §11.5 M19–M21/M23) — acquire / enumerate /
    # remove. ADDITIVE wrappers over kento.layers / podman; produce/manage
    # LayeredImages (the 1.0 OCI-store representation, §4.4).
    #
    # PLACEMENT (disclosed): §11.5 phrases these `Image.pull -> Self`, but for
    # 1.0 they are OCI-store ops that produce/manage LayeredImages (§4.4), and
    # `Image` is a genuine ABC (abstract prepare/mount/...) that cannot be
    # instantiated — so a classmethod resolving to `cls(...)` only works on the
    # concrete LayeredImage. They live here as LayeredImage classmethods
    # (`pull`/`get`/`list`) + instance `remove`; `Self` is satisfied (cls IS
    # LayeredImage). A future base-`Image` dispatch (when VolumeImage fetch
    # lands) is purely additive / non-breaking.
    # ------------------------------------------------------------------- #
    @classmethod
    def pull(cls, ref: "str | OciReference") -> "LayeredImage":
        """Acquire an OCI image from a registry into the local store (M19).

        ``podman pull <ref>``, then resolve the now-local image to a populated
        handle via :meth:`resolve`. Accepts a ``str`` or an ``OciReference``:
        a ``str`` is parsed through ``OciReference.parse`` (§2 principle 3 — we
        never hand an unvalidated string to the shell; a malformed ref raises
        ``MalformedReference`` BEFORE any podman call). No ``force`` (§11.5 M19:
        podman re-pulls moved tags / no-ops identical digests). Raises a typed
        ``SubprocessError`` on pull failure (principle 5), never a sentinel.
        """
        from kento import layers as _layers
        from kento.subprocess_util import run_or_die

        oci = cls._coerce_ref(ref)
        rendered = oci.render()
        run_or_die(
            [*_layers._podman_cmd(), "pull", rendered],
            "pull image",
            name=rendered,
        )
        return cls.resolve(oci)

    @classmethod
    def get(cls, ref: "str | OciReference") -> "LayeredImage":
        """Resolve an ALREADY-LOCAL image to a handle; no network (M20).

        Read-only: :meth:`resolve` queries the local podman store only (no
        ``pull``). When the image is absent ``layers.resolve_layers`` raises
        ``ImageNotFoundError`` — :meth:`get` lets that propagate (mirrors
        ``Instance.get``); it does NOT fabricate a handle for a missing image
        (gate C). A ``str`` ref is parsed/validated as in :meth:`pull`.
        """
        return cls.resolve(cls._coerce_ref(ref))

    @classmethod
    def list(cls) -> "list[LayeredImage]":
        """Enumerate local OCI images as resolved handles (M21).

        Queries ``podman images`` for repository references (new, additive — we
        do NOT wrap ``images.list_images()``, which renders a DISPLAY TABLE
        STRING, not objects), then resolves each to a ``LayeredImage``.

        TOTAL OVER THE STORE (disclosed policy, grounded in §2 + §7.2's
        ``Status.UNKNOWN`` totality rationale): a single image that fails to
        resolve mid-enumeration — e.g. a tag that raced a removal, or an
        unexpected store layout for one entry — is SKIPPED WITH A LOG, not
        raised, so one bad image cannot blow up enumeration of every other. A
        hard failure of the enumerating query itself (``podman images``) is a
        different thing and DOES raise a typed ``SubprocessError`` — that is the
        whole listing failing, not one entry. NO provenance flag in 1.0 (§11.5
        M21 — that lands with the lifecycle EPIC).
        """
        from kento import layers as _layers
        from kento.subprocess_util import run_or_die

        result = run_or_die(
            [*_layers._podman_cmd(), "images",
             "--format", "{{.Repository}}:{{.Tag}}"],
            "list images",
        )
        images: list[LayeredImage] = []
        for line in result.stdout.splitlines():
            entry = line.strip()
            # podman renders a dangling (untagged) image as "<none>:<none>";
            # such an entry has no resolvable repository ref, so skip it (it is
            # surfaced by the lifecycle-EPIC provenance work, not 1.0 list).
            if not entry or "<none>" in entry:
                continue
            try:
                oci = OciReference.parse(entry)
                images.append(cls.resolve(oci))
            except KentoError as exc:
                # Total over the store: one unresolvable entry is logged and
                # skipped, never fatal to the whole enumeration.
                _images_logger.warning(
                    "skipping unresolvable image %r: %s", entry, exc)
        return images

    @classmethod
    def prune(
        cls, *, scope: PruneScope = PruneScope.DANGLING,
    ) -> ReclaimReport:
        """Reclaim DANGLING images; **never touches a held image** (M22).

        Removes unused/dangling images — untagged ``<none>`` layers podman no
        longer references — and returns a :class:`ReclaimReport` of what was
        removed (``reclaimed``) and what podman refused (``failed`` = ``(id,
        reason)`` pairs, surfaced not swallowed — the 1.6.2 contract). This is
        a store-level GC, so it is a ``classmethod`` mirroring ``pull``/``get``/
        ``list`` (it manages the store, not one handle); it is **distinct** from
        the kento orphan-HOLD GC (``images.prune`` / future
        ``Instance.prune_orphans``).

        The locked M22 signature carries ONE param, ``scope: PruneScope =
        PruneScope.DANGLING`` (§11.5). ``DANGLING`` is the only 1.0 value (the
        further provenance scopes land with the lifecycle EPIC, §11.9). Any
        other ``scope`` value cannot occur in 1.0, but is rejected with a typed
        :class:`ValidationError` (principle 5 — a typed raise, never a silent
        no-op or a fabricated result) so a future caller passing an
        unimplemented scope fails loudly instead of silently pruning DANGLING
        anyway (gate C).

        There is **NO** ``dry_run`` param: the locked signature has only
        ``scope``, so ``prune`` EXECUTES and the report is ``dry_run=False``.

        **The held-image invariant is guaranteed, not assumed.** A held image is
        pinned by a stopped ``kento-hold.<guest>`` container, so podman's
        ``dangling=true`` filter normally already excludes it — but the spec
        says *never*, so we EXCLUDE held content explicitly rather than trusting
        the filter: any dangling id that matches a hold's pinned **content id**
        (``images._hold_image_ids()`` label values, and ``images._holds()``'s
        ``{{.Image}}`` field for a modern id-pinned hold) is skipped. We reuse
        the existing hold knowledge (no forked hold logic) — the same content-id
        keying :meth:`_is_held` uses, since a dangling image is untagged and so
        is identified by its **id**, not a ``repo:tag``.

        Raises a typed :class:`SubprocessError` (principle 5) if the dangling
        enumeration query itself fails (the whole prune failing, distinct from a
        per-image ``rmi`` refusal which is surfaced in ``failed``).
        """
        from kento import images as _images_mod
        from kento import layers as _layers
        from kento.errors import SubprocessError
        from kento.subprocess_util import run_or_die

        if scope is not PruneScope.DANGLING:
            from kento.errors import ValidationError

            raise ValidationError(
                f"unsupported prune scope: {scope!r}"
                " — only PruneScope.DANGLING is supported in this release"
                " (further provenance scopes land with the image-lifecycle"
                " EPIC)."
            )

        # Enumerate dangling images by content id ({{.Id}} = the BARE 64-hex
        # sha256 real podman emits — the same form layers.resolve_image_id
        # yields and create_image_hold stores in its label, so this compares
        # apples-to-apples (bare-hex vs bare-hex) against a hold's pinned content
        # id below). A dangling image is untagged ("<none>"), so its id IS its
        # identifier in the report.
        result = run_or_die(
            [*_layers._podman_cmd(), "images",
             "--filter", "dangling=true", "--format", "{{.Id}}"],
            "list dangling images",
        )

        # Held content ids — guarantee the "never touches a held image"
        # invariant ourselves rather than trusting podman's filter. Compose the
        # existing hold knowledge (no forked hold logic): the authoritative
        # io.kento.hold-image-id label values, plus _holds()'s {{.Image}} field
        # (the pinned content id for a modern hold; a repo:tag for a legacy
        # hold, which can never equal a dangling image's id and so is inert).
        held_ids: set[str] = set(_images_mod._hold_image_ids().values())
        for _name, img in _images_mod._holds():
            if img:
                held_ids.add(img)

        reclaimed: list[str] = []
        failed: list[tuple[str, str]] = []
        seen: set[str] = set()
        for line in result.stdout.splitlines():
            image_id = line.strip()
            if not image_id or image_id in seen:
                continue
            seen.add(image_id)
            if image_id in held_ids:
                # Belt-and-suspenders: a held image must never be removed even
                # if it somehow surfaced as dangling (spec invariant).
                _images_logger.debug(
                    "prune: skipping held dangling image %s", image_id)
                continue
            try:
                run_or_die(
                    [*_layers._podman_cmd(), "rmi", image_id],
                    "remove dangling image", name=image_id,
                )
                reclaimed.append(image_id)
            except SubprocessError as exc:
                # Surface the refusal (1.6.2 contract) — do NOT swallow it, and
                # do NOT abort the batch on one refusal.
                failed.append((image_id, str(exc)))

        return ReclaimReport(
            dry_run=False,
            reclaimed=tuple(reclaimed),
            failed=tuple(failed),
        )

    def remove(self, *, force: bool = False) -> None:
        """Remove THIS image from the local store (M23). ``-> None``.

        ``podman rmi <ref>``. REFUSES BY DEFAULT if a kento hold pins THIS
        image's content (a stopped ``kento-hold.<name>`` container — see
        ``layers``/``images``): removing a held image would break the guest that
        pinned it. The hold is identified by the **content id** the image was
        pinned from, matched against this handle's resolved ``self.id``
        (:meth:`_is_held` — NOT the ``repo:tag``, which would never match a
        modern id-pinned hold). ``force`` removes past the hold (``podman rmi
        --force``, which detaches the hold container). Raises ``StateError``
        when held and not forced; raises a typed ``SubprocessError`` on a podman
        failure (principle 5). Reuses the existing hold knowledge
        (``images._hold_image_ids()`` / ``images._holds()``) — does NOT
        reimplement hold detection.
        """
        from kento import layers as _layers
        from kento.subprocess_util import run_or_die

        rendered = self.source.render()
        if not force and self._is_held(rendered):
            raise StateError(
                f"image is held by a kento guest and was not removed: {rendered}"
                "\n  A stopped kento-hold.<guest> container pins it so the"
                " guest's overlay base stays present."
                "\n  Remove the guest(s) first, or force removal with"
                " remove(force=True) (kento image rm --force)."
            )
        cmd = [*_layers._podman_cmd(), "rmi"]
        if force:
            cmd.append("--force")
        cmd.append(rendered)
        run_or_die(cmd, "remove image", name=rendered)

    def _is_held(self, rendered_ref: str) -> bool:
        """True iff a kento hold container pins THIS image's content (M23).

        Ground truth (``layers.create_image_hold``): the modern hold is created
        FROM THE RESOLVED IMAGE ID and records it in an ``io.kento.hold-image-id``
        label; only a legacy hold (older podman, id unresolvable at create time)
        pins by tag. So a hold's image is identified by a **content id**, NOT a
        ``repo:tag`` — comparing ``_holds()``'s ``{{.Image}}`` to a re-normalized
        ``source.render()`` would NEVER match the common case and would let a
        held image be removed without ``force`` (the M23 violation B1).

        We therefore key on the content **id this handle already carries**
        (``self.id``, a ``Digest`` resolved via the same ``resolve_image_id``
        the hold used — apples-to-apples), composing the existing hold knowledge
        (no forked hold logic):

        * the authoritative ``io.kento.hold-image-id`` label
          (``images._hold_image_ids()`` — the purpose-built content-id record); and
        * ``images._holds()``'s ``{{.Image}}`` field, which is that same content
          id for a modern hold OR the ``repo:tag`` for a legacy hold — so we
          accept a match there against EITHER the content id or the rendered tag.

        **Bare-hex comparison space (the real-podman shape).**
        ``create_image_hold`` stores ``resolve_image_id``'s output in the label,
        and real podman returns a **bare** 64-hex ``{{.Id}}`` (NO ``sha256:``
        prefix). Our ``self.id`` renders ``sha256:<hex>``, so a naive
        ``self.id.render() == <bare hex>`` would never match → a held image would
        become removable without ``force`` (silent M23 break in the real env).
        We compare in podman's NATIVE bare-hex space: ``self.id.encoded`` (the
        bare hex) against each value, stripping a leading ``sha256:`` from EITHER
        side first so a future prefixed label still matches. The legacy
        ``repo:tag`` fallback is preserved on the raw (un-stripped) value.
        """
        from kento import images as _images_mod

        # Compare in podman's native bare-hex space ({{.Id}}). self.id.encoded
        # is the bare hex; strip a leading "sha256:" off either side defensively
        # so a prefixed label/{{.Image}} still matches.
        content_hex = self.id.encoded

        def _bare(value: str) -> str:
            return value.split(":", 1)[1] if ":" in value else value

        # Authoritative: the io.kento.hold-image-id label records the pinned id.
        for label_id in _images_mod._hold_image_ids().values():
            if label_id and _bare(label_id) == content_hex:
                return True
        # Fallback: _holds() {{.Image}} is the content id (modern hold) or the
        # repo:tag (legacy hold pinned by ref) — match the bare content id OR
        # the rendered tag (the latter against the raw value, not stripped).
        for _name, img in _images_mod._holds():
            if img and (_bare(img) == content_hex or img == rendered_ref):
                return True
        return False

    def diagnose(self) -> Diagnosis:
        """Run the read-only IMAGE-domain health checks (§11.8 D3 b).

        Projects the IMAGE-domain findings from the existing
        ``diagnose.run_diagnostics()`` scan into a typed :class:`Diagnosis`. The
        IMAGE domain today is the image-HOLD health checks (stale holds +
        hold/guest content-id drift — the runtime category ``"hold"``).

        **Scope — collection-level, NOT per-image (DISCLOSED).** The runtime
        hold checks are GLOBAL scans whose findings carry no clean per-image
        subject (their ``scope`` is ``"host"`` and the image ref is embedded only
        in the message text, which the additive wrapper must not parse, §2
        principle 3). So ``image.diagnose()`` returns the **collection-level**
        IMAGE-domain findings (overall hold health), NOT a per-image filter on
        ``self``. Richer per-image diagnostics (dangling / stale / missing-layers
        attributed to a single image) arrive with the image-lifecycle EPIC that
        refactors ``diagnose.py``; until then a per-image filter would silently
        return an empty diagnosis (the holds have no per-image subject to match),
        which is a worse lie than honestly reporting the collection's hold
        health. This is the under-specification the plan flagged when it deferred
        ``image.diagnose()`` to this block.

        Performs I/O (the scan) — an explicit, named method (§2 principle 2); the
        returned ``Diagnosis`` is inert.
        """
        import importlib

        from kento._diagnosis import DiagnosisDomain, diagnosis_from_report

        # Reach the diagnose SUBMODULE, not the top-level ``kento.diagnose``
        # FUNCTION (Block 10's name-collision foot-gun) — see Instance.diagnose.
        _diagnose = importlib.import_module("kento.diagnose")
        report = _diagnose.run_diagnostics(None)
        return diagnosis_from_report(report, domain=DiagnosisDomain.IMAGE)

    @staticmethod
    def _coerce_ref(ref: "str | OciReference") -> OciReference:
        """Normalize a ``str | OciReference`` argument to an ``OciReference``.

        A ``str`` is parsed through ``OciReference.parse`` (faithful, raises
        ``MalformedReference`` on a bad ref) so no unvalidated string ever
        reaches the shell (§2 principle 3); an ``OciReference`` passes through
        unchanged. The single coercion point shared by ``pull``/``get``.
        """
        if isinstance(ref, OciReference):
            return ref
        return OciReference.parse(ref)

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
