# Architecture

This document explains how kento works under the hood.

## Design principles

- **Zero pip dependencies** — stdlib-only Python (argparse, json,
  subprocess, pathlib, shutil, pwd)
- **Hook is pure shell** — no Python runtime needed at instance start
- **Podman is the only runtime dependency** (for OCI layer storage)
- **Don't duplicate layers** — read podman's store directly
- **Pre-resolve at create time** — the hook must be fast and
  self-contained
- **Per-instance hooks** — each instance gets its own script with
  baked-in paths

## Overlayfs layering

Kento composes OCI image layers using Linux overlayfs. Each OCI layer
becomes a read-only `lowerdir`, and a writable `upperdir` captures all
changes.

```
┌─────────────────────┐
│   upperdir (rw)     │  Writable layer — all changes go here
├─────────────────────┤
│   OCI layer N       │  ↑ Topmost image layer
│   OCI layer N-1     │  │
│   ...               │  │ Read-only (lowerdir)
│   OCI layer 1       │  │
│   OCI layer 0       │  ↓ Base layer
└─────────────────────┘
```

The layers come directly from podman's overlay storage at
`/var/lib/containers/storage/overlay/<hash>/diff` (root store). Kento never
copies image data.

### The mount workaround

Kernel 6.x's `mount(8)` uses `fsconfig(2)`, which rejects
colon-separated multi-path `lowerdir` strings for overlayfs. Kento
works around this:

```bash
export LIBMOUNT_FORCE_MOUNT2=always
mount -t overlay overlay -o "lowerdir=...,upperdir=...,workdir=..." "$rootfs"
```

`LIBMOUNT_FORCE_MOUNT2=always` forces the old `mount(2)` syscall path,
which supports long option strings. This is scoped per-process via
`getenv()` — no system-wide side effects. Requires util-linux 2.39+
(May 2023).

The old `mount(2)` syscall has a 4096-byte option string limit (vs
256-byte per-option in the new API), which is actually better for long
lowerdir strings with many layers.

## Two components

### 1. kento CLI (Python)

The management tool. Handles create, run, pull, start, shutdown, scrub,
destroy, info, and list. Runs as root.

Key operations at create time:

- Queries podman for image layer paths (`podman image inspect`)
- Creates the instance directory and metadata files
- Generates a per-instance hook script with baked-in layer paths
- Writes the LXC or PVE config

### 2. kento-hook (shell script)

A per-instance shell script generated at create time, stored at
`<instance-dir>/kento-hook`. Called by LXC at instance start and stop.

The hook:

- Validates that all layer paths still exist
- Mounts overlayfs at the rootfs path
- Unmounts on instance stop

The hook is pure POSIX shell with no dependencies beyond `mount` and
`mountpoint`. It runs in LXC's restricted mount namespace where podman
can't initialize its storage driver — that's why layer paths are
pre-resolved at create time rather than looked up at start time.

## Mode-specific startup sequences

### LXC mode

1. `lxc-start -n <name>` reads the config at `<dir>/config`
2. The hook fires (`pre-start`) — mounts overlayfs at `<dir>/rootfs`
3. Instance boots with systemd as PID 1

### PVE mode

1. `pct start <VMID>` triggers PVE's LXC machinery
2. PVE generates LXC config with hardcoded
   `lxc.rootfs.path = /var/lib/lxc/<VMID>/rootfs`
3. `lxc-pve-prestart-hook` runs (harmless no-op for kento instances)
4. Kento's hook fires (`pre-mount`) — mounts overlayfs at
   `$LXC_ROOTFS_PATH`
5. LXC bind-mounts the now-populated rootfs to `$LXC_ROOTFS_MOUNT`
6. Instance boots with systemd

The hook uses `$LXC_ROOTFS_PATH` (the source path) for the mount
target, not `$LXC_ROOTFS_MOUNT`. LXC bind-mounts the source to the
final location afterwards.

PVE uses hook version 0 (not 1). `$LXC_HOOK_TYPE` is empty; the hook
type comes from `$3`. The hook handles both formats:
`${LXC_HOOK_TYPE:-$3}`.

### VM mode

1. `kento vm start <name>` mounts overlayfs at `<dir>/rootfs`
   on the host (no hook -- this is done directly by the CLI)
2. Validates `/boot/vmlinuz` and `/boot/initramfs.img` exist in rootfs
3. Starts virtiofsd, sharing the rootfs via a Unix socket
4. Starts QEMU with `-kernel` and `-initrd` from the rootfs, virtiofs
   as the root device
5. Writes PID files (`kento-qemu-pid`, `kento-virtiofsd-pid`)

The guest kernel mounts the virtiofs share as its root filesystem. The
kernel command line is:

```
console=ttyS0 rootfstype=virtiofs root=rootfs
```

## Sudo-aware storage

When kento is run via `sudo`, it detects `SUDO_USER` and splits
storage:

- **Instance directory** (`/var/lib/lxc/<name>/` or
  `/var/lib/kento/vm/<name>/`) — owned by root, contains config,
  hook, metadata, and rootfs mountpoint
- **State directory** (`~user/.local/share/kento/<name>/`) — owned
  by the invoking user, contains the writable upper and work dirs

Podman images are always resolved from the store matching the
effective UID. When running as root, this is the root store
(`/var/lib/containers/storage/`). Kento does not cross between
podman's rootful and rootless stores.

The `kento-state` file records the state directory path so all
commands work regardless of who runs them.

### `KENTO_STATE_DIR` override

Setting `KENTO_STATE_DIR` overrides the default base for the writable
layer and takes precedence over sudo-user detection. A leading `~` is
expanded. This is useful when the default path sits on an overlayfs
(e.g. nested LXC where the outer rootfs is itself overlay) — the kernel
refuses overlay-on-overlay as an upperdir. Point it at a tmpfs or plain
filesystem instead:

```
sudo KENTO_STATE_DIR=/tmp/kento-state kento lxc create ...
```

## PVE cluster filesystem quirks

PVE's cluster filesystem (`/etc/pve`) is a FUSE mount with non-standard
behavior:

- `stat()` returns `ENOENT` on empty virtual directories
- `mkdir()` returns `EEXIST` on those same directories
- `os.makedirs()` / `Path.mkdir(parents=True)` both fail

Kento's `write_pve_config()` handles this by creating each directory
level individually with `FileExistsError` handling.

Config files must be written to `/etc/pve/nodes/<hostname>/lxc/`, not
`/etc/pve/lxc/` — the latter is a read-only virtual aggregate view.
