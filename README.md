# Kento

Compose OCI container images into LXC system containers via overlayfs.

Kento reads podman's layer store directly — no image duplication, no
conversion. The OCI store IS the layer store.

Named after kento (見当): the registration notches carved into Japanese
woodblock printing blocks that ensure each color layer aligns perfectly.

## How it works

1. `kento container create <image>` inspects an OCI image via podman,
   resolves the layer paths, and writes the appropriate config + hook.
2. At container start, overlayfs is mounted using the image layers as
   read-only lower dirs, plus a writable upper layer.
3. The container boots with systemd as PID 1 — a full system container.

The OCI image layers are read-only. All writes go to a separate upper
directory. `kento container reset` clears the upper layer to revert to
a clean image state.

## Three modes

- **LXC** (default on plain LXC) — standard LXC containers via
  `lxc-start`. Auto-detected.
- **PVE** (default on Proxmox VE) — containers visible in Proxmox web UI
  via `pct`. Auto-detected when `/etc/pve` exists.
- **VM** (explicit `--vm` only) — boots OCI images as QEMU VMs via
  virtiofs. Kernel and initramfs come from inside the OCI image
  (`/boot/vmlinuz`, `/boot/initramfs.img`).

## Requirements

- Python 3.11+
- Podman
- LXC (for LXC/PVE modes)
- util-linux 2.39+ (for `LIBMOUNT_FORCE_MOUNT2` support)
- QEMU + virtiofsd (for VM mode)

## Install

```
pipx install .
```

Or with pip:

```
pip install .
```

## Usage

All commands require root (run with `sudo`).

### Create a container

```
sudo kento container create <image> [--name <name>]
```

Options:

| Flag | Default | Description |
|------|---------|-------------|
| `--name NAME` | auto | Container name (auto-generated if omitted) |
| `--pve` / `--lxc` / `--vm` | auto | Force mode |
| `--bridge NAME` | `vmbr0`/`lxcbr0` | Network bridge (LXC/PVE) |
| `--memory MB` | no limit | Memory limit in MB |
| `--cores N` | no limit | CPU core count |
| `--nesting / --no-nesting` | on | Enable LXC nesting |
| `--vmid N` | auto | PVE VMID (PVE mode only) |
| `--port H:G` | `auto:22` | Port forwarding (VM mode only) |
| `--start` | off | Start container after creation |

### Start / stop

```
sudo kento container start <name>
sudo kento container stop <name>
```

For LXC/PVE containers, you can also use `lxc-attach` / `pct exec` directly.
For VM containers, use `ssh -p <port> root@localhost`.

### List containers

```
sudo kento container list
```

Shows name, image, status, mode, and writable layer size. Lists containers
from all modes (LXC, PVE, VM).

### Reset a container

```
sudo kento container reset <name>
```

Clears the writable layer and re-resolves image layers from podman.
The container must be stopped first.

### Remove a container

```
sudo kento container rm <name>
```

Stops the container if running, unmounts the rootfs, and removes
everything including the writable layer.

## Runtime layout

```
/var/lib/lxc/<name>/            (LXC mode)
/var/lib/lxc/<VMID>/            (PVE mode)
├── config / kento-hook         # LXC config + mount hook
├── kento-image                 # OCI image name
├── kento-layers                # Pre-resolved layer paths
├── kento-state                 # Path to writable layer directory
├── kento-mode                  # "lxc", "pve", or "vm"
├── kento-name                  # Container name
└── rootfs/                     # Overlayfs mount point

/var/lib/kento/vm/<name>/       (VM mode)
├── kento-port                  # Host:guest port (e.g., "10022:22")
├── kento-qemu-pid              # QEMU PID (when running)
├── kento-virtiofsd-pid         # virtiofsd PID (when running)
├── virtiofsd.sock              # virtiofsd socket (when running)
└── rootfs/                     # Overlayfs mount point
```

## License

MIT
