"""List kento-managed LXC containers."""

import subprocess
from pathlib import Path

from kento import LXC_BASE


def list_containers() -> None:
    found = False

    print(f"{'CONTAINER':<20} {'IMAGE':<30} {'STATUS':<10} {'MODE':<6} UPPER SIZE")
    print(f"{'---------':<20} {'-----':<30} {'------':<10} {'----':<6} ----------")

    for image_file in sorted(LXC_BASE.glob("*/kento-image")):
        found = True
        lxc_dir = image_file.parent
        container_id = lxc_dir.name
        image = image_file.read_text().strip()

        # Display name from kento-name file (falls back to dir name)
        name_file = lxc_dir / "kento-name"
        display_name = name_file.read_text().strip() if name_file.is_file() else container_id

        # Detect mode
        mode_file = lxc_dir / "kento-mode"
        mode = mode_file.read_text().strip() if mode_file.is_file() else "lxc"

        # Status check — use mode-appropriate command
        if mode == "pve":
            result = subprocess.run(
                ["pct", "status", container_id],
                capture_output=True, text=True,
            )
            status = "running" if result.returncode == 0 and "running" in result.stdout else "stopped"
        else:
            result = subprocess.run(
                ["lxc-info", "-n", container_id, "-sH"],
                capture_output=True, text=True,
            )
            status = "running" if result.returncode == 0 and "RUNNING" in result.stdout else "stopped"

        state_file = lxc_dir / "kento-state"
        state_dir = Path(state_file.read_text().strip()) if state_file.is_file() else lxc_dir
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
