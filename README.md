# Kento

Compose OCI container images into LXC system containers via overlayfs.

Kento reads podman's layer store directly — no image duplication, no
conversion. The OCI store IS the layer store.

Named after kento (見当): the registration notches carved into Japanese
woodblock printing blocks that ensure each color layer aligns perfectly.

## How it works

1. `kento create` inspects an OCI image via podman, resolves the layer
   paths, and writes an LXC config with a generated hook script.
2. At container start, the hook mounts overlayfs using the image layers
   as read-only lower dirs, plus a writable upper layer.
3. The container boots with systemd as PID 1 — a full system container.

The OCI image layers are read-only. All writes go to a separate upper
directory. `kento reset` clears the upper layer to revert to a clean
image state.

## Requirements

- Python 3.11+
- Podman
- LXC
- util-linux 2.39+ (for `LIBMOUNT_FORCE_MOUNT2` support)

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
sudo kento create mybox --image myimage:latest
```

Options:

| Flag | Default | Description |
|------|---------|-------------|
| `--image IMAGE` | (required) | OCI image name |
| `--bridge NAME` | `lxcbr0` | Network bridge |
| `--memory MB` | no limit | Memory limit in MB |
| `--cores N` | no limit | CPU core count |
| `--nesting / --no-nesting` | on | Enable LXC nesting |
| `--start` | off | Start container after creation |

### Start / stop / attach

Kento creates standard LXC containers. Use LXC tools directly:

```
sudo lxc-start -n mybox
sudo lxc-stop -n mybox
sudo lxc-attach -n mybox
```

### List containers

```
sudo kento list
```

Shows name, image, status, and writable layer size.

### Reset a container

```
sudo kento reset mybox
```

Clears the writable layer and re-resolves image layers from podman.
The container must be stopped first. Use this after pulling an updated
image to pick up the new layers.

### Destroy a container

```
sudo kento destroy mybox
```

Stops the container if running, unmounts the rootfs, and removes
everything including the writable layer.

## Writable layer storage

When you run kento via `sudo`, the writable layer (upper/work) is
stored in your home directory, separate from the LXC container config:

```
~/.local/share/kento/<name>/
├── upper/          # All container writes land here
└── work/           # Overlayfs internal workdir
```

When run as root directly (not via sudo), the writable layer is stored
alongside the container in `/var/lib/lxc/<name>/`.

This means:
- Each user's container changes are isolated in their home directory
- Cleanup is straightforward: `kento destroy` removes both locations,
  or you can manually delete `~/.local/share/kento/` to wipe all
  writable state
- `kento reset` clears the writable layer without destroying the
  container

## Runtime layout

```
/var/lib/lxc/<name>/
├── config          # LXC config (generated)
├── kento-hook      # Mount hook script (generated, per-container)
├── kento-image     # OCI image name
├── kento-layers    # Pre-resolved layer paths
├── kento-state     # Path to writable layer directory
└── rootfs/         # Overlayfs mount point

~/.local/share/kento/<name>/    (when run via sudo)
├── upper/          # Writable layer
└── work/           # Overlayfs workdir
```

## Layer resolution

OCI image layers are resolved at create time (and on reset) because
the LXC hook runs in a restricted mount namespace where podman can't
initialize its storage driver. The hook validates that layer paths
still exist at start time and gives an actionable error if they don't:

```
Error: layer path missing: /var/lib/containers/storage/overlay/.../diff
Image may have changed. Run: kento reset mybox
```

When run via sudo, kento queries the invoking user's podman store
(not root's), so your images don't need to be in the root store.

## License

MIT
