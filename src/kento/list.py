"""List kento-managed containers."""

import subprocess
from pathlib import Path

from kento import LXC_BASE, VM_BASE, is_running


def list_containers() -> None:
    found = False

    print(f"{'CONTAINER':<20} {'IMAGE':<30} {'STATUS':<10} {'MODE':<6} UPPER SIZE")
    print(f"{'---------':<20} {'-----':<30} {'------':<10} {'----':<6} ----------")

    # Collect kento-image files from both LXC and VM bases
    image_files = []
    for base in [LXC_BASE, VM_BASE]:
        if base.is_dir():
            image_files.extend(base.glob("*/kento-image"))

    for image_file in sorted(image_files, key=lambda f: f.parent.name):
        found = True
        container_dir = image_file.parent
        container_id = container_dir.name
        image = image_file.read_text().strip()

        # Display name from kento-name file (falls back to dir name)
        name_file = container_dir / "kento-name"
        display_name = name_file.read_text().strip() if name_file.is_file() else container_id

        # Detect mode
        mode_file = container_dir / "kento-mode"
        mode = mode_file.read_text().strip() if mode_file.is_file() else "lxc"

        # Status check
        status = "running" if is_running(container_dir, mode) else "stopped"

        state_file = container_dir / "kento-state"
        state_dir = Path(state_file.read_text().strip()) if state_file.is_file() else container_dir
        upper_dir = state_dir / "upper"
        if upper_dir.is_dir():
            du = subprocess.run(
                ["du", "-sh", str(upper_dir)],
                capture_output=True, text=True,
            )
            upper_size = du.stdout.split()[0] if du.returncode == 0 else "?"
        else:
            upper_size = "0"

        print(f"{display_name:<20} {image:<30} {status:<10} {mode:<6} {upper_size}")

    if not found:
        print("(no kento-managed containers found)")
