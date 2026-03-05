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

All commands except `list` require root.

### Create a container

```
sudo kento create mybox --image myimage:latest
```

Options:

```
--bridge NAME        Network bridge (default: lxcbr0)
--memory MB          Memory limit (default: no limit)
--cores N            CPU cores (default: no limit)
--nesting/--no-nesting  LXC nesting (default: on)
--start              Start container after creation
```

### Start / stop

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
The container must be stopped first.

### Destroy a container

```
sudo kento destroy mybox
```

Stops the container if running, unmounts the rootfs, and removes
everything.

## Runtime layout

```
/var/lib/lxc/<name>/
├── config          # LXC config (generated)
├── kento-hook      # Mount hook script (generated, per-container)
├── kento-image     # OCI image name
├── kento-layers    # Pre-resolved layer paths
├── rootfs/         # Overlayfs mount point
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

## License

MIT
