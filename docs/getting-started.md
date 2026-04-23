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

## Pass-through flags

Kento exposes a short list of first-class flags (`--memory`, `--cores`,
`--network`, `--mac`, `--ssh-key`, ...). For everything else — a QEMU
device kento doesn't know about, a PVE config key kento doesn't set —
two escape-hatch flags let you inject raw config without waiting for a
kento release.

### `--qemu-arg` (VM modes only)

Appends a verbatim argument to the QEMU argv. Repeatable. Plain VM and
pve-vm both honour it. QEMU reads flags left-to-right and the last
occurrence wins, so pass-through args override kento's own defaults
(e.g. `--qemu-arg '-m 2048'` raises the memory size past the
`--memory` default).

```
sudo kento vm create <image> --qemu-arg '-device virtio-rng-pci'
sudo kento vm create <image> --qemu-arg '-cpu host' --qemu-arg '-smp 4'
```

> **pve-vm whitespace caveat:** PVE's `qm` tokenizes its `args:` line
> with plain whitespace splitting (no shell-quoting), so a single
> `--qemu-arg` value that itself contains a space will be rejected at
> start time. Split it into two flags:
>
> ```
> # Wrong (fails at start under pve-vm):
> --qemu-arg '-device virtio-rng-pci,rng=rng0'
>
> # Right:
> --qemu-arg '-device' --qemu-arg 'virtio-rng-pci,rng=rng0'
> ```
>
> Plain VM has no such limit — kento's own argv parser is shell-split.
> The whitespace error only fires on pve-vm.

Kento rejects args that would collide with flags it manages itself
(`-kernel`, `-initrd`, virtiofs `-chardev`, the memfd memory backend,
and `-serial` / `-chardev` reserved for future VM-interactive work).
Error message points at the offending needle.

### `--pve-arg` (PVE modes only)

Appends a verbatim line to the generated PVE config (`<VMID>.conf` for
pve-lxc, the qemu-server config for pve-vm). Repeatable. Not valid on
plain LXC or plain VM — kento errors at create time with a pointer to
the right alternative.

```
sudo kento lxc create <image> --pve --pve-arg 'tags: kento-test'
sudo kento lxc create <image> --pve --pve-arg 'onboot: 1' --pve-arg 'startup: order=2'
```

PVE's config parsers honour the last assignment of a repeated key, so
a `--pve-arg` line will override a kento-generated line with the same
key.

Kento rejects pass-through values that would clobber keys it manages
(`rootfs:`, `mp0:`, `lxc.rootfs.path`, `arch:`, `hostname:`).

### Storage and scrub behaviour

Both flag lists are stored in the instance directory alongside the
other metadata — `<instance_dir>/kento-qemu-args` and
`<instance_dir>/kento-pve-args`, one entry per line. They're preserved
by `kento scrub` (scrub only rebuilds layers and the hook script; the
pass-through files are left untouched) and surfaced in `kento info
--verbose`:

```
$ sudo kento info my-vm --verbose
Name: my-vm
Mode: vm
...
Pass-through flags:
  --qemu-arg:
    -device virtio-rng-pci
    -cpu host
```

The same data is included under the `qemu_args` / `pve_args` keys of
`kento info --json`.

## Next steps

- [Modes](modes.md) — understand lxc vs pve-lxc vs vm vs pve-vm modes
- [VM Mode](vm-mode.md) — boot OCI images as QEMU VMs
- [Instance Lifecycle](container-lifecycle.md) — naming, state, scrub behavior
- [Troubleshooting](troubleshooting.md) — common errors and fixes
