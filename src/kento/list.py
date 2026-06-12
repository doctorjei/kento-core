"""List kento-managed instances."""

import json
import subprocess
from pathlib import Path

from kento import LXC_BASE, VM_BASE, is_running, pve_config_exists, read_mode
from kento.info import _get_ssh_host_key_fingerprints


def list_containers(scope: str | None = None, show_size: bool = False,
                    as_json: bool = False) -> None:
    instances = []

    image_files = []
    if scope in (None, "lxc"):
        if LXC_BASE.is_dir():
            image_files.extend(LXC_BASE.glob("*/kento-image"))
    if scope in (None, "vm"):
        if VM_BASE.is_dir():
            image_files.extend(VM_BASE.glob("*/kento-image"))

    for image_file in sorted(image_files, key=lambda f: f.parent.name):
        # list is read-only introspection: a concurrent `kento destroy`
        # (rmtree) can race between the glob above and the reads below,
        # raising FileNotFoundError/OSError. Skip the bad entry rather than
        # aborting the whole listing and hiding all healthy instances.
        try:
            container_dir = image_file.parent
            container_id = container_dir.name
            image = image_file.read_text().strip()

            name_file = container_dir / "kento-name"
            display_name = name_file.read_text().strip() if name_file.is_file() else container_id

            mode = read_mode(container_dir)
            # Normalize the raw mode ('pve' -> 'pve-lxc') to match info.py.
            ctype = "pve-lxc" if mode == "pve" else mode
            # type = LXC/VM family (same derivation as info.py).
            family = "VM" if mode in ("vm", "pve-vm") else "LXC"

            # JSON vmid mirrors info.py exactly: from the kento-vmid file only
            # (so list --json and inspect --json agree). For plain pve-lxc the
            # orphan check uses the dir name (which is the vmid), independent
            # of this.
            vmid_file = container_dir / "kento-vmid"
            vmid = vmid_file.read_text().strip() if vmid_file.is_file() else None

            # For PVE modes, surface an orphaned instance (PVE config gone,
            # destroyed out-of-band) as "orphan" so the user can see it and
            # clean it up with `destroy -f`.
            if mode in ("pve", "pve-vm"):
                check_vmid = container_dir.name if mode == "pve" else vmid
                if check_vmid is None or not pve_config_exists(check_vmid, mode):
                    status = "orphan"
                else:
                    status = "running" if is_running(container_dir, mode) else "stopped"
            else:
                status = "running" if is_running(container_dir, mode) else "stopped"

            # Build the per-instance dict, mirroring the keys inspect --json
            # emits so machine consumers can drop an N+1 inspect call.
            entry: dict = {
                "name": display_name,
                "type": family,
                "mode": ctype,
                "image": image,
                "status": status,
            }
            # The mac/env/vmid/fingerprint fields only surface in --json. The
            # human table never shows them, and _get_ssh_host_key_fingerprints
            # shells out to ssh-keygen per key — so skip all of it (especially
            # the subprocess) on the common columnar path.
            if as_json:
                if vmid is not None:
                    try:
                        entry["vmid"] = int(vmid)
                    except (TypeError, ValueError):
                        entry["vmid"] = vmid

                mac_file = container_dir / "kento-mac"
                if mac_file.is_file():
                    mac = mac_file.read_text().strip()
                    if mac:
                        entry["mac"] = mac

                env_file = container_dir / "kento-env"
                if env_file.is_file():
                    env = env_file.read_text()
                    env_lines = env.splitlines()
                    if env_lines:
                        entry["environment"] = env_lines

                fingerprints, _ = _get_ssh_host_key_fingerprints(container_dir)
                if fingerprints:
                    entry["ssh_host_key_fingerprints"] = fingerprints

            if show_size:
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
                entry["upper_size"] = upper_size

            instances.append(entry)
        except OSError:
            continue

    if as_json:
        print(json.dumps(instances, indent=2))
        return

    if not instances:
        print("(no instances found)")
        return

    if show_size:
        rows = [(e["name"], e["mode"], e["image"], e["status"], e["upper_size"])
                for e in instances]
    else:
        rows = [(e["name"], e["mode"], e["image"], e["status"])
                for e in instances]

    if show_size:
        headers = ("NAME", "TYPE", "IMAGE", "STATUS", "UPPER SIZE")
    else:
        headers = ("NAME", "TYPE", "IMAGE", "STATUS")
    widths = []
    for i, header in enumerate(headers):
        col_max = max((len(row[i]) for row in rows), default=0)
        widths.append(max(len(header), col_max))

    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(val.ljust(w) for val, w in zip(row, widths)))
