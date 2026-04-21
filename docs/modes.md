# Modes

Kento operates in one of four modes, each producing a running system
from the same OCI image layers.

## Noun-verb pattern

The CLI uses a noun-verb pattern to select the instance type:

- `kento lxc <cmd>` — LXC instances (lxc or pve-lxc)
- `kento vm <cmd>` — VM instances (vm or pve-vm)

The noun selects the type; PVE integration is auto-detected based on
whether `/etc/pve` exists.

## Mode detection

- `kento lxc create` on a plain host → **lxc** mode
- `kento lxc create` on a PVE host → **pve-lxc** mode
- `kento vm create` on a plain host → **vm** mode
- `kento vm create` on a PVE host → **pve-vm** mode

Override PVE auto-detection with `--pve` or `--no-pve`:

```
sudo kento lxc create <image> --pve       # Force PVE integration
sudo kento lxc create <image> --no-pve    # Disable PVE integration
sudo kento vm create <image> --no-pve     # Plain VM even on PVE host
```

The mode is recorded in `kento-mode` at create time. All subsequent
commands (start, shutdown, scrub, destroy) read this file — you don't
need to pass the flag again.

## lxc mode

Standard LXC system containers. Created with `kento lxc create` on a
non-PVE host.

- **Start command:** `lxc-start`
- **Access:** `sudo lxc-attach -n <name>`
- **Network bridge:** `lxcbr0` (override with `--network bridge=<name>`)
- **Config location:** `/var/lib/lxc/<name>/config`
- **Memory/CPU:** no limit by default (override with `--memory` / `--cores`)
- **Nesting:** enabled by default (`--no-nesting` to disable)
- **Apparmor:** set to `unconfined`

The instance runs systemd as PID 1 in a shared kernel namespace.

## pve-lxc mode

Proxmox VE containers -- same LXC underneath, but integrated with the
PVE management stack. Instances appear in the Proxmox web UI and can
be managed with `pct`. Created with `kento lxc create` on a PVE host
(or with `--pve` on any host).

- **Start command:** `pct start <VMID>`
- **Access:** `pct exec <VMID> -- bash` or the Proxmox web console
- **Network bridge:** `vmbr0` (override with `--network bridge=<name>`)
- **Config location:** `/etc/pve/nodes/<hostname>/lxc/<VMID>.conf`
- **Memory:** 512 MB default (override with `--memory`, configurable via `/etc/kento/lxc.conf`)
- **CPU:** 1 core default (override with `--cores`, configurable via `/etc/kento/lxc.conf`)
- **Nesting:** enabled by default
- **VMID:** auto-assigned (lowest free >= 100), or specify with `--vmid`

### VMID allocation

PVE requires a numeric VMID >= 100 for each instance. Kento reads
`/etc/pve/.vmlist` (or scans config files as fallback) to find used
IDs, then assigns the lowest free one.

The instance directory is named by VMID (e.g., `/var/lib/lxc/100/`)
but the human-readable name is stored in the `kento-name` file. All
kento commands accept the instance name, not the VMID.

### PVE startup sequence

1. PVE generates LXC config with hardcoded rootfs path
2. `lxc-pve-prestart-hook` runs (harmless no-op for kento)
3. Kento's hook fires (`pre-mount`) -- mounts overlayfs
4. LXC bind-mounts the populated rootfs
5. Instance boots with systemd

## vm mode

Full QEMU virtual machines with the guest kernel coming from inside the
OCI image. Created with `kento vm create`. See [VM Mode](vm-mode.md)
for full details.

- **Start command:** QEMU via kento (no external hypervisor manager)
- **Access:** SSH (`ssh -p <port> <user>@localhost`)
- **Network:** QEMU user-mode networking with port forwarding
- **Memory:** 512 MB default (override with `--memory`, configurable via `/etc/kento/vm.conf`)
- **Instance directory:** `/var/lib/kento/vm/<name>/`
- **Requires:** QEMU, virtiofsd, kernel + initramfs in image

VM mode is useful when you need a real kernel -- custom modules, kernel
testing, or full hardware isolation.

## pve-vm mode

QEMU VMs managed through Proxmox VE. Created with `kento vm create` on
a PVE host (or with `--pve`). Uses a PVE hookscript and qm config so
the VM appears in the Proxmox web UI.

- **Start command:** `qm start <VMID>`
- **Access:** `qm terminal <VMID>` or SSH
- **Network:** bridge (`vmbr0` by default) with `net0:` in qm config
- **Memory:** 512 MB default (override with `--memory`)
- **Instance directory:** `/var/lib/kento/vm/<name>/`
- **Requires:** QEMU, virtiofsd, kernel + initramfs in image, PVE host

## Comparison

| | lxc | pve-lxc | vm | pve-vm |
|---|---|---|---|---|
| Noun | `kento lxc` | `kento lxc` | `kento vm` | `kento vm` |
| PVE auto-detect | n/a | Yes | n/a | Yes |
| Guest kernel | Shared (host) | Shared (host) | Own (from image) | Own (from image) |
| Start time | ~1s | ~1s | ~10s | ~10s |
| Access | lxc-attach | pct exec / web UI | SSH | qm terminal / SSH |
| Network | Bridge | Bridge | User-mode (NAT) | Bridge |
| Management UI | None | Proxmox web UI | None | Proxmox web UI |
| Memory default | No limit | 512 MB | 512 MB | 512 MB |
| CPU default | No limit | 1 core | Host CPU | Host CPU |
