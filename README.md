# Kento

Compose OCI images into system containers via overlayfs.

Kento reads podman's layer store directly — no image duplication, no
conversion. The OCI store IS the layer store.

Named after kento (見当): the registration notches carved into Japanese
woodblock printing blocks that ensure each color layer aligns perfectly.

## How it works

1. `kento lxc create <image>` (or `kento vm create <image>`) inspects an
   OCI image via podman, resolves the layer paths, and writes the
   appropriate config + hook.
2. At instance start, overlayfs is mounted using the image layers as
   read-only lower dirs, plus a writable upper layer.
3. The instance boots with systemd as PID 1 — a full system container
   or QEMU VM.

The OCI image layers are read-only. All writes go to a separate upper
directory. `kento lxc scrub` (or `kento vm scrub`) clears the upper
layer to revert to a clean image state.

## Four modes

The CLI uses a noun-verb pattern: `kento lxc <cmd>` for LXC instances,
`kento vm <cmd>` for VM instances. The noun selects the type.

- **lxc** (default on plain LXC) — standard LXC containers via
  `lxc-start`. Use `kento lxc create`.
- **pve-lxc** (default on Proxmox VE) — containers visible in Proxmox
  web UI via `pct`. Auto-detected when `/etc/pve` exists.
  Use `kento lxc create` on a PVE host.
- **vm** — boots OCI images as QEMU VMs via virtiofs.
  Use `kento vm create`. Kernel and initramfs come from inside the OCI
  image (`/boot/vmlinuz`, `/boot/initramfs.img`).
- **pve-vm** — QEMU VMs managed through PVE (hookscript + qm config).
  Auto-detected when using `kento vm create` on a PVE host.

PVE is auto-detected. Override with `--pve` to force PVE integration
or `--no-pve` to disable it.

## Requirements

- Python 3.11+
- Podman
- LXC (for LXC/PVE modes)
- util-linux 2.39+ (for `LIBMOUNT_FORCE_MOUNT2` support)
- QEMU + virtiofsd (for VM mode)
- nftables (for `--port` with LXC/PVE modes)

## Install

```
pip install kento
```

Or from source:

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

### Create an instance

```
sudo kento lxc create <image> [--name <name>]
sudo kento vm create <image> [--name <name>]
```

The noun (`lxc` or `vm`) selects the instance type.

Options:

| Flag | Default | Description |
|------|---------|-------------|
| `--name NAME` | auto | Instance name (auto-generated if omitted) |
| `--pve` / `--no-pve` | auto | Force or disable PVE integration (auto-detected by default) |
| `--network MODE` | auto | Network mode: `bridge`, `bridge=<name>`, `host`, `usermode`, `none` |
| `--nesting / --no-nesting` | on | Enable LXC nesting |
| `--vmid N` | auto | PVE VMID (PVE modes only) |
| `--memory MB` | varies | Memory limit in MB (default depends on mode) |
| `--cores N` | varies | Number of CPU cores (default depends on mode) |
| `--port H:G` | none | Port forwarding (all modes: usermode for VM, nftables for LXC) |
| `--ip CIDR` | none | Static IP address (e.g. `192.168.1.10/24`; works for all modes) |
| `--gateway IP` | none | Default gateway (requires `--ip`) |
| `--dns IP` | none | DNS server |
| `--searchdomain DOMAIN` | none | DNS search domain |
| `--timezone TZ` | none | Timezone (e.g. `America/New_York`) |
| `--env KEY=VALUE` | none | Environment variable (repeatable) |
| `--ssh-key PATH` | none | SSH public key file (repeatable) |
| `--ssh-key-user NAME` | root | User for SSH key injection |
| `--ssh-host-keys` | off | Auto-generate SSH host keys at create time |
| `--config-mode MODE` | auto | Config delivery: `injection`, `cloudinit`, or `auto` |
| `--mac XX:XX:...` | auto | Override MAC address (VM modes only) |
| `--start` | off | Start instance after creation |

### Run (create + start)

```
sudo kento lxc run <image> [--name <name>]
sudo kento vm run <image> [--name <name>]
```

Creates and starts an instance in one step. Accepts all the same flags
as `create` (except `--start`).

### Start

```
sudo kento start <name> [<name> ...]
```

Multiple instances can be started in one command. The `start` command
works across all modes (it reads the mode from metadata). You can also
use `kento lxc start` or `kento vm start` explicitly.

For LXC/PVE instances, you can also use `lxc-attach` / `pct exec` directly.
For VM instances, use `ssh -p <port> root@localhost`.

### Shutdown / stop

```
sudo kento shutdown <name> [<name> ...]
sudo kento stop <name> [<name> ...]
```

`shutdown` is the primary command; `stop` is an alias. Pass `-f` / `--force`
to force an immediate stop (kill) instead of a graceful shutdown.

### List

```
sudo kento list
sudo kento ls
```

Shows name, image, status, mode, and writable layer size. Lists instances
from all modes (lxc, pve-lxc, vm, pve-vm). `ls` is an alias for `list`.

### Info / inspect

```
sudo kento info <name>
sudo kento inspect <name>
```

Shows instance details: image, mode, status, directory paths, network
config, layer count, and more. `inspect` is an alias for `info`.

| Flag | Description |
|------|-------------|
| `--json` | Machine-readable JSON output |
| `-v` / `--verbose` | Include layer sizes and paths |

### Scrub an instance

```
sudo kento scrub <name> [<name> ...]
```

Clears the writable layer and re-resolves image layers from podman.
The instance must be stopped first.

### Destroy / rm

```
sudo kento destroy <name> [<name> ...]
sudo kento rm <name> [<name> ...]
sudo kento destroy -f <name>
```

`destroy` is the primary command; `rm` is an alias. Removes an instance
and its writable layer. Errors if the instance is running unless
`-f` / `--force` is passed (which stops it first).

## Runtime layout

```
/var/lib/lxc/<name>/            (LXC mode)
/var/lib/lxc/<VMID>/            (PVE mode)
├── config / kento-hook         # LXC config + mount hook
├── kento-image                 # OCI image name
├── kento-layers                # Pre-resolved layer paths
├── kento-state                 # Path to writable layer directory
├── kento-mode                  # "lxc", "pve-lxc", "vm", or "pve-vm"
├── kento-name                  # Instance name
└── rootfs/                     # Overlayfs mount point

/var/lib/kento/vm/<name>/       (VM mode)
├── kento-port                  # Host:guest port (e.g., "10022:22")
├── kento-qemu-pid              # QEMU PID (when running)
├── kento-virtiofsd-pid         # virtiofsd PID (when running)
├── virtiofsd.sock              # virtiofsd socket (when running)
└── rootfs/                     # Overlayfs mount point
```

## Documentation

- [Getting Started](docs/getting-started.md) — install, first instance walkthrough
- [Modes](docs/modes.md) — lxc vs pve-lxc vs vm vs pve-vm, auto-detection, defaults
- [VM Mode](docs/vm-mode.md) — image requirements, SSH access, port forwarding
- [Instance Lifecycle](docs/container-lifecycle.md) — naming, state, scrub, sudo behavior
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
