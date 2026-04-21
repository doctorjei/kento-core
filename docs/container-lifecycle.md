# Instance Lifecycle

## Create

```
sudo kento lxc create <image> [--name <name>]
sudo kento vm create <image> [--name <name>]
```

The create command:

1. Resolves OCI image layers from podman's store
2. Creates the instance directory with metadata files
3. Generates a mount hook script (LXC/PVE modes)
4. Writes the LXC or PVE config (LXC/PVE modes)
5. Allocates a port and writes `kento-port` (VM mode)

No image data is copied. Kento reads podman's layer store directly —
the OCI store IS the layer store.

### Auto-naming

If `--name` is omitted, kento generates a name from the image reference.
Names are unique across all modes (LXC and VM namespaces):

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

With `--name`, the name is used as-is. If an instance with that name
already exists, kento exits with an error.

### The `--start` flag

Pass `--start` to start the instance immediately after creation:

```
sudo kento lxc create <image> --name my-ct --start
```

## Run (create + start)

```
sudo kento lxc run <image> [--name <name>]
sudo kento vm run <image> [--name <name>]
```

Equivalent to `create --start`. Accepts all the same flags as `create`
except `--start` (which is implicit).

## Start

```
sudo kento start <name>
```

Behavior depends on the mode recorded at create time. You can also use
`kento lxc start` or `kento vm start` explicitly:

- **LXC:** runs `lxc-start -n <name>`
- **PVE:** runs `pct start <VMID>`
- **VM:** mounts overlayfs, starts virtiofsd + QEMU, writes PID files

## Shutdown / stop

```
sudo kento shutdown <name>
sudo kento stop <name>
```

`shutdown` is the primary command; `stop` is an alias. Pass `-f` / `--force`
to force an immediate stop (kill).

- **LXC:** runs `lxc-stop -n <name>`
- **PVE:** runs `pct stop <VMID>`
- **VM:** sends SIGTERM to QEMU and virtiofsd, waits for exit (SIGKILL
  fallback after 5s), unmounts rootfs

## Scrub

```
sudo kento scrub <name>
```

Scrubs an instance back to a clean state matching the OCI image:

1. Checks the instance is stopped (refuses if running)
2. Unmounts rootfs if still mounted
3. Deletes and recreates the writable upper and work directories
4. Re-resolves image layers from podman (picks up image updates)
5. Regenerates the hook script (LXC/PVE modes)

Use scrub when:

- You want to discard all changes and start fresh
- You've updated the OCI image and want the instance to use new layers
- Layer paths have gone stale (podman reorganized its store)

## Destroy / rm

```
sudo kento destroy <name>
sudo kento rm <name>
sudo kento destroy -f <name>
```

`destroy` is the primary command; `rm` is an alias. Removes an instance
completely. If the instance is running, kento refuses unless
`-f` / `--force` is passed. With `--force`:

1. Stops the instance
2. Unmounts rootfs
3. Releases the OCI image mount (LXC/PVE modes)
4. Removes the state directory (writable layer)
5. Removes the instance directory
6. Deletes the PVE config (PVE mode)

This is irreversible. All writable state is lost.

## Info / inspect

```
sudo kento info <name>
sudo kento inspect <name>
```

Shows instance metadata: image, mode, status, directory paths, network
config, layer count, and creation time. `inspect` is an alias for `info`.

Pass `--json` for machine-readable output. Pass `-v` / `--verbose` to
include layer sizes and individual layer paths.

## Sudo and user storage

When kento is run via `sudo`, it detects the invoking user from
`SUDO_USER` and stores the writable layer in
`~user/.local/share/kento/<name>/` instead of under the instance
directory.

Podman images are always resolved from the root store
(`/var/lib/containers/storage/`). Pull images as root:

```
sudo kento pull <image>
```

The `kento-state` file records which path was used, so scrub and destroy
work correctly regardless of how you run them later.

## Runtime files

Each instance directory contains metadata files:

| File | Contents | Example |
|------|----------|---------|
| `kento-image` | OCI image reference | `docker.io/library/debian:12` |
| `kento-layers` | Colon-separated layer paths from podman | `/path/to/layer1/diff:/path/to/layer2/diff` |
| `kento-state` | Path to the upper/work directory | `/home/user/.local/share/kento/my-ct` |
| `kento-mode` | Mode used at create time | `lxc`, `pve-lxc`, `vm`, or `pve-vm` |
| `kento-name` | Human-readable instance name | `my-ct` |
| `kento-port` | Host:guest port mapping (VM only) | `10022:22` |
| `kento-qemu-pid` | QEMU process ID (VM, when running) | `12345` |
| `kento-virtiofsd-pid` | virtiofsd process ID (VM, when running) | `12344` |

These files are managed by kento. You shouldn't need to edit them, but
they're plain text if you need to inspect them.

## Instance directories

```
/var/lib/lxc/<name>/       # lxc mode (name = instance name)
/var/lib/lxc/<VMID>/       # pve-lxc mode (VMID = numeric ID)
/var/lib/kento/vm/<name>/  # vm / pve-vm mode
```

In PVE mode, the directory is named by VMID, not by instance name.
The `kento-name` file maps back to the human-readable name. All kento
commands accept the instance name.
