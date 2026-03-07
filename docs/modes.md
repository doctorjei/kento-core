# Modes

Kento operates in one of three modes, each producing a running system
from the same OCI image layers.

## Mode detection

By default, kento auto-detects the mode:

- If `/etc/pve` exists → **PVE mode**
- Otherwise → **LXC mode**
- **VM mode** is never auto-detected — requires `--vm`

Override with `--pve`, `--lxc`, or `--vm`:

```
sudo kento container create <image> --lxc
sudo kento container create <image> --pve
sudo kento container create <image> --vm
```

The mode is recorded in `kento-mode` at create time. All subsequent
commands (start, stop, reset, rm) read this file — you don't need to
pass the flag again.

## LXC mode

Standard LXC system containers. This is the default on non-PVE hosts.

- **Start command:** `lxc-start`
- **Access:** `sudo lxc-attach -n <name>`
- **Network bridge:** `lxcbr0` (override with `--bridge`)
- **Config location:** `/var/lib/lxc/<name>/config`
- **Memory/CPU:** no limit unless `--memory` or `--cores` specified
- **Nesting:** enabled by default (`--no-nesting` to disable)
- **Apparmor:** set to `unconfined`

The container runs systemd as PID 1 in a shared kernel namespace.

## PVE mode

Proxmox VE containers — same LXC underneath, but integrated with the
PVE management stack. Containers appear in the Proxmox web UI and can
be managed with `pct`.

- **Start command:** `pct start <VMID>`
- **Access:** `pct exec <VMID> -- bash` or the Proxmox web console
- **Network bridge:** `vmbr0` (override with `--bridge`)
- **Config location:** `/etc/pve/nodes/<hostname>/lxc/<VMID>.conf`
- **Memory:** 512 MB default (override with `--memory`)
- **CPU:** 1 core default (override with `--cores`)
- **Nesting:** enabled by default
- **VMID:** auto-assigned (lowest free >= 100), or specify with `--vmid`

### VMID allocation

PVE requires a numeric VMID >= 100 for each container. Kento reads
`/etc/pve/.vmlist` (or scans config files as fallback) to find used
IDs, then assigns the lowest free one.

The container directory is named by VMID (e.g., `/var/lib/lxc/100/`)
but the human-readable name is stored in the `kento-name` file. All
kento commands accept the container name, not the VMID.

### PVE startup sequence

1. PVE generates LXC config with hardcoded rootfs path
2. `lxc-pve-prestart-hook` runs (harmless no-op for kento)
3. Kento's hook fires (`pre-mount`) — mounts overlayfs
4. LXC bind-mounts the populated rootfs
5. Container boots with systemd

## VM mode

Full QEMU virtual machines with the guest kernel coming from inside the
OCI image. See [VM Mode](vm-mode.md) for full details.

- **Start command:** QEMU via kento (no external hypervisor manager)
- **Access:** SSH (`ssh -p <port> <user>@localhost`)
- **Network:** QEMU user-mode networking with port forwarding
- **Memory:** 512 MB (hardcoded)
- **Container directory:** `/var/lib/kento/vm/<name>/`
- **Requires:** `--vm` flag, QEMU, virtiofsd, kernel + initramfs in image

VM mode is useful when you need a real kernel — custom modules, kernel
testing, or full hardware isolation.

## Comparison

| | LXC | PVE | VM |
|---|---|---|---|
| Auto-detected | Yes | Yes | No |
| Guest kernel | Shared (host) | Shared (host) | Own (from image) |
| Start time | ~1s | ~1s | ~10s |
| Access | lxc-attach | pct exec / web UI | SSH |
| Network | Bridge | Bridge | User-mode (NAT) |
| Management UI | None | Proxmox web UI | None |
| Memory default | No limit | 512 MB | 512 MB |
| CPU default | No limit | 1 core | Host CPU |
