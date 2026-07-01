"""The ``Image`` family — directory-tree views of a data source.

A ``SourceReference`` (§3.8, a *locator* — unresolved) **resolves to** an
``Image`` (this module). An ``Image`` is **a (possibly writable) directory-tree
VIEW OF A DATA SOURCE** (§4.1, AMENDED run 36): members may be read-only (OCI,
content-addressed) OR writable (a volume / dir-backed view). The family is keyed
by **representation** (how the base is stored & overlaid), orthogonal to the
locator's scheme:

* ``LayeredImage`` (ABC) — "the layering": a union of read-only directory layers
  (overlayfs). Abstract and source-agnostic; the concrete layer shape + identity
  are decided per subclass.
* ``OciImage`` — the podman/OCI-store ``LayeredImage``. Holds all the OCI/podman
  specifics (``source: OciReference``, the content ``id``, ``layers``,
  ``overlay_root``) and the full lifecycle.
* ``LocalDirectoryImage`` — the first NON-OCI ``LayeredImage`` (URL-VM Phase B,
  OPTION 2): a ``.txz`` rootfs fetched from an ``https://`` URL and extracted into
  a directory that becomes the VM's single overlay lowerdir. ``source:
  UrlReference``; NO content ``id`` (identity is the URL location, not a digest).
* ``VolumeImage`` — one partition (single fs) over block-overlay. The first
  post-1.0 feature; declared here as a **documented stub** with NO lifecycle.
* ``CompositeImage`` — Images composited at mount points (the image-lifecycle
  EPIC's mount facility). **Plan only**, NOT built up front; a documented stub.

**Capability vs policy (§4.1).** ``Image.is_writable()`` is the *capability* —
can the underlying data source be written? — determined by the source, not the
representation class (``OciImage`` → ``False`` because OCI store layers are
read-only by spec; a future volume/dir view answers per-source). HOW an instance
mounts the image (read-only vs read-write) is a separate *policy* carried by
``instance.storage: StorageMode`` — NOT a field on the image.

**Identity (§4.1, §4.3).** Identity is the ``source`` universally (on the base).
A content ``Digest`` (``id``) is the *content-addressed addition* a member like
``OciImage`` carries; it is NOT on the base, because a future writable image's
identity is its location, not a digest. ``OciImage.id`` is the *resolved* content
identity and is **not** the locator's OCI-only ``digest`` pin — do not conflate
the two (§4.3 last bullet).

Two layers, two rules (§2 principle 2). The ``Image`` *dataclass fields* are
inert, frozen value data; they carry no I/O. The **runtime-lifecycle methods**
are the named, enumerable moments where the handle reaches the outside world —
``prepare`` / ``mount`` / ``unmount`` / ``release`` (§4.4). On ``OciImage`` these
are ADDITIVE wrappers: they **delegate** to the existing procedural functions in
``kento.layers`` and ``kento.vm`` and do NOT reimplement their logic or touch
their live callers (``create.py`` / ``vm.py`` / ``hook.py`` / the CLI).

The public surface (``Image``, ``LayeredImage``, ``OciImage``,
``LocalDirectoryImage``, ``Layer``, ``DiskFormat``, ``VolumeImage``,
``CompositeImage``) is re-exported flat from ``kento`` — refer to
``kento.OciImage``, not ``kento._images.OciImage``.

Spec: ``~/workspace/kento-core-api-design.md`` §4.1 (AMENDED run 36), §4.3, §4.4,
§12.1; §3.8 (``Digest`` is the content id).
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from kento._diagnosis import Diagnosis, PruneScope, ReclaimReport
from kento._references import Digest, OciReference, SourceReference, UrlReference
from kento._result import Ok, Result, _error_from
from kento.errors import ImageNotFoundError, KentoError, StateError

_images_logger = logging.getLogger("kento")

# A bare podman content id as it appears in a hold's ``.Image`` field: a 64-char
# lowercase sha256 hex with NO ``algorithm:`` prefix and no repository/tag. Such
# a value is an image ID, not an OCI reference, so ``Hold.list`` builds a Digest
# from it rather than feeding it to ``OciReference.parse`` (§12.3 / JC3). Anchored
# + ASCII so a 64-char *tag* (which would contain non-hex chars or live after a
# repo path) is not misread as an id.
_RE_BARE_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)

__all__ = [
    "DiskFormat",
    "Layer",
    "Hold",
    "ManagedStatus",
    "ImageRecord",
    "Image",
    "LayeredImage",
    "OciImage",
    "LocalDirectoryImage",
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
    """One read-only podman-store layer of an ``OciImage`` (§4.1).

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
# Hold — a typed view of one kento prune-protection pin (§12.3).
#
# A hold pins a content-addressed image against ``podman prune`` so a live or
# stopped instance can't have its layers reaped. Physically it is a STOPPED
# podman container named ``kento-hold.<guest>`` carrying the label
# ``io.kento.hold-for=<guest>`` and EITHER an ``io.kento.hold-image-id`` label
# (modern, an id-pin) OR a tag-pinned ``.Image`` (legacy, older podman without
# the id label) — see layers.create_image_hold / images._holds /
# images._hold_image_ids for the ground truth this wraps.
#
# This is an ADDITIVE typed READ: it wraps the existing procedural queries and
# does NOT change how holds are created/removed/re-pinned (that stays the
# layers.py path). Holds + Digest are the content-addressed concept (the podman
# store); they do not apply to writable images (§12.3).
# --------------------------------------------------------------------------- #


def _digest_from_podman_id(raw: str) -> Digest:
    """Normalize a podman content-id string to a typed ``Digest`` (§3.2).

    The podman boundary (mirrors ``OciImage.resolve_id``): the
    ``io.kento.hold-image-id`` label records ``layers.resolve_image_id``'s
    output, which on real podman is a **bare 64-char sha256 hex with NO
    ``algorithm:`` prefix** (e.g. ``279c3d3b…``); some podman/docker builds DO
    carry the prefix. A bare hex is correctly not a valid ``Digest`` *string*
    (§3.8 grammar = ``algorithm:encoded``), so we normalize HERE without
    loosening the grammar:

    * prefixed (``algorithm:encoded``) → ``Digest.parse`` (faithful — a
      non-sha256 algorithm survives, no sha256 assumption);
    * bare hex → ``Digest(algorithm="sha256", encoded=<hex>)``. The constructor
      runs the SAME ``validate_digest`` (sha256 => exactly 64 lowercase hex), so
      a garbage id raises a typed ``MalformedReference`` rather than building a
      bad ``Digest`` (gate C: no malformed value escapes).
    """
    if ":" in raw:
        return Digest.parse(raw)
    return Digest(algorithm="sha256", encoded=raw)


def _parse_legacy_pinned(image: str) -> Digest | OciReference:
    """Parse a legacy hold's ``.Image`` field to ``Digest`` | ``OciReference``.

    A legacy hold (older podman, no ``io.kento.hold-image-id`` label) pins via
    its container's ``.Image`` field. That field is USUALLY a tag reference
    (e.g. ``docker.io/library/debian:12``) → ``OciReference.parse`` (§2
    principle 3 — never re-split a reference by hand). But podman may also
    render ``.Image`` as a **bare content id** (a 64-char sha256 hex with no
    ``algorithm:`` prefix and no path/tag), which ``OciReference.parse`` would
    reject as a bare-identifier name (it is an id, not a ref). We detect that
    shape and build a ``Digest`` instead — faithful to BOTH legacy ``.Image``
    forms (§12.3 / JC3).
    """
    if _RE_BARE_SHA256.match(image):
        return _digest_from_podman_id(image)
    # parse now returns a Result; .unwrap() preserves today's raise-on-bad-input
    # behavior exactly (an Error → ResultError, a KentoError) — Block P1.
    return OciReference.parse(image).unwrap()


@dataclass(frozen=True, kw_only=True)
class Hold:
    """A typed view of one kento prune-protection pin (§12.3).

    A frozen, inert value (§2 principle 2 — no I/O on access). ``instance`` is
    the guest the hold pins for (the ``io.kento.hold-for`` label). ``pinned`` is
    the pinned image identity, faithful to BOTH physical hold shapes (§2
    principle 1 — neither form is normalized away):

    * a ``Digest`` — the MODERN id-pin, from the ``io.kento.hold-image-id``
      label (a content id);
    * an ``OciReference`` — the LEGACY tag-pin, from the hold container's
      ``.Image`` field on older podman that predates the id label.

    The container name ``kento-hold.<instance>`` is DERIVABLE from ``instance``
    (principle 7), so it is NOT a field — add it only if a real consumer needs
    it (none does in 1.0). Created/removed/re-pinned via the procedural
    ``layers.py`` path; this type is the READ projection only.
    """

    instance: str
    pinned: Digest | OciReference

    @classmethod
    def list(cls) -> Result[list[Hold]]:
        """Return every kento hold, typed and sorted by instance name (§12.3).

        Public Result boundary (Result-propagation sweep, Block S2): the body is
        the raising :meth:`_list`; any internal ``KentoError`` is caught here and
        converted to a one-condition ``Error`` via ``_error_from`` (§2 principle
        5). A non-``KentoError`` is a panic and propagates.

        SPLIT from ``_list`` (the P1 ``parse``/``_parse`` precedent) because the
        still-raising ``ImageRecord._list`` calls the raising form so an internal
        ``ImageNotFoundError`` keeps its real kind at THAT public boundary rather
        than collapsing to ``INTERNAL`` (the KIND-FIDELITY rule).

        Wraps the existing procedural queries AS-IS: ``images._holds()`` (the
        ``(held_for, .Image)`` pairs) joined with ``images._hold_image_ids()``
        (the ``held_for -> io.kento.hold-image-id`` map). For each hold:

        * a non-empty id label present → MODERN id-pin, ``pinned`` is a
          ``Digest`` (``_digest_from_podman_id``);
        * no id label → LEGACY tag-pin, ``pinned`` is parsed from the ``.Image``
          field (``_parse_legacy_pinned`` — an ``OciReference``, or a ``Digest``
          for a bare-id ``.Image``, JC3).

        Mirrors ``Instance.list()`` (collection-scoped, global). Sorted by
        ``instance`` for a stable, deterministic listing (JC4). A hold whose
        legacy ``.Image`` is empty (no id label and no ``.Image``) is SKIPPED
        WITH A LOG — there is no faithful ``pinned`` to build, and one
        unparseable hold must not hide every healthy one (the same totality
        stance ``Instance.list`` takes over the store).
        """
        try:
            return Ok(value=cls._list())
        except KentoError as exc:
            return _error_from(exc)

    @classmethod
    def _list(cls) -> list[Hold]:
        """The raising body of :meth:`list` (§12.3).

        Internal: the public :meth:`list` boundary catches its ``KentoError`` and
        converts to an ``Error``. Other still-raising internal callers
        (``ImageRecord._list``) use THIS form so a raised kind reaches their own
        public boundary intact (the KIND-FIDELITY rule).
        """
        from kento.images import _hold_image_ids, _holds

        ids = _hold_image_ids()
        holds: list[Hold] = []
        for held_for, image in _holds():
            image_id = ids.get(held_for, "")
            if image_id:
                pinned: Digest | OciReference = _digest_from_podman_id(image_id)
            elif image:
                pinned = _parse_legacy_pinned(image)
            else:
                # No id label AND no .Image — nothing faithful to pin to.
                _images_logger.warning(
                    "skipping hold for %r: no image-id label and no .Image",
                    held_for,
                )
                continue
            holds.append(cls(instance=held_for, pinned=pinned))
        return sorted(holds, key=lambda h: h.instance)


# --------------------------------------------------------------------------- #
# ManagedStatus + ImageRecord — kento's typed LEDGER ENTRY about an image
# (§12.4, JC1 — the 1.0 blocker).
#
# An ImageRecord is NOT an Image (the artifact, podman store): it is the typed
# projection of kento's OWN markers about an image — the per-guest kento-image
# references + the kento-hold.<guest> pins (Hold from SD2) + (future) provenance.
# Composition (has-a), keyed on the content id. It absorbs id-centricity, the
# ref<->id duality, the dangling case, and future provenance, keeping Image (§4)
# clean. The string-returning images.list_images() it REPLACES is removed; the
# data helpers it shared (images._guest_image_refs) feed ImageRecord.list().
# --------------------------------------------------------------------------- #


class ManagedStatus(str, Enum):
    """The kento managed-image status (§12.4). 2-state, locked.

    ``str, Enum`` so a member compares/serializes as its wire value
    (``ManagedStatus.IN_USE == "in-use"``) — the library idiom for closed value
    sets (NetworkMode / Status / StorageMode / DiskFormat). The two values match
    the legacy ``kento images`` table's STATUS column EXACTLY (LOCKED run 36 —
    NOT a 3-state enum; ``dangling`` is a SEPARATE, orthogonal derived bool on
    ``ImageRecord``, not a status value).
    """

    IN_USE = "in-use"
    ORPHANED = "orphaned"


@dataclass(frozen=True, kw_only=True)
class ImageRecord:
    """kento's typed LEDGER ENTRY about one image, keyed on content id (§12.4).

    The typed projection of kento's own markers about an image — distinct from
    ``Image`` (§4, the artifact). A frozen, inert value (§2 principle 2 — no I/O
    on field access); the I/O lives in the explicit classmethods
    (:meth:`list` / :meth:`get`) and in ``image.record()`` / :meth:`resolve`.

    Fields (§12.4 verbatim):

    * ``id`` — the content **identity**, a ``Digest`` — the KEY. MANDATORY
      (id-centric; it survives a moved tag, and is the phantom-copy guard). A
      record is always keyed on resolvable content; a rare legacy tag-only hold
      whose content is GONE has no ``id`` and is logged + skipped by
      :meth:`list` rather than represented.
    * ``refs`` — the tag(s) kento has seen at this id: ``0`` = dangling (no
      surviving tag), ``N`` = multi-tagged. Sourced from the guest ``kento-image``
      references that resolve to this id, deduplicated, in a stable order.
    * ``guests`` — the instance names whose ``kento-image`` references this id.
    * ``holds`` — the pins (``Hold`` from SD2; ``N`` — one per guest hold). NOT a
      bool: the typed pins themselves.

    Derived properties (matching today's ``images`` table; all pure, no I/O):

    * ``held`` — ``bool(self.holds)``.
    * ``in_use`` — ``bool(self.guests)``.
    * ``dangling`` — ``not self.refs`` (a SEPARATE axis from ``status``: an
      orphaned image may or may not be dangling, §12.4).
    * ``status`` — ``IN_USE`` if any guest references it, else ``ORPHANED``.

    ``provenance`` is a FUTURE field (kento-pulled provenance — the image
    -lifecycle EPIC); it is deliberately NOT added in 1.0 (adding it later is
    non-breaking).
    """

    id: Digest
    refs: tuple[OciReference, ...] = ()
    guests: tuple[str, ...] = ()
    holds: tuple["Hold", ...] = ()

    def __post_init__(self) -> None:
        # Freeze iterable arguments to tuples (the frozen-dataclass coercion
        # idiom shared with OciImage / ReclaimReport — a frozen dataclass freezes
        # the binding, not a mutable list behind it).
        for field_name in ("refs", "guests", "holds"):
            value = getattr(self, field_name)
            if not isinstance(value, tuple):
                object.__setattr__(self, field_name, tuple(value))

    # ----- derived properties (pure; match today's images table) -----
    @property
    def held(self) -> bool:
        """True iff a kento hold pins this image (§12.4)."""
        return bool(self.holds)

    @property
    def in_use(self) -> bool:
        """True iff a guest references this image (§12.4)."""
        return bool(self.guests)

    @property
    def dangling(self) -> bool:
        """True iff NO surviving tag references this id (§12.4).

        A SEPARATE, orthogonal axis from :attr:`status` (LOCKED run 36): a
        held-only image with no guest ref is ``ORPHANED`` AND ``dangling``; an
        orphaned image that still carries a tag is ``ORPHANED`` but NOT dangling.
        """
        return not self.refs

    @property
    def status(self) -> ManagedStatus:
        """``IN_USE`` if a guest references this image, else ``ORPHANED`` (§12.4).

        Matches the legacy ``images`` table's STATUS column exactly: in-use means
        a kento guest references the image; an image present only via a hold (its
        guest gone) is orphaned.
        """
        return ManagedStatus.IN_USE if self.guests else ManagedStatus.ORPHANED

    # ----- ledger navigation — lazy, returns a Result -----
    def resolve(self) -> Result["Image"]:
        """Resolve THIS record's content to the live ``Image`` artifact (§12.4).

        Public Result boundary (Result-propagation sweep, Block S2): the inverse
        of ``image.record()``. Resolves by content **id** — the record's key —
        via the raising ``OciImage._get`` against the local store (no network).
        Performs I/O (an explicit named method, §2 principle 2). The
        ``ImageNotFoundError`` raised when the content is gone (a dangling record
        whose layers podman has since reaped, §12.4) is caught HERE and converted
        to ``Error(IMAGE_NOT_FOUND)`` — a predictable outcome (principle 5), never
        a sentinel. The record exists as a ledger marker even when the artifact
        does not, so resolution is the moment the absence surfaces.

        Calls the raising ``OciImage._get`` (not the Result-returning public
        ``get``) so the deep ``ImageNotFoundError`` reaches THIS boundary with its
        real kind rather than collapsing to ``INTERNAL`` (the KIND-FIDELITY rule).

        Resolving by the rendered ``id`` (``algorithm:encoded``) lets podman
        match the content regardless of which (if any) tag still points at it, so
        a dangling-but-held record still resolves while its layers survive.
        """
        try:
            return Ok(value=OciImage._get(self.id.render()))
        except KentoError as exc:
            return _error_from(exc)

    # ----- entry points — reconstruct from kento markers -----
    @classmethod
    def list(cls) -> Result["list[ImageRecord]"]:
        """Every kento-MANAGED image as a typed record, grouped by id (§12.4).

        Public Result boundary (Result-propagation sweep, Block S2): the body is
        the raising :meth:`_list`; an internal ``KentoError`` is caught here and
        converted to an ``Error`` via ``_error_from``. A non-``KentoError`` is a
        panic and propagates.

        SPLIT from ``_list`` (the P1 ``parse``/``_parse`` precedent) because the
        still-raising internal ``_for_id`` (used by ``Image.record()``, an S3
        method) calls the raising form — so a marker scan can be reused inside a
        raising context, and a future raise keeps its real kind at THAT public
        boundary rather than collapsing to ``INTERNAL`` (the KIND-FIDELITY rule).
        The public ``get`` likewise calls ``_list`` inside its own boundary.

        (Disclosed deviation from the director's WRAP-only classification:
        ``_for_id``/``Image.record()`` is a still-raising internal caller of
        ``list``, so a SPLIT is required, not a bare wrap.)

        Reconstructs the ledger from kento's markers (like ``Instance.list``
        builds its snapshot from many ``kento-*`` files): the dataset is every
        image **referenced by a guest OR pinned by a hold** (today's
        ``managed = refs | held`` boundary), one record per content **id**.
        Ordering is stable — sorted by the rendered id.
        """
        try:
            return Ok(value=cls._list())
        except KentoError as exc:
            return _error_from(exc)

    @classmethod
    def _list(cls) -> "list[ImageRecord]":
        """The raising body of :meth:`list` (§12.4).

        Internal: the public :meth:`list` boundary catches its ``KentoError`` and
        converts to an ``Error``. Still-raising internal callers (``get`` inside
        its own boundary, ``_for_id`` via ``Image.record()``) use THIS form so a
        raised kind reaches their own public boundary intact (KIND-FIDELITY rule).

        Grouping (the id-centric core):

        * Each guest ``kento-image`` reference (``images._guest_image_refs``) is
          resolved to its content id via ``OciImage.resolve_id`` (the same
          ``{{.Id}}`` podman boundary the holds use, so the ids compare
          apples-to-apples). The parsed reference is recorded under that id as a
          *seen tag* (``refs``); the referencing guests are recorded under it.
        * Each ``Hold`` (``Hold._list`` from SD2) contributes its content id: a
          MODERN id-pin already carries a ``Digest``; a LEGACY tag-pin
          (``OciReference``) is resolved to its id via ``OciImage.resolve_id``.

        **id is mandatory — unresolvable content is logged + skipped** (LOCKED
        run 36): a guest reference or legacy tag-pin whose content is GONE
        (``resolve_id`` raises ``ImageNotFoundError``) cannot be keyed, so it is
        logged and dropped rather than represented with a fabricated id. Modern
        id-pinned holds always carry their id even for vanished content, so a
        dangling-but-held image (no tag, ``refs=()``) is still represented.

        TOTAL OVER THE STORE (same stance as ``OciImage.list`` / ``Hold.list``):
        one unresolvable marker is skipped with a log, never fatal to the whole
        listing. Ordering is stable — sorted by the rendered id — so the listing
        is deterministic regardless of marker-scan order.
        """
        from kento.images import _guest_image_refs

        # Per-id accumulators. We key the buckets on the rendered Digest string
        # (a stable, hashable key) and keep the typed Digest alongside.
        digests: dict[str, Digest] = {}
        refs: dict[str, dict[str, OciReference]] = {}
        guests: dict[str, set[str]] = {}
        holds: dict[str, list[Hold]] = {}

        def _bucket(digest: Digest) -> str:
            key = digest.render()
            digests.setdefault(key, digest)
            refs.setdefault(key, {})
            guests.setdefault(key, set())
            holds.setdefault(key, [])
            return key

        # --- guest references: image string -> [guests] ---
        for image, guest_names in _guest_image_refs().items():
            parsed = _parse_legacy_pinned(image)
            # A guest image string is USUALLY a tag (OciReference); rarely a bare
            # content id (a Digest), which is the id itself and not a "seen tag".
            if isinstance(parsed, Digest):
                digest = parsed
                ref: OciReference | None = None
            else:
                ref = parsed
                try:
                    digest = OciImage.resolve_id(image)
                except ImageNotFoundError:
                    # Content gone — cannot key this guest reference. Faithful to
                    # "id mandatory": log + skip rather than fabricate an id.
                    _images_logger.warning(
                        "skipping guest reference %r (guests %s): content not "
                        "resolvable in the local store",
                        image, ",".join(sorted(guest_names)),
                    )
                    continue
            key = _bucket(digest)
            if ref is not None:
                refs[key][ref.render()] = ref
            guests[key].update(guest_names)

        # --- holds: each pins a content id ---
        for hold in Hold._list():
            if isinstance(hold.pinned, Digest):
                digest = hold.pinned
            else:
                # Legacy tag-pin: resolve the ref to its content id. Unresolvable
                # (content gone) -> log + skip (the rare legacy tag-only hold,
                # LOCKED run 36).
                rendered = hold.pinned.render()
                try:
                    digest = OciImage.resolve_id(rendered)
                except ImageNotFoundError:
                    _images_logger.warning(
                        "skipping hold for %r: legacy tag-pin %r is not "
                        "resolvable in the local store (content gone)",
                        hold.instance, rendered,
                    )
                    continue
            key = _bucket(digest)
            holds[key].append(hold)

        records: list[ImageRecord] = []
        for key in digests:
            records.append(cls(
                id=digests[key],
                refs=tuple(refs[key].values()),
                guests=tuple(sorted(guests[key])),
                holds=tuple(holds[key]),
            ))
        # Stable, deterministic order: by the rendered content id.
        return sorted(records, key=lambda r: r.id.render())

    @classmethod
    def get(cls, ref: "str | Digest | OciReference") -> Result["ImageRecord"]:
        """The ledger entry for ONE managed image (§12.4).

        Public Result boundary (Result-propagation sweep, Block S2): accepts all
        three input forms (LOCKED run 36): a ``str`` (a tag, a rendered digest, or
        a bare content-id hex), a ``Digest``, or an ``OciReference``. Resolves the
        input to a content id, then returns the matching record from :meth:`_list`
        (so ``get`` reports the SAME composed guest/hold/ref view ``list`` does,
        never a partial one).

        A miss — the input does not name a kento-MANAGED image (no record in
        :meth:`_list` is keyed on that id) — is a predictable ``Error``
        (``IMAGE_NOT_FOUND``), never a fabricated empty record. ``_coerce_to_id``
        may also raise ``ImageNotFoundError`` (input ref not in the local store);
        both are caught at this boundary. Note this is distinct from
        ``image.record()``, which is TOTAL (an unmanaged image still has an empty
        record): ``get`` reports only the *managed* set, so a miss is a not-found,
        matching ``Instance.get`` / ``OciImage.get``.

        Calls the raising ``_coerce_to_id`` and ``_list`` (not the public
        Result-returning ``list``) so a deep ``ImageNotFoundError`` reaches THIS
        boundary with its real kind, not ``INTERNAL`` (the KIND-FIDELITY rule).
        """
        try:
            target = cls._coerce_to_id(ref)
            rendered = target.render()
            for record in cls._list():
                if record.id.render() == rendered:
                    return Ok(value=record)
            raise ImageNotFoundError(
                f"no kento-managed image record for: {ref}"
                " — it is neither referenced by a guest nor pinned by a hold."
                "  Run 'kento images' to see managed images."
            )
        except KentoError as exc:
            return _error_from(exc)

    @classmethod
    def _for_id(cls, digest: Digest) -> "ImageRecord":
        """Build the TOTAL record for a content ``id`` (``image.record()`` core).

        Internal helper — STAYS RAISING (it backs the still-raising
        ``Image.record()``, an S3 method). Scans the managed dataset
        (:meth:`_list`, the raising form, NOT the public Result ``list``) for the
        record keyed on ``digest`` and returns it; if the image is NOT managed (no
        guest ref, no hold) returns an EMPTY record (``refs``/``guests``/``holds``
        empty) rather than ``None`` — ``image.record()`` is TOTAL (§12.4). The
        image's own resolved id is the key; ``refs`` is derived from the managed
        markers only (an unmanaged image has seen no kento tag), so an empty
        record has ``refs=()`` and is therefore ``dangling`` from kento's ledger
        POV — which is correct: kento holds no marker for it.
        """
        rendered = digest.render()
        for record in cls._list():
            if record.id.render() == rendered:
                return record
        return cls(id=digest)

    @staticmethod
    def _coerce_to_id(ref: "str | Digest | OciReference") -> Digest:
        """Resolve a ``str | Digest | OciReference`` input to a content ``Digest``.

        * a ``Digest`` is the id already — returned as-is (no podman call);
        * an ``OciReference`` is resolved to its content id via
          ``OciImage.resolve_id`` (raises ``ImageNotFoundError`` if not local);
        * a ``str`` is interpreted faithfully: a bare 64-hex content id or a
          rendered ``algorithm:encoded`` digest becomes a ``Digest`` directly
          (no podman call needed — it IS the id); any other string is a
          reference, parsed via ``OciReference.parse`` then resolved.

        Never hands an unvalidated string to the shell (§2 principle 3): a string
        is parsed/typed before any podman call.
        """
        if isinstance(ref, Digest):
            return ref
        if isinstance(ref, OciReference):
            return OciImage.resolve_id(ref.render())
        # A str: a bare content id or a rendered digest is the id itself; any
        # other string is a reference to resolve.
        if _RE_BARE_SHA256.match(ref):
            return _digest_from_podman_id(ref)
        if ":" in ref:
            # Could be a rendered digest (algorithm:encoded) OR a repo:tag. A
            # digest's encoded part is hex of the algorithm's fixed length; try
            # Digest.parse first and fall back to a reference on failure.
            try:
                return Digest.parse(ref)
            except KentoError:
                pass
        return OciImage.resolve_id(OciReference.parse(ref).unwrap().render())


# --------------------------------------------------------------------------- #
# Image — the abstract resolved-artifact base (§4.1).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, kw_only=True)
class Image(ABC):
    """A (possibly writable) directory-tree VIEW OF A DATA SOURCE (§4.1).

    Generalized run 36: an ``Image`` is no longer "a resolved read-only base"
    but a directory-tree view that MAY be read-only (OCI, content-addressed) OR
    writable (a volume / dir-backed view). Identity is the ``source``
    universally; a content ``Digest`` is the content-addressed *addition* a
    member carries (see ``OciImage.id``), NOT a base field — a writable image's
    identity is its location, not a digest, so a mandatory ``id`` on the base
    would PRECLUDE writable images (§4.1).

    Frozen, inert value fields (§2 principle 2 — a property never performs I/O):

    * ``source`` — the ``SourceReference`` locator this resolved from (narrowed
      per subclass, e.g. to ``OciReference`` on ``OciImage``).
    * ``kernel`` / ``initramfs`` — OPTIONAL off-image kernel override for
      Linux/VM direct-kernel-boot, on ANY image (§4.3). ``None`` means use the
      in-image ``/boot`` or the gemet default; ignored for LXC and the future
      firmware/non-Linux path.

    **Capability vs policy (§4.1).** ``is_writable()`` is the *capability* — can
    the underlying data source be written? — determined by the data source, NOT
    readable off the representation class for all members (``OciImage`` →
    ``False``; a future volume/dir view → per-source). It is distinct from the
    mount *policy* (read-only vs read-write), which is the instance's call via
    ``instance.storage: StorageMode`` (§8) — there is deliberately NO mount-mode
    field here.

    The **runtime lifecycle** is the abstract contract every concrete image
    implements (§4.4) — two primitives plus their inverses, the named moments
    an image is made usable and then torn down:

    * ``prepare`` — materialize the base (assemble the overlay / set up a
      backing chain / create the writable upper). **No host mount.**
    * ``mount`` → host dir — host-side mount; **skipped** when the backend takes
      a block device directly (VM + ``VolumeImage``).
    * ``unmount`` / ``release`` — the inverses, run in reverse order on
      teardown.

    They — together with ``is_writable`` — are declared ``abstractmethod`` here
    so ``Image`` is genuinely abstract (cannot be instantiated) and so a concrete
    subclass that forgets one fails loudly at definition rather than silently
    no-op'ing (gate C). ``OciImage`` implements all of them; ``VolumeImage`` /
    ``CompositeImage`` are documented stubs that deliberately leave them
    unimplemented and so stay abstract/uninstantiable until they are built (§4.5).

    The lifecycle methods perform I/O (principle 2) and are kept **total
    functions of their explicit arguments** plus the image's own resolved value:
    a frozen value cannot carry mutable mount state, so the host directory and
    the writable state directory are passed in by the caller (kento's
    create/start routine, which owns that on-disk layout) rather than stashed on
    the image.
    """

    source: SourceReference
    kernel: Path | None = None
    initramfs: Path | None = None

    # ----- capability contract (§4.1) — abstract on the base -----
    @abstractmethod
    def is_writable(self) -> bool:
        """Can the underlying data source be written? (§4.1).

        The image CAPABILITY (data-source-determined), NOT the mount policy
        (read-only vs read-write), which is the instance's call via
        ``instance.storage: StorageMode`` (§8). ``OciImage`` → ``False`` (OCI
        store layers are read-only by spec); a future volume / dir-backed view
        answers per its own source.

        Abstract here so the family base does not assume read-only-ness (the
        whole point of the run-36 generalization) and a concrete member that
        forgets to declare its writability fails loudly (gate C).
        """

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

        Run AFTER ``unmount`` on teardown (reverse order). For an ``OciImage``
        the materialized state is the writable upper/work tree under
        ``state_dir``; releasing it is the caller's storage policy (it may
        persist across restarts), so the base contract does not destroy it.
        """

    # ----- ledger navigation (§12.4) — lazy method, TOTAL -----
    def record(self) -> "ImageRecord":
        """The kento LEDGER entry about THIS image — the inverse of
        :meth:`ImageRecord.resolve` (§12.4).

        Builds the typed projection of kento's own markers (guest ``kento-image``
        references + ``kento-hold.<guest>`` holds) for this image's content
        ``id``. It is the inverse navigation of ``ImageRecord.resolve()`` (which
        goes record → artifact); this goes artifact → record.

        **TOTAL** — an image that no guest references and no hold pins still has
        a record (``guests=()``, ``holds=()``); it NEVER returns ``None`` (§12.4).
        Such an image is simply not in the *managed* dataset
        ``ImageRecord.list`` reports, but its record is still a well-defined,
        empty-marker value (``refs`` derived from the image's own resolved
        identity, never the markers).

        **Lazy METHOD, not a stored field** (§12.4): performs I/O (it scans the
        kento markers, §2 principle 2), so it is an explicit named call, not a
        property — and storing it on a frozen ``Image`` would break Image's
        one-source purity and create a frozen circular reference. The mutual
        ``ImageRecord`` import is LOCAL to this method (the existing
        family-cycle pattern; ``ImageRecord`` is defined below in this module).

        The base implementation needs only the image's resolved content ``id``;
        a member whose identity is NOT a content ``Digest`` (a future writable
        image) cannot have a content-keyed ledger entry, so the base raises a
        typed ``StateError`` and a content-addressed member (``OciImage``)
        supplies its ``id`` via :meth:`_record_id`. (No abstract churn: a single
        overridable hook, defaulting to "no content id".)
        """
        return ImageRecord._for_id(self._record_id())

    def _record_id(self) -> Digest:
        """The content ``Digest`` this image's ledger entry is keyed on (§12.4).

        Default: an ``Image`` with no content-addressed identity (a future
        writable image) cannot key a content ledger entry — a typed
        ``StateError`` rather than a fabricated value (principle 5, gate C).
        ``OciImage`` overrides this with its resolved ``id``.
        """
        raise StateError(
            f"{type(self).__name__} has no content id; only content-addressed "
            "images (OciImage) have a kento image-record"
        )


# --------------------------------------------------------------------------- #
# LayeredImage — the abstract "layering" node (§4.1, AMENDED run 36).
#
# A union of read-only directory layers (overlayfs). ABSTRACT and
# source-agnostic: it carries NO concrete fields or methods. The concrete layer
# shape, identity, and lifecycle are decided per subclass — for 1.0 the only
# concrete member is ``OciImage`` (below). The shared dir-layer abstraction (a
# common layer model usable by both OCI and a future ``LocalDirectoryImage``) is
# deliberately NOT worked out here; it is settled only when ``LocalDirectoryImage``
# lands (a future block), so that the abstraction is designed against two real
# members rather than guessed from one. Leaving it a pure marker now avoids
# baking OCI-shaped assumptions into the shared parent (gate C).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, kw_only=True)
class LayeredImage(Image, ABC):
    """The "layering": a UNION of read-only directory layers (overlayfs) (§4.1).

    **ABSTRACT — uninstantiable.** Source-agnostic; it adds nothing concrete over
    ``Image`` (no fields, no methods). It still inherits ``Image``'s abstract
    contract (``is_writable`` + ``prepare`` / ``mount`` / ``unmount`` /
    ``release``), so it remains abstract and cannot be constructed. The concrete
    layer shape + identity are decided per subclass.

    Judgment call (disclosed): kept a **pure minimal marker** — no shared
    dir-layer fields/methods yet. That shared abstraction (what OCI and a future
    ``LocalDirectoryImage`` have in common) is worked out only when the second
    member lands, so it is designed against two real cases rather than guessed
    from one. For 1.0 the sole concrete member is ``OciImage``.
    """


# --------------------------------------------------------------------------- #
# OciImage — the 1.0, OCI/podman-backed overlayfs representation (§4.1, §4.4).
#
# The only built ``LayeredImage`` for 1.0; holds ALL the OCI/podman specifics
# (``source: OciReference``, the content ``id``, ``layers``, ``overlay_root``)
# and the full content + runtime lifecycle. Every lifecycle method is an ADDITIVE
# wrapper over the existing procedural functions (kento.layers / kento.vm) — it
# delegates, it does not fork their logic.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, kw_only=True)
class OciImage(LayeredImage):
    """The podman/OCI-store ``LayeredImage`` — the ONLY built member for 1.0.

    Holds everything OCI/podman-specific (§4.1, AMENDED run 36 — demoted here off
    the base + the abstract ``LayeredImage``):

    * ``source`` is narrowed to an ``OciReference``.
    * ``id`` — the content **identity**, a ``Digest`` (content-addressed; §4.3).
      This is the *resolved* identity, **not** the locator's OCI-only ``digest``
      pin — do not conflate the two (§4.3 last bullet). It lives HERE (not on the
      base) because identity is content-addressed only for content-addressed
      members; a writable image's identity is its location.
    * ``layers`` are the ordered lowerdirs (top→bottom; each ``Layer.id`` is the
      podman ``<id>`` in ``<overlay_root>/<id>/diff``, §4.1) and ``overlay_root``
      is the shared podman-store base — the directory a mount ``chdir``s into so
      the short ``l/<short>`` lowerdir entries resolve. Both are MANDATORY (§4.1
      verbatim — no defaults): an ``OciImage`` is by definition a populated union,
      and a layer-less / rootless one would be a degenerate value whose mount
      renders ``""`` (gate C). Construct via :meth:`resolve`, or supply both
      fields explicitly.

    ``is_writable()`` is ``False``: OCI store layers are read-only by spec.

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
    id: Digest
    layers: tuple[Layer, ...]
    overlay_root: Path

    def is_writable(self) -> bool:
        """OCI store layers are read-only by spec → always ``False`` (§4.1)."""
        return False

    def _record_id(self) -> Digest:
        """This OCI image's resolved content ``id`` keys its ledger entry (§12.4).

        Overrides the base "no content id" hook with the content-addressed
        ``Digest`` this handle already carries, so ``image.record()`` builds a
        ledger entry keyed on the same content id ``ImageRecord.list`` groups by.
        """
        return self.id

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
    # OciImage. This is the boundary where a string ref becomes a value.
    # ------------------------------------------------------------------- #
    @classmethod
    def resolve(cls, source: OciReference) -> Result["OciImage"]:
        """Resolve an ``OciReference`` to a populated ``OciImage`` (§4.3).

        Public Result boundary (Result-propagation sweep, Block S2): the body is
        the raising :meth:`_resolve`; an internal ``KentoError`` (most often
        ``ImageNotFoundError`` from ``resolve_layers``/``resolve_id``, or a
        ``StateError`` for an unexpected store layout) is caught here and
        converted to an ``Error`` via ``_error_from``. A non-``KentoError`` is a
        panic and propagates.

        SPLIT from ``_resolve`` (the P1 ``parse``/``_parse`` precedent) because
        the public ``pull``/``get``/``list`` AND the still-raising cross-module
        ``Instance.image()`` (an S3 method) all call the resolver internally — the
        raising form lets a deep ``ImageNotFoundError`` reach THEIR public
        boundary with its real kind rather than collapsing to ``INTERNAL`` (the
        KIND-FIDELITY rule).
        """
        try:
            return Ok(value=cls._resolve(source))
        except KentoError as exc:
            return _error_from(exc)

    @classmethod
    def _resolve(cls, source: OciReference) -> "OciImage":
        """The raising body of :meth:`resolve` (§4.3).

        Internal: the public :meth:`resolve` boundary catches its ``KentoError``
        and converts to an ``Error``. Still-raising internal callers (``pull`` /
        ``get`` / ``list`` inside their own boundaries; the cross-module
        ``Instance.image()``) use THIS form so a raised kind reaches their own
        public boundary intact (the KIND-FIDELITY rule).

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
    # OciImages (the 1.0 OCI-store representation, §4.4).
    #
    # PLACEMENT (disclosed): §11.5 phrases these `Image.pull -> Self`, but for
    # 1.0 they are OCI-store ops that produce/manage OciImages (§4.4), and
    # `Image`/`LayeredImage` are genuine ABCs (abstract is_writable +
    # prepare/mount/...) that cannot be instantiated — so a classmethod resolving
    # to `cls(...)` only works on the concrete OciImage. They live here as
    # OciImage classmethods (`pull`/`get`/`list`) + instance `remove`; `Self` is
    # satisfied (cls IS OciImage). A future base-`Image` dispatch (when
    # VolumeImage fetch lands) is purely additive / non-breaking.
    # ------------------------------------------------------------------- #
    @classmethod
    def pull(cls, ref: "str | OciReference") -> Result["OciImage"]:
        """Acquire an OCI image from a registry into the local store (M19).

        Public Result boundary (Result-propagation sweep, Block S2): ``podman
        pull <ref>``, then resolve the now-local image to a populated handle via
        the raising ``_resolve``. Accepts a ``str`` or an ``OciReference``: a
        ``str`` is parsed through ``OciReference.parse`` (§2 principle 3 — we
        never hand an unvalidated string to the shell). The predictable failures
        — a malformed ref (``MalformedReference`` BEFORE any podman call, via
        ``_coerce_ref``), a pull failure (``SubprocessError``), or the image still
        absent after the pull (``ImageNotFoundError`` from ``_resolve``) — are all
        caught at THIS boundary and converted to an ``Error`` with the right kind.
        No ``force`` (§11.5 M19: podman re-pulls moved tags / no-ops identical
        digests). A non-``KentoError`` is a panic and propagates.

        Calls the raising ``_coerce_ref`` and ``_resolve`` (not the public
        Result-returning ``resolve``) so a deep ``ImageNotFoundError`` /
        ``SubprocessError`` reaches THIS boundary with its real kind, not
        ``INTERNAL`` (the KIND-FIDELITY rule).
        """
        from kento import layers as _layers
        from kento.subprocess_util import run_or_die

        try:
            oci = cls._coerce_ref(ref)
            rendered = oci.render()
            run_or_die(
                [*_layers._podman_cmd(), "pull", rendered],
                "pull image",
                name=rendered,
            )
            return Ok(value=cls._resolve(oci))
        except KentoError as exc:
            return _error_from(exc)

    @classmethod
    def get(cls, ref: "str | OciReference") -> Result["OciImage"]:
        """Resolve an ALREADY-LOCAL image to a handle; no network (M20).

        Public Result boundary (Result-propagation sweep, Block S2): read-only —
        the raising ``_resolve`` queries the local podman store only (no
        ``pull``). When the image is absent ``layers.resolve_layers`` raises
        ``ImageNotFoundError``; ``get`` catches it at this boundary and returns
        ``Error(IMAGE_NOT_FOUND)`` (mirrors ``Instance.get``); it does NOT
        fabricate a handle for a missing image (gate C). A ``str`` ref is
        parsed/validated as in :meth:`pull` (a ``MalformedReference`` is likewise
        caught here).

        SPLIT from ``_get`` (the P1 precedent) because the still-raising
        ``ImageRecord.resolve`` body calls the raising form so a deep
        ``ImageNotFoundError`` reaches ITS boundary with the real kind rather than
        collapsing to ``INTERNAL`` (the KIND-FIDELITY rule).
        """
        try:
            return Ok(value=cls._get(ref))
        except KentoError as exc:
            return _error_from(exc)

    @classmethod
    def _get(cls, ref: "str | OciReference") -> "OciImage":
        """The raising body of :meth:`get` (M20).

        Internal: the public :meth:`get` boundary catches its ``KentoError`` and
        converts to an ``Error``. Still-raising internal callers
        (``ImageRecord.resolve`` inside its own boundary) use THIS form so a
        raised kind reaches their public boundary intact (KIND-FIDELITY rule).
        """
        return cls._resolve(cls._coerce_ref(ref))

    @classmethod
    def list(cls) -> Result["list[OciImage]"]:
        """Enumerate local OCI images as resolved handles (M21).

        Public Result boundary (Result-propagation sweep, Block S2): queries
        ``podman images`` for repository references directly (the whole-store
        podman view — distinct from ``ImageRecord.list``, which is kento's MANAGED
        ledger, the guest/hold subset), then resolves each to an ``OciImage`` via
        the raising ``_resolve``.

        TOTAL OVER THE STORE (disclosed policy, grounded in §2 + §7.2's
        ``Status.UNKNOWN`` totality rationale): a single image that fails to
        resolve mid-enumeration — e.g. a tag that raced a removal, or an
        unexpected store layout for one entry — is SKIPPED WITH A LOG, not
        raised, so one bad image cannot blow up enumeration of every other. A
        hard failure of the enumerating query itself (``podman images``) is a
        different thing and becomes an ``Error(SUBPROCESS_FAILED)`` — that is the
        whole listing failing, not one entry. NO provenance flag in 1.0 (§11.5
        M21 — that lands with the lifecycle EPIC).
        """
        from kento import layers as _layers
        from kento.subprocess_util import run_or_die

        try:
            result = run_or_die(
                [*_layers._podman_cmd(), "images",
                 "--format", "{{.Repository}}:{{.Tag}}"],
                "list images",
            )
            images: list[OciImage] = []
            for line in result.stdout.splitlines():
                entry = line.strip()
                # podman renders a dangling (untagged) image as "<none>:<none>";
                # such an entry has no resolvable repository ref, so skip it (it
                # is surfaced by the lifecycle-EPIC provenance work, not 1.0).
                if not entry or "<none>" in entry:
                    continue
                try:
                    # .unwrap() preserves the skip-on-malformed behavior: an
                    # Error → ResultError (a KentoError), caught below (Block P1).
                    oci = OciReference.parse(entry).unwrap()
                    images.append(cls._resolve(oci))
                except KentoError as exc:
                    # Total over the store: one unresolvable entry is logged and
                    # skipped, never fatal to the whole enumeration. (The
                    # whole-listing failure — the `podman images` query itself —
                    # is the OUTER boundary's SubprocessError, not this skip.)
                    _images_logger.warning(
                        "skipping unresolvable image %r: %s", entry, exc)
            return Ok(value=images)
        except KentoError as exc:
            return _error_from(exc)

    @classmethod
    def prune(
        cls, *, scope: PruneScope = PruneScope.DANGLING,
    ) -> Result[ReclaimReport]:
        """Reclaim DANGLING images; **never touches a held image** (M22).

        Public Result boundary (Result-propagation sweep, Block S2): removes
        unused/dangling images — untagged ``<none>`` layers podman no longer
        references — and returns ``Ok`` of a :class:`ReclaimReport` of what was
        removed (``reclaimed``) and what podman refused (``failed`` = ``(id,
        reason)`` pairs, surfaced not swallowed — the 1.6.2 contract). This is
        a store-level GC, so it is a ``classmethod`` mirroring ``pull``/``get``/
        ``list`` (it manages the store, not one handle); it is **distinct** from
        the kento orphan-HOLD GC (``images.prune`` / future
        ``Instance.prune_orphans``).

        The locked M22 signature carries ONE param, ``scope: PruneScope =
        PruneScope.DANGLING`` (§11.5). ``DANGLING`` is the only 1.0 value (the
        further provenance scopes land with the lifecycle EPIC, §11.9). Any
        other ``scope`` value cannot occur in 1.0, but yields an
        ``Error(VALIDATION)`` (principle 5 — never a silent no-op or a fabricated
        result) so a future caller passing an unimplemented scope fails loudly
        instead of silently pruning DANGLING anyway (gate C).

        There is **NO** ``dry_run`` param: the locked signature has only
        ``scope``, so ``prune`` EXECUTES and the report is ``dry_run=False``.

        **Two distinct failure channels (preserved).** A per-image ``rmi``
        refusal is surfaced INSIDE the report's ``failed`` (the 1.6.2 contract) —
        it does NOT make the whole op an ``Error``; the batch continues and the
        ``Ok`` report carries the refusals. By contrast a failure of the dangling
        ENUMERATION query itself is the whole prune failing → caught at this
        boundary as ``Error(SUBPROCESS_FAILED)``.

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
        """
        from kento import images as _images_mod
        from kento import layers as _layers
        from kento.errors import SubprocessError
        from kento.subprocess_util import run_or_die

        try:
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
            # apples-to-apples (bare-hex vs bare-hex) against a hold's pinned
            # content id below). A dangling image is untagged ("<none>"), so its
            # id IS its identifier in the report.
            result = run_or_die(
                [*_layers._podman_cmd(), "images",
                 "--filter", "dangling=true", "--format", "{{.Id}}"],
                "list dangling images",
            )

            # Held content ids — guarantee the "never touches a held image"
            # invariant ourselves rather than trusting podman's filter. Compose
            # the existing hold knowledge (no forked hold logic): the
            # authoritative io.kento.hold-image-id label values, plus _holds()'s
            # {{.Image}} field (the pinned content id for a modern hold; a
            # repo:tag for a legacy hold, which can never equal a dangling
            # image's id and so is inert).
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
                    # Belt-and-suspenders: a held image must never be removed
                    # even if it somehow surfaced as dangling (spec invariant).
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
                    # Surface the refusal (1.6.2 contract) — do NOT swallow it,
                    # and do NOT abort the batch on one refusal. This stays a
                    # per-image entry in the Ok report's `failed`, NOT an Error.
                    failed.append((image_id, str(exc)))

            return Ok(value=ReclaimReport(
                dry_run=False,
                reclaimed=tuple(reclaimed),
                failed=tuple(failed),
            ))
        except KentoError as exc:
            return _error_from(exc)

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
        return OciReference.parse(ref).unwrap()

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


# --------------------------------------------------------------------------- #
# LocalDirectoryImage — the first NON-OCI LayeredImage: a fetched-and-extracted
# rootfs directory (URL-VM Phase B, OPTION 2). NOT in the podman store; the base
# is a single extracted tree that becomes the overlay's one lowerdir (§4.1).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, kw_only=True)
class LocalDirectoryImage(LayeredImage):
    """A fetched-and-extracted rootfs DIRECTORY tree — the first non-OCI image (§4.1).

    The URL-VM (OPTION 2) representation: a ``.txz`` rootfs streamed from an
    ``https://`` URL and extracted into a local directory that becomes the VM's
    SINGLE overlay ``lowerdir``. Unlike ``OciImage`` it is NOT in the podman store
    and is NOT content-addressed:

    * ``source`` is narrowed to a ``UrlReference`` (mirrors how ``OciImage``
      narrows ``source`` to ``OciReference``).
    * There is deliberately **NO** ``id: Digest`` — identity is the URL location,
      not a content digest (a URL rootfs is fetched fresh, not addressed by
      content; §4.1: a content ``Digest`` is the content-addressed member's
      addition, not a base field). It therefore has no kento image-record either
      (the base ``_record_id`` raises ``StateError`` — inherited unchanged).
    * ``kernel`` / ``initramfs`` are inherited from the base (unset here);
      ``Instance.image()`` populates them from the ``kento-kernel`` /
      ``kento-initramfs`` markers via ``dataclasses.replace``, exactly as for OCI.
    * It carries NONE of the OCI-store fields (``id`` / ``layers`` /
      ``overlay_root``) — those are podman-store specifics.

    ``is_writable()`` is ``False``: the extracted base is a read-only lowerdir;
    the writable layer is the overlay ``upper``, same as ``OciImage`` (§4.1).

    The runtime lifecycle mirrors ``OciImage``'s four primitives, but the base is
    a single extracted directory rather than a podman layer union:

    * ``prepare`` — the heavy I/O: fetch + extract the ``.txz`` into
      ``state_dir/rootfs-base`` (via the shared ``urlvm`` helper), then create the
      overlay ``upper``/``work`` dirs.
    * ``mount`` — overlay-mount with ``rootfs-base`` as the SINGLE lowerdir.
    * ``unmount`` — the inverse (identical to ``OciImage``).
    * ``release`` — REMOVE the extracted ``rootfs-base`` (and the ``.txz`` if it
      lingered): the ephemeral discard, where this DIFFERS from ``OciImage``'s
      no-op release (a URL rootfs is not persisted store content).
    """

    source: UrlReference

    def is_writable(self) -> bool:
        """The extracted base is a read-only lowerdir → always ``False`` (§4.1).

        Same as ``OciImage``: the writable layer is the overlay ``upper``, not the
        base tree. The capability is data-source-determined (§4.1) — an extracted
        rootfs directory is treated as a read-only base.
        """
        return False

    @classmethod
    def resolve(cls, source: UrlReference) -> "LocalDirectoryImage":
        """Build the handle from a ``UrlReference`` — CHEAP, no I/O (§4.3, §4.5).

        Unlike ``OciImage.resolve`` (which queries the podman store), there is
        nothing to snapshot here at resolve time: the URL is not fetched (fetching
        needs a per-instance ``state_dir`` for the ``.txz`` + extraction target —
        that is :meth:`prepare`'s job). So ``resolve`` just constructs the frozen
        value. It is TOTAL and pure, hence a plain value (no ``Result``): a URL is
        already a validated ``UrlReference`` and no store lookup can fail.
        """
        return cls(source=source)

    def prepare(self, state_dir: Path) -> None:
        """Fetch + extract the rootfs, then create ``upper``/``work`` (§4.4).

        The heavy I/O: streams the ``.txz`` from ``self.source`` and extracts it
        into ``state_dir/rootfs-base`` via the shared ``urlvm.fetch_and_extract_
        rootfs`` helper (the SAME composition B3b's procedural create path uses —
        no forked logic), then mkdirs the overlay ``upper``/``work`` dirs (mirrors
        ``OciImage.prepare``).

        **The ``.unwrap()`` seam (DISCLOSED).** The helper is ``Result``-native,
        but the base ``Image.prepare`` contract returns ``None`` (and raises a
        typed error on failure — the four-primitive ABC signature every image
        implements). So this primitive ``.unwrap()``s the helper's ``Result``: a
        PREDICTABLE fetch/extract failure (an ``Error``) surfaces HERE as a raised
        ``ResultError`` (a ``KentoError``), matching the ABC's raising signature.
        The ``Result``-native seam is the shared ``urlvm`` helper itself — a
        library caller wanting a ``Result`` from the fetch/extract composition
        calls ``fetch_and_extract_rootfs`` directly; the primitive matches the base
        contract. (Making the base ``Image.prepare`` return a ``Result`` would be a
        family-wide ABC refactor — out of scope for this block.) A fragment-drop
        ``Warning`` unwraps to its value cleanly (``Warning.unwrap`` returns the
        value), so a benign warning does not abort ``prepare``.
        """
        from kento.urlvm import fetch_and_extract_rootfs

        fetch_and_extract_rootfs(
            self.source,
            state_dir / "rootfs.txz",
            state_dir / "rootfs-base",
        ).unwrap()
        (state_dir / "upper").mkdir(parents=True, exist_ok=True)
        (state_dir / "work").mkdir(parents=True, exist_ok=True)

    def mount(self, host_dir: Path, state_dir: Path) -> None:
        """Overlay-mount the extracted base at ``host_dir/rootfs`` (§4.4).

        The extracted ``state_dir/rootfs-base`` is the SINGLE overlay lowerdir.
        ``vm.mount_rootfs`` re-derives the lowerdir form from the absolute string
        via ``to_overlay_lowerdir``, which gracefully degrades for a single
        non-store directory (it does not match the podman ``<root>/<id>/diff``
        shape → returns ``("", <absolute dir>)`` and mounts it as an absolute
        single lowerdir). So we hand it the absolute ``rootfs-base`` path. Mirrors
        ``OciImage.mount`` (same delegation, same host overlay-mount logic).
        """
        from kento import vm as _vm

        _vm.mount_rootfs(host_dir, str(state_dir / "rootfs-base"), state_dir)

    def unmount(self, host_dir: Path) -> None:
        """Inverse of ``mount`` — unmount ``host_dir/rootfs`` (§4.4).

        Delegates to ``vm.unmount_rootfs`` (identical to ``OciImage.unmount``).
        """
        from kento import vm as _vm

        _vm.unmount_rootfs(host_dir)

    def release(self, state_dir: Path) -> None:
        """Inverse of ``prepare`` — DISCARD the extracted base (§4.4).

        Where this DIFFERS from ``OciImage.release`` (a no-op that keeps the
        persisted store content): a URL rootfs is NOT store content, so the
        extracted ``state_dir/rootfs-base`` tree is ephemeral and is removed here,
        along with the intermediate ``.txz`` if it ever lingered (it is normally
        already deleted by the ``urlvm`` helper after a successful extract). This
        is the ephemeral no-store discard.

        A disk error while removing is an environmental fault → it PROPAGATES
        (panic): ``shutil.rmtree`` runs with ``ignore_errors=False`` (its default)
        so an ``OSError`` is not swallowed, consistent with the leaves' /
        primitives' disk-``OSError`` boundary. ``missing_ok`` on the ``.txz`` /
        the ``rootfs-base`` existence check keep a re-run idempotent (a
        double-release does not fail on already-gone state).
        """
        import shutil

        base = state_dir / "rootfs-base"
        if base.exists():
            shutil.rmtree(base, ignore_errors=False)
        (state_dir / "rootfs.txz").unlink(missing_ok=True)


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
    block representation non-breakingly (principle 7), but **no capability or
    runtime lifecycle is implemented**: ``VolumeImage`` therefore inherits
    ``Image``'s abstract ``is_writable`` + ``prepare``/``mount``/``unmount``/
    ``release`` unimplemented and is consequently **abstract / uninstantiable**
    until the feature lands. Leaving them inherited-abstract (rather than adding
    a stub-raise body) is the minimum that keeps the stub uninstantiable while
    declaring nothing it would have to re-decide when built. Do not construct it;
    do not wire it into resolution.

    Fields (§4.1):

    * ``path`` — the resolved base partition image.
    * ``format`` — ``DiskFormat`` (RAW | QCOW2).
    * ``backing`` — block backing chain (qcow2); the base may itself be a chain.
    * ``id`` — ``Digest | None``: a content ``Digest`` when read-only /
      content-addressed, ``None`` when writable (identity = location, not a
      digest). Declared here rather than on the base because identity is
      content-addressed only for content-addressed members (§4.1, run-36
      demotion); a ``VolumeImage`` may be either, hence the ``| None``.

    The per-instance writable UPPER is created at ``prepare`` and governed by
    ``StorageMode`` — it is instance/runtime state, NOT a field here (§4.3).
    The VM recipe for a ``VolumeImage`` is **prepare-only** → block device, no
    ``mount`` (§4.4 matrix); the ``Image`` lifecycle split (prepare vs mount)
    already accommodates that cell without rework when this is built.
    """

    path: Path
    format: DiskFormat
    backing: "VolumeImage | None"
    id: Digest | None


# --------------------------------------------------------------------------- #
# CompositeImage — DOCUMENTED STUB / PLAN ONLY (§4.1, §4.5).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, kw_only=True)
class CompositeImage(Image):
    """Images composited at mount points — **PLAN ONLY, not built up front**.

    **STUB — NOT BUILT (§4.5).** ``mounts`` maps a mountpoint to an ``Image``,
    e.g. ``{"/": OciImage, "/home": VolumeImage}`` — this is the image-lifecycle
    EPIC's mount facility / per-path persistence ("persistent ``/home`` over an
    ephemeral root"); the root mount (``"/"``) is the bootable one. Declared with
    its field per §4.1 so the ``Image`` ABC is shown to accommodate composition
    non-breakingly (principle 7), but **no capability or runtime lifecycle is
    implemented** — it inherits ``Image``'s abstract ``is_writable`` + lifecycle
    primitives unimplemented and is therefore **abstract / uninstantiable** until
    it is built. Do not construct it; do not wire it into resolution.
    """

    mounts: dict[str, Image]
