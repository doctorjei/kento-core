"""Resolve OCI image layer paths via podman."""

import os
import subprocess
import sys


def _podman_cmd(mode: str | None = None) -> list[str]:
    """Return the podman command prefix.

    In LXC/PVE mode (or when mode is None), always uses root's podman
    store to avoid UID remapping issues with rootless layers.
    In VM mode, uses runuser to query the invoking user's podman store
    when run via sudo.
    """
    if mode == "vm":
        sudo_user = os.environ.get("SUDO_USER")
        if sudo_user:
            return ["runuser", "-u", sudo_user, "--", "podman"]
    return ["podman"]


def resolve_layers(image: str, mode: str | None = None) -> str:
    """Return colon-separated lowerdir string for an OCI image.

    Queries podman for the image's GraphDriver layer paths.
    Upper layer comes first (topmost), matching overlayfs lowerdir order.
    """
    podman = _podman_cmd(mode)

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
