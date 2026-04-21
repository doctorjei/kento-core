"""List kento-managed instances."""

import subprocess
from pathlib import Path

from kento import LXC_BASE, VM_BASE, is_running, read_mode


def list_containers(scope: str | None = None) -> None:
    rows = []

    image_files = []
    if scope in (None, "lxc"):
        if LXC_BASE.is_dir():
            image_files.extend(LXC_BASE.glob("*/kento-image"))
    if scope in (None, "vm"):
        if VM_BASE.is_dir():
            image_files.extend(VM_BASE.glob("*/kento-image"))

    for image_file in sorted(image_files, key=lambda f: f.parent.name):
        container_dir = image_file.parent
        container_id = container_dir.name
        image = image_file.read_text().strip()

        name_file = container_dir / "kento-name"
        display_name = name_file.read_text().strip() if name_file.is_file() else container_id

        mode = read_mode(container_dir)
        ctype = "pve-lxc" if mode == "pve" else mode

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

        rows.append((display_name, ctype, image, status, upper_size))

    if not rows:
        print("(no instances found)")
        return

    headers = ("NAME", "TYPE", "IMAGE", "STATUS", "UPPER SIZE")
    widths = []
    for i, header in enumerate(headers):
        col_max = max((len(row[i]) for row in rows), default=0)
        widths.append(max(len(header), col_max))

    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(val.ljust(w) for val, w in zip(row, widths)))
