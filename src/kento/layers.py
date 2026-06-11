"""Resolve OCI image layer paths via podman."""

import subprocess
import sys


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
        print(f"Error: image not found in local store: {image}", file=sys.stderr)
        print(f"  Pull it first:  kento pull {image}", file=sys.stderr)
        sys.exit(1)

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
