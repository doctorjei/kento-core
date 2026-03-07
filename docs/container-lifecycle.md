# Container Lifecycle

## Create

```
sudo kento container create <image> [--name <name>]
```

The create command:

1. Resolves OCI image layers from podman's store
2. Creates the container directory with metadata files
3. Generates a mount hook script (LXC/PVE modes)
4. Writes the LXC or PVE config (LXC/PVE modes)
5. Allocates a port and writes `kento-port` (VM mode)

No image data is copied. Kento reads podman's layer store directly —
the OCI store IS the layer store.

### Auto-naming

If `--name` is omitted, kento generates a name from the image reference:

```
docker.io/library/debian:12 → docker.io-library-debian_12-0
ghcr.io/org/my-image:latest → ghcr.io-org-my--image_latest-0
```

The transformation is bijective (reversible):

| Character | Becomes |
|-----------|---------|
| `-` | `--` |
| `/` | `-` |
| `_` | `__` |
| `:` | `_` |

A numeric suffix (`-0`, `-1`, `-2`, ...) is appended, incrementing
until an unused name is found.

### Explicit naming

With `--name`, the name is used as-is. If a container with that name
already exists, kento exits with an error.

### The `--start` flag

Pass `--start` to start the container immediately after creation:

```
sudo kento container create <image> --name my-ct --start
```

## Start

```
sudo kento container start <name>
```

Behavior depends on the mode recorded at create time:

- **LXC:** runs `lxc-start -n <name>`
- **PVE:** runs `pct start <VMID>`
- **VM:** mounts overlayfs, starts virtiofsd + QEMU, writes PID files

## Stop

```
sudo kento container stop <name>
```

- **LXC:** runs `lxc-stop -n <name>`
- **PVE:** runs `pct stop <VMID>`
- **VM:** sends SIGTERM to QEMU and virtiofsd, waits for exit (SIGKILL
  fallback after 5s), unmounts rootfs

## Reset

```
sudo kento container reset <name>
```

Resets a container to a clean state matching the OCI image:

1. Checks the container is stopped (refuses if running)
2. Unmounts rootfs if still mounted
3. Deletes and recreates the writable upper and work directories
4. Re-resolves image layers from podman (picks up image updates)
5. Regenerates the hook script (LXC/PVE modes)

Use reset when:

- You want to discard all changes and start fresh
- You've updated the OCI image and want the container to use new layers
- Layer paths have gone stale (podman reorganized its store)

## Remove

```
sudo kento container rm <name>
```

Destroys a container completely:

1. Stops the container if running (automatic)
2. Unmounts rootfs
3. Releases the OCI image mount (LXC/PVE modes)
4. Removes the state directory (writable layer)
5. Removes the container directory
6. Deletes the PVE config (PVE mode)

This is irreversible. All writable state is lost.

## Sudo and user storage

When kento is run via `sudo`, it detects the invoking user from
`SUDO_USER` and:

- **Queries the invoking user's podman store** (via `runuser`) instead
  of root's. This means `sudo kento container create myimage` uses the
  images you pulled as your normal user.

- **Stores the writable layer** in `~user/.local/share/kento/<name>/`
  instead of under `/var/lib/lxc/<name>/`.

When run as root directly (not via sudo), everything stays under the
container directory.

The `kento-state` file records which path was used, so reset and rm
work correctly regardless of how you run them later.

## Runtime files

Each container directory contains metadata files:

| File | Contents | Example |
|------|----------|---------|
| `kento-image` | OCI image reference | `docker.io/library/debian:12` |
| `kento-layers` | Colon-separated layer paths from podman | `/path/to/layer1/diff:/path/to/layer2/diff` |
| `kento-state` | Path to the upper/work directory | `/home/user/.local/share/kento/my-ct` |
| `kento-mode` | Mode used at create time | `lxc`, `pve`, or `vm` |
| `kento-name` | Human-readable container name | `my-ct` |
| `kento-port` | Host:guest port mapping (VM only) | `10022:22` |
| `kento-qemu-pid` | QEMU process ID (VM, when running) | `12345` |
| `kento-virtiofsd-pid` | virtiofsd process ID (VM, when running) | `12344` |

These files are managed by kento. You shouldn't need to edit them, but
they're plain text if you need to inspect them.

## Container directories

```
/var/lib/lxc/<name>/       # LXC mode (name = container name)
/var/lib/lxc/<VMID>/       # PVE mode (VMID = numeric ID)
/var/lib/kento/vm/<name>/  # VM mode
```

In PVE mode, the directory is named by VMID, not by container name.
The `kento-name` file maps back to the human-readable name. All kento
commands accept the container name.
