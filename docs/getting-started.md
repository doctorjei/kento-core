# Getting Started

## Prerequisites

Kento requires a Linux host with:

- **Python 3.11+**
- **Podman** (any recent version) — for OCI image storage
- **LXC** (for LXC and PVE modes)
- **util-linux 2.39+** — for `LIBMOUNT_FORCE_MOUNT2` support (see [Troubleshooting](troubleshooting.md) if mount fails)

**VM mode** additionally requires:

- **QEMU** (`qemu-system-x86_64`)
- **virtiofsd** (often at `/usr/libexec/virtiofsd` on Debian, not in `$PATH`)

### Debian / Ubuntu

```
sudo apt install podman lxc lxc-templates python3 python3-pip
```

For VM mode:

```
sudo apt install qemu-system-x86 virtiofsd
```

### Fedora

```
sudo dnf install podman lxc python3 python3-pip
```

For VM mode:

```
sudo dnf install qemu-system-x86 virtiofsd
```

## Install

From a local checkout:

```
pip install .
```

Or with pipx for isolation:

```
pipx install .
```

This installs the `kento` command.

## Your first container

All kento commands require root. Run with `sudo`.

### 1. Pull an OCI image

```
podman pull docker.io/library/debian:12
```

### 2. Create a container

```
sudo kento container create docker.io/library/debian:12 --name my-first
```

Kento inspects the image, resolves the layer paths from podman's store,
and writes the LXC config and hook script. No image data is copied.

### 3. Start the container

```
sudo kento container start my-first
```

### 4. Attach to the container

For LXC mode:

```
sudo lxc-attach -n my-first
```

You're now inside a full system container running systemd.

### 5. Stop the container

```
sudo kento container stop my-first
```

### 6. Reset to clean state

```
sudo kento container reset my-first
```

This clears all writable changes and re-resolves image layers from
podman. The container returns to a pristine state matching the OCI
image.

### 7. Remove the container

```
sudo kento container rm my-first
```

Removes the container, its config, hook, and writable layer.

## List containers

```
sudo kento container list
sudo kento container ls
```

Shows all kento-managed containers across all modes (LXC, PVE, VM)
with their name, image, status, mode, and writable layer size.

## Next steps

- [Modes](modes.md) — understand LXC vs PVE vs VM mode
- [VM Mode](vm-mode.md) — boot OCI images as QEMU VMs
- [Container Lifecycle](container-lifecycle.md) — naming, state, reset behavior
- [Troubleshooting](troubleshooting.md) — common errors and fixes
