"""Root-storage strategy value type — ``StorageMode``.

A **pure, inert** value (spec §2 principle 2): a flat enum, no I/O. It is the
*root-storage strategy* axis — how an instance's writable root is materialized —
and becomes a create-time/immutable **field on the base ``Instance``**
(``storage: StorageMode``), persisted to the ``kento-storage`` state file. The
materialization itself (overlayfs vs qcow2 CoW vs virtio-blk) is the polymorphic
per-backend impl in a later block; this module ships only the inert enum.

The public surface (``StorageMode``) is re-exported flat from ``kento`` — refer
to ``kento.StorageMode``, not ``kento._storage.StorageMode``.

Spec: ``~/workspace/kento-core-api-design.md`` §8 (the StorageMode roadmap
bullet). Modeled as a flat enum, not a bool (PlatformMode / NetworkMode
precedent), so it is extensible without an API break: each value names a
coherent **substrate × lifecycle** strategy, and a new strategy is a new value,
never a restructure (§2 principle 7).
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "StorageMode",
]


# --------------------------------------------------------------------------- #
# StorageMode — substrate (fs vs block) × lifecycle (persistent vs ephemeral).
#
# ``str``-backed so the value IS the wire string written to ``kento-storage``,
# matching PlatformMode / NetworkMode / Status. "Overlay" is universal — the
# writable root is "an overlay over a ro base" in every value; the axes are the
# *substrate* (fs-overlay vs block-overlay) and the *lifecycle* (persistent vs
# ephemeral), which are coupled in practice, so a single flat enum beats two
# orthogonal bools (§8).
# --------------------------------------------------------------------------- #


class StorageMode(str, Enum):
    """How an instance's writable root is materialized (§8).

    * ``OVERLAY`` — the default. fs-overlay (overlayfs lowerdir + upper, over a
      ``LayeredImage``); the upper is **PERSISTENT** across restarts. The only
      mode kento ships today.
    * ``EPHEMERAL_IMAGE`` — block-overlay (qcow2 CoW / ``snapshot=on`` /
      dm-snapshot over a read-only base); **disposed on stop**. The impl is a
      FUTURE feature (kento is overlay/virtiofs-only today); this block ships the
      enum *value* only, so the 1.0 surface reserves the field.

    ``StorageMode`` (the writable strategy) is **orthogonal** to the ``Image``
    representation (§4): an ``OVERLAY`` ``LayeredImage`` and an
    ``EPHEMERAL_IMAGE`` flatten-to-disk are two writable strategies over the
    same resolved base. The enum grows by adding values (§2 principle 7) — e.g.
    a future ``PERSISTENT_IMAGE`` (block-overlay that survives restarts) is a new
    member, not a restructure.
    """

    OVERLAY = "overlay"               # default — fs-overlay; PERSISTENT upper
    EPHEMERAL_IMAGE = "ephemeral-image"  # block-overlay over ro base; disposed-on-stop
    # FUTURE: PERSISTENT_IMAGE = "persistent-image" — block-overlay that
    # survives restarts. A new value when its impl lands (§2 principle 7: grow
    # by adding values, no restructure) — deliberately a comment, not a member,
    # so the 1.0 public enum carries only what is real (§2 principle 7).
