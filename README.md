# Kento

Compose OCI images into system containers via overlayfs.

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
directory. `kento container scrub` clears the upper layer to revert to
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

### Pull an image

```
sudo kento pull <image>
```

Fetches an OCI image via podman. This is optional — `create` will use
images already in podman's store.

### Create a container

```
sudo kento container create <image> [--name <name>]
```

Options:

| Flag | Default | Description |
|------|---------|-------------|
| `--name NAME` | auto | Container name (auto-generated if omitted) |
| `--pve` / `--lxc` / `--vm` | auto | Force mode |
| `--network MODE` | auto | Network mode: `bridge`, `bridge=<name>`, `host`, `usermode`, `none` |
| `--nesting / --no-nesting` | on | Enable LXC nesting |
| `--vmid N` | auto | PVE VMID (PVE mode only) |
| `--port H:G` | `auto:22` | Port forwarding (VM mode only) |
| `--ip CIDR` | none | Static IP address (e.g. `192.168.1.10/24`) |
| `--gateway IP` | none | Default gateway (requires `--ip`) |
| `--dns IP` | none | DNS server (requires `--ip`) |
| `--searchdomain DOMAIN` | none | DNS search domain |
| `--timezone TZ` | none | Timezone (e.g. `America/New_York`) |
| `--env KEY=VALUE` | none | Environment variable (repeatable) |
| `--start` | off | Start container after creation |

### Run (create + start)

```
sudo kento run <image> [--name <name>]
```

Creates and starts a container in one step. Accepts all the same flags
as `create` (except `--start`).

### Start

```
sudo kento container start <name> [<name> ...]
```

Multiple containers can be started in one command.

For LXC/PVE containers, you can also use `lxc-attach` / `pct exec` directly.
For VM containers, use `ssh -p <port> root@localhost`.

### Shutdown / stop

```
sudo kento container shutdown <name> [<name> ...]
sudo kento container stop <name> [<name> ...]
```

`shutdown` is the primary command; `stop` is an alias. Pass `-f` / `--force`
to force an immediate stop (kill) instead of a graceful shutdown.

### List

```
sudo kento container list
sudo kento container ls
```

Shows name, image, status, mode, and writable layer size. Lists containers
from all modes (LXC, PVE, VM). `ls` is an alias for `list`.

### Info / inspect

```
sudo kento info <name>
sudo kento inspect <name>
```

Shows container details: image, mode, status, directory paths, network
config, layer count, and more. `inspect` is an alias for `info`.

| Flag | Description |
|------|-------------|
| `--json` | Machine-readable JSON output |
| `-v` / `--verbose` | Include layer sizes and paths |

### Scrub a container

```
sudo kento container scrub <name> [<name> ...]
```

Clears the writable layer and re-resolves image layers from podman.
The container must be stopped first.

### Destroy / rm

```
sudo kento container destroy <name> [<name> ...]
sudo kento container rm <name> [<name> ...]
sudo kento container destroy -f <name>
```

`destroy` is the primary command; `rm` is an alias. Removes a container
and its writable layer. Errors if the container is running unless
`-f` / `--force` is passed (which stops it first).

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

## Documentation

- [Getting Started](docs/getting-started.md) — install, first container walkthrough
- [Modes](docs/modes.md) — LXC vs PVE vs VM, auto-detection, defaults
- [VM Mode](docs/vm-mode.md) — image requirements, SSH access, port forwarding
- [Container Lifecycle](docs/container-lifecycle.md) — naming, state, scrub, sudo behavior
- [Troubleshooting](docs/troubleshooting.md) — error messages and fixes
- [Architecture](docs/architecture.md) — overlayfs, hooks, startup sequences, internals

## Related projects

Kento is part of a stack of independent projects that work together but
are each usable on their own:

- **[kanibako](https://github.com/doctorjei/kanibako)** — container
  management platform built on droste images and kento.
- **[droste](https://github.com/doctorjei/droste)** — builds layered
  OCI images (process containers, system containers, VMs).
- **kento** (this project) — composes OCI images into running LXC
  containers or QEMU VMs via overlayfs. Works with any OCI image, not
  just droste's.
- **[tenkei](https://github.com/doctorjei/tenkei)** — provides the
  minimal kernel and initramfs for VM mode. Any compatible kernel +
  initramfs will work.

Kento was originally extracted from droste's OCI-backed LXC mount
system.

## License

GPL-3.0
