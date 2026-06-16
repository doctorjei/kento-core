"""Resolve OCI image layer paths via podman."""

import logging
import subprocess
from pathlib import Path

from kento.errors import ImageNotFoundError, StateError

logger = logging.getLogger("kento")


def _podman_cmd() -> list[str]:
    """Return the podman command prefix."""
    return ["podman"]


def resolve_layers(image: str) -> str:
    """Return colon-separated lowerdir string for an OCI image.

    Queries podman for the image's GraphDriver layer paths.
    Upper layer comes first (topmost), matching overlayfs lowerdir order.
    """
    podman = _podman_cmd()

    result = subprocess.run(
        [*podman, "image", "exists", image],
        capture_output=True,
    )
    if result.returncode != 0:
        raise ImageNotFoundError(
            f"image not found in local store: {image}"
            f" — pull it first:  kento pull {image}"
        )

    upper = subprocess.run(
        [*podman, "image", "inspect", image,
         "--format", "{{.GraphDriver.Data.UpperDir}}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    lower = subprocess.run(
        [*podman, "image", "inspect", image,
         "--format", "{{.GraphDriver.Data.LowerDir}}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    if lower and lower != "<no value>":
        return f"{upper}:{lower}"
    return upper


def to_overlay_lowerdir(layers: str) -> tuple[str, str]:
    """Convert an absolute colon-joined LAYERS string into chdir-relative form.

    Returns ``(overlay_root, relative_lowerdir)`` where:

      * ``overlay_root`` is the common parent of the podman store layers
        (e.g. ``/var/lib/containers/storage/overlay``) — the directory a
        mount must ``chdir`` into before using ``relative_lowerdir``.
      * ``relative_lowerdir`` is the colon-joined lowerdir built from short
        ``l/<SHORTID>`` symlinks (matching Docker overlay2 / podman), in the
        same top-to-bottom order as the input.

    Why: the kernel caps classic ``mount(2)`` options at one 4096-byte page.
    Each absolute ``<root>/<id>/diff`` lowerdir entry is ~104 bytes; a deeply
    layered image overruns the page and the kernel SILENTLY TRUNCATES the
    options string, corrupting upperdir/workdir and failing the overlay mount
    with cryptic errors. podman/Docker avoid this by mounting from short
    per-layer symlinks (``l/<short>`` → ``../<id>/diff``, ~28 bytes each) after
    chdir-ing into the overlay root. We do the same.

    Each layer's short id lives in the sibling ``<root>/<id>/link`` file.

    Degrades gracefully and NEVER raises on an unexpected store layout:

      * if a layer's ``link`` file is missing/empty, fall back to the relative
        ``<id>/diff`` form for that entry (still relative to the overlay root,
        so still short-ish);
      * if the overlay root can't be derived at all (layers don't share a
        ``<root>/<id>/diff`` shape), return ``("", layers)`` — the original
        absolute behavior, so the caller mounts as before.
    """
    entries = [e for e in layers.split(":") if e]
    if not entries:
        return "", layers

    # Derive the common overlay root by stripping the trailing /<id>/diff from
    # each entry. All podman overlay layers share one root. If any entry does
    # not match that shape, or the roots disagree, fall back to absolute.
    root: str | None = None
    parsed: list[Path] = []
    for e in entries:
        p = Path(e)
        # Expect .../<id>/diff
        if p.name != "diff" or p.parent == p.parent.parent:
            return "", layers
        layer_root = p.parent.parent  # strip /<id>/diff
        if root is None:
            root = str(layer_root)
        elif str(layer_root) != root:
            return "", layers
        parsed.append(p)

    if root is None:
        return "", layers

    rel_parts: list[str] = []
    for p in parsed:
        layer_id = p.parent.name
        link_file = p.parent / "link"
        short = ""
        try:
            short = link_file.read_text().strip()
        except OSError:
            short = ""
        if short:
            rel_parts.append(f"l/{short}")
        else:
            # Fallback for this layer: relative <id>/diff (still under root).
            rel_parts.append(f"{layer_id}/diff")

    return root, ":".join(rel_parts)


def preflight_overlay_layers(layers: str, state_dir: "Path | None" = None) -> None:
    """Fail closed (before any state is written) if the overlay can't be mounted.

    Two checks:

      1. **Layer-count cap** (``MAX_OVERLAY_LAYERS``, Docker overlay2 parity).
         An image with more layers than the cap is rejected. Bounded by the
         4096-byte mount(2) options page limit — see ``to_overlay_lowerdir``.
      2. **Defensive byte assert** (only when ``state_dir`` is given). Compute
         the EXACT options string kento will hand the kernel
         (``lowerdir=...,upperdir=...,workdir=...`` in the short l/<short>
         chdir-relative form) and refuse if it is within 16 bytes of the page
         limit. With the l/<short> form the 128 cap keeps us far under this;
         this is a backstop for pathological state-dir / name lengths so we
         NEVER hand the kernel a truncatable string.

    Raises ``StateError`` with the layer count (and byte count) on violation.
    """
    from kento.defaults import MAX_OVERLAY_LAYERS, OVERLAY_OPTS_PAGE_LIMIT

    entries = [e for e in layers.split(":") if e]
    count = len(entries)
    if count > MAX_OVERLAY_LAYERS:
        raise StateError(
            f"image has {count} overlay layers, exceeding kento's cap of "
            f"{MAX_OVERLAY_LAYERS} (Docker overlay2 parity).\n"
            "  The kernel caps classic mount(2) options at one 4096-byte page;\n"
            "  a deeper lowerdir would be silently truncated and fail to mount.\n"
            "  Flatten/squash the image to fewer layers, or rebuild it with a\n"
            "  shallower layer stack."
        )

    if state_dir is not None:
        root, rel = to_overlay_lowerdir(layers)
        upper = Path(state_dir) / "upper"
        work = Path(state_dir) / "work"
        opts = f"lowerdir={rel},upperdir={upper},workdir={work}"
        if len(opts) >= OVERLAY_OPTS_PAGE_LIMIT - 16:
            raise StateError(
                f"overlay mount options are {len(opts)} bytes across {count} "
                f"layers, at or above the {OVERLAY_OPTS_PAGE_LIMIT}-byte "
                "mount(2) single-page limit.\n"
                "  The kernel would silently truncate the options string and "
                "fail the overlay mount.\n"
                "  Use a shorter KENTO_STATE_DIR / instance name, or an image "
                "with fewer layers."
            )


def create_image_hold(image: str, name: str) -> None:
    """Create a stopped podman container to pin the image against pruning."""
    hold_name = f"kento-hold.{name}"
    subprocess.run(
        [*_podman_cmd(), "create", "--name", hold_name,
         "--label", f"io.kento.hold-for={name}",
         image, "/bin/true"],
        capture_output=True,
    )


def ensure_image_hold(image: str, name: str) -> None:
    """Idempotent — create the image hold only if missing (backfills pre-hold guests)."""
    try:
        exists = subprocess.run(
            [*_podman_cmd(), "container", "exists", f"kento-hold.{name}"],
            capture_output=True,
        )
        if exists.returncode != 0:
            create_image_hold(image, name)
    except Exception:
        pass


def remove_image_hold(name: str) -> None:
    """Remove the podman hold container for the given kento container."""
    hold_name = f"kento-hold.{name}"
    subprocess.run(
        [*_podman_cmd(), "rm", hold_name],
        capture_output=True,
    )
