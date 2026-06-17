"""kento images / kento prune — image listing and safe garbage collection.

Each kento guest pins its OCI image against `podman prune` via a stopped
hold container named ``kento-hold.<guestname>`` carrying the label
``io.kento.hold-for=<guestname>`` (see layers.py). These two bare-only
commands report on and reclaim those holds:

- ``kento images`` is read-only: it lists kento-managed images, marking
  which are in-use vs orphaned and whether a hold pins them.
- ``kento prune`` is destructive but conservative: it removes only
  *orphaned* hold containers (a ``kento-hold.<name>`` whose guest no
  longer exists) and then the images those holds freed (only if no
  surviving guest references them and no remaining hold pins them). It
  NEVER touches a hold whose guest still exists and NEVER runs
  ``podman prune -a``.
"""

import logging
import subprocess

from kento import LXC_BASE, VM_BASE
from kento.layers import _podman_cmd, remove_image_hold

logger = logging.getLogger("kento")

# Exact podman queries used by this module:
#   hold enumeration (name + pinned image, tab-separated, one per line):
#       podman ps -a --filter label=io.kento.hold-for \
#           --format {{.Label "io.kento.hold-for"}}\t{{.Image}}
#   hold removal:       podman rm kento-hold.<name>   (via remove_image_hold)
#   image removal:      podman image rm <image>
# No exists check is needed: the guest-name set comes from the on-disk
# kento-image files, and orphan-ness is computed against that set.


def _guest_image_refs() -> dict[str, list[str]]:
    """Map each referenced OCI image -> sorted list of guest names.

    Iterates LXC_BASE + VM_BASE ``*/kento-image`` (same approach as
    list.list_containers); guest name comes from ``kento-name`` falling
    back to the directory name.
    """
    refs: dict[str, set[str]] = {}
    for base in (LXC_BASE, VM_BASE):
        if not base.is_dir():
            continue
        for image_file in base.glob("*/kento-image"):
            container_dir = image_file.parent
            image = image_file.read_text().strip()
            if not image:
                continue
            name_file = container_dir / "kento-name"
            name = (name_file.read_text().strip()
                    if name_file.is_file() else container_dir.name)
            refs.setdefault(image, set()).add(name)
    return {img: sorted(names) for img, names in refs.items()}


def _guest_names() -> set[str]:
    """Set of all kento guest names across both bases (kento-name/dir)."""
    names: set[str] = set()
    for base in (LXC_BASE, VM_BASE):
        if not base.is_dir():
            continue
        for image_file in base.glob("*/kento-image"):
            container_dir = image_file.parent
            name_file = container_dir / "kento-name"
            names.add(name_file.read_text().strip()
                      if name_file.is_file() else container_dir.name)
    return names


def _holds() -> list[tuple[str, str]]:
    """Return [(held_for_name, image), ...] for every kento hold container.

    Single podman query: the format string emits the hold-for label and
    the pinned image, tab-separated.
    """
    result = subprocess.run(
        [*_podman_cmd(), "ps", "-a",
         "--filter", "label=io.kento.hold-for",
         "--format", '{{.Label "io.kento.hold-for"}}\t{{.Image}}'],
        capture_output=True, text=True,
    )
    holds: list[tuple[str, str]] = []
    if result.returncode != 0:
        return holds
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        held_for = parts[0].strip()
        image = parts[1].strip() if len(parts) > 1 else ""
        if held_for:
            holds.append((held_for, image))
    return holds


def _hold_image_ids() -> dict[str, str]:
    """Map each held-for guest name -> the hold's pinned content-ID label.

    Separate query from ``_holds()`` (whose (name, image) shape prune/list
    depend on) so those callers are untouched. Only entries carrying a
    non-empty ``io.kento.hold-image-id`` label are returned; legacy holds
    created before the label existed are simply absent from the map.
    """
    result = subprocess.run(
        [*_podman_cmd(), "ps", "-a",
         "--filter", "label=io.kento.hold-for",
         "--format",
         '{{.Label "io.kento.hold-for"}}\t{{.Label "io.kento.hold-image-id"}}'],
        capture_output=True, text=True,
    )
    ids: dict[str, str] = {}
    if result.returncode != 0:
        return ids
    for line in result.stdout.splitlines():
        line = line.rstrip("\n")
        if not line.strip():
            continue
        parts = line.split("\t")
        held_for = parts[0].strip()
        image_id = parts[1].strip() if len(parts) > 1 else ""
        if held_for and image_id and image_id != "<no value>":
            ids[held_for] = image_id
    return ids


def list_images(in_use_only: bool = False) -> str:
    """List kento-managed images (read-only).

    A managed image is any image referenced by a guest or pinned by a
    hold container. Each row reports the image, the referencing guests,
    whether a hold pins it, and an in-use/orphaned status.

    Returns the rendered table as a string (no trailing newline).
    """
    refs = _guest_image_refs()
    holds = _holds()

    held_images: set[str] = {img for _, img in holds if img}

    managed = set(refs) | held_images
    if not managed:
        return "No kento-managed images."

    rows = []
    for image in sorted(managed):
        guests = refs.get(image, [])
        status = "in-use" if guests else "orphaned"
        if in_use_only and status != "in-use":
            continue
        guests_cell = ",".join(guests) if guests else "-"
        hold_cell = "yes" if image in held_images else "no"
        rows.append((image, guests_cell, hold_cell, status))

    if not rows:
        return "No kento-managed images."

    headers = ("IMAGE", "GUESTS", "HOLD", "STATUS")
    widths = []
    for i, header in enumerate(headers):
        col_max = max((len(row[i]) for row in rows), default=0)
        widths.append(max(len(header), col_max))

    lines = []
    lines.append("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append("  ".join(val.ljust(w) for val, w in zip(row, widths)))
    return "\n".join(lines)


def prune(yes: bool = False) -> tuple[str, int]:
    """Safe GC of orphaned kento hold containers and the images they freed.

    DRY-RUN by default. Removes only holds whose guest no longer exists,
    then images pinned solely by those orphaned holds and referenced by
    no surviving guest. Never removes a hold whose guest exists; never
    prunes all images.

    Returns ``(summary_text, failed_count)`` where ``summary_text`` is the
    user-facing plan/summary (no trailing newline) and ``failed_count`` is
    the number of candidate images podman refused to remove. Because every
    candidate is, by construction, an image with no surviving guest ref and
    no surviving-hold ref, a removal failure is meaningful (an external
    non-kento reference or a kento-accounting miss) — the CLI exits
    non-zero on a non-zero count, mirroring ``diagnose``.
    """
    guest_names = _guest_names()
    refs = _guest_image_refs()  # image -> guests still present
    holds = _holds()

    orphaned = [(name, image) for name, image in holds
                if name not in guest_names]
    surviving = [(name, image) for name, image in holds
                 if name in guest_names]

    if not orphaned:
        return "Nothing to prune.", 0

    # Images still pinned by a surviving (non-orphaned) hold must not be
    # removed. Likewise, images referenced by any guest must not be removed.
    surviving_hold_images = {img for _, img in surviving if img}
    orphaned_images = {img for _, img in orphaned if img}
    candidate_images = sorted(
        img for img in orphaned_images
        if img not in refs and img not in surviving_hold_images
    )

    if not yes:
        lines = []
        lines.append("Dry run — nothing removed. The following would be removed:")
        lines.append("  Orphaned hold containers:")
        for name, image in orphaned:
            lines.append(f"    kento-hold.{name}  (pinned {image or '?'})")
        if candidate_images:
            lines.append("  Images then eligible for removal:")
            for image in candidate_images:
                lines.append(f"    {image}")
        else:
            lines.append("  Images then eligible for removal: (none)")
        lines.append("Run 'kento prune --yes' to remove them.")
        return "\n".join(lines), 0

    removed_holds = 0
    for name, _image in orphaned:
        remove_image_hold(name)
        removed_holds += 1

    removed_images = 0
    failures: list[tuple[str, str]] = []
    for image in candidate_images:
        result = subprocess.run(
            [*_podman_cmd(), "image", "rm", image],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            removed_images += 1
        else:
            # Every candidate had no surviving guest ref and no surviving
            # hold ref, so a refusal here is meaningful — an external
            # non-kento reference or a kento-accounting miss. Surface it.
            msg = (result.stderr or result.stdout or "").strip()
            logger.warning("skipped image %s: %s", image, msg)
            failures.append((image, msg))

    lines = [f"Removed {removed_holds} orphaned hold(s), {removed_images} image(s)."]
    if failures:
        lines.append(
            f"Failed to remove {len(failures)} image(s) "
            "(still referenced or accounting mismatch):"
        )
        for image, reason in failures:
            lines.append(f"  {image}: {reason}")
    return "\n".join(lines), len(failures)
