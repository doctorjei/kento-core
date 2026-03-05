"""Resolve OCI image layer paths via podman."""

import os
import subprocess
import sys


def _podman_cmd() -> list[str]:
    """Return the podman command prefix.

    When run via sudo, uses runuser to query the invoking user's
    podman store instead of root's.
    """
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        return ["runuser", "-u", sudo_user, "--", "podman"]
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
        print(f"Error: image not found: {image}", file=sys.stderr)
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
