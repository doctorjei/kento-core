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

## Your first instance

All kento commands require root. Run with `sudo`.

### 1. Pull an OCI image

```
sudo kento pull docker.io/library/debian:12
```

`kento pull` wraps `podman pull` and ensures the image lands in the root
store where kento can find it.

> **Note:** You can skip this step if the image is already in podman's
> store. `create` will use it directly.

### 2. Create an instance

```
sudo kento lxc create docker.io/library/debian:12 --name my-first
```

Kento inspects the image, resolves the layer paths from podman's store,
and writes the LXC config and hook script. No image data is copied.

### 3. Start the instance

```
sudo kento lxc start my-first
```

### 3.5. Check instance info

```
sudo kento info my-first
```

Shows the instance's image, mode, status, and metadata.

### 4. Attach to the instance

For LXC mode:

```
sudo lxc-attach -n my-first
```

You're now inside a full system container running systemd (LXC mode).
For VM mode, connect via SSH instead.

### 5. Stop the instance

```
sudo kento lxc shutdown my-first
```

(`stop` also works as an alias.)

### 6. Scrub to clean state

```
sudo kento lxc scrub my-first
```

This clears all writable changes and re-resolves image layers from
podman. The instance returns to a pristine state matching the OCI
image.

### 7. Remove the instance

```
sudo kento lxc destroy my-first
```

Removes the instance, its config, hook, and writable layer. (`rm` also
works as an alias.)

> **Tip:** `sudo kento lxc run docker.io/library/debian:12 --name my-first`
> combines create and start in one step.

## List instances

```
sudo kento list
sudo kento ls
```

Shows all kento-managed instances across all modes (lxc, pve-lxc, vm,
pve-vm) with their name, image, status, mode, and writable layer size.

## Next steps

- [Modes](modes.md) — understand lxc vs pve-lxc vs vm vs pve-vm modes
- [VM Mode](vm-mode.md) — boot OCI images as QEMU VMs
- [Instance Lifecycle](container-lifecycle.md) — naming, state, scrub behavior
- [Troubleshooting](troubleshooting.md) — common errors and fixes
