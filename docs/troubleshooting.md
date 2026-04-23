# Troubleshooting

## Error messages

### "Error: must run as root"

All kento commands require root privileges. Run with `sudo`:

```
sudo kento lxc create <image>
```

### "Error: image not found: \<image\>"

Podman can't find the image in its local store. Make sure you've
pulled it first:

```
podman pull <image>
```

If you're running kento via `sudo`, kento queries the invoking user's
podman store (not root's). Pull the image as your normal user, not as
root.

### "Error: instance name already taken: \<name\>"

An instance with that name already exists. Choose a different name or
remove the existing instance:

```
sudo kento rm <name>
```

### "Error: no instance named '\<name\>'"

No kento-managed instance matches that name. Check available
instances:

```
sudo kento list
```

Namespace-specific variants ("no lxc named ...", "no vm named ...")
point at `kento lxc list` or `kento vm list` respectively.

### "Error: instance already exists: \<id\>"

The underlying directory already exists. This can happen if a previous
removal was interrupted. Manually check and clean up:

```
ls /var/lib/lxc/<id>/         # LXC/PVE
ls /var/lib/kento/vm/<id>/    # VM
```

### "Error: instance is running. Stop it first: kento stop \<name\>"

Scrub requires the instance to be stopped. Stop it first:

```
sudo kento shutdown <name>
```

### "Error: VMID must be >= 100"

PVE requires VMIDs to be 100 or greater. Use a valid VMID:

```
sudo kento lxc create <image> --pve --vmid 200
```

Or omit `--vmid` to let kento auto-assign one.

### "Error: VMID \<N\> is already in use"

Another PVE instance already uses that VMID. Omit `--vmid` for
auto-assignment, or check used IDs:

```
cat /etc/pve/.vmlist
```

### "Error: --vmid cannot be used with \<MODE\> mode"

The `--vmid` flag only works with PVE modes. Remove `--vmid` or add
`--pve`.

### "Error: --qemu-arg is not supported for LXC/PVE-LXC"

`--qemu-arg` appends verbatim flags to the QEMU argv, so it only
applies to VM modes (plain `vm` and `pve-vm`). LXC modes never invoke
QEMU. For PVE-LXC config pass-through use `--pve-arg` instead; raw
config pass-through for plain LXC is not yet implemented (tracked as
the future `--lxc-config` flag).

```
sudo kento vm create <image> --qemu-arg '-device virtio-rng-pci'
```

### "Error: --pve-arg is not supported for plain LXC"

`--pve-arg` appends lines to the PVE qm/lxc config, which only exists
on PVE hosts. Either run on a PVE host (kento will auto-detect and use
pve-lxc), force it with `--pve`, or drop the flag. Plain-LXC raw config
pass-through is a separate, not-yet-implemented feature (`--lxc-config`).

### "Error: --pve-arg is not supported for plain VM"

Same cause as above but for VM mode: `--pve-arg` only applies under
pve-vm. Run on a PVE host (or add `--pve`) to land under pve-vm, or
drop the flag.

### "Error: --pve-arg requires PVE mode but --no-pve was specified"

`--pve-arg` and `--no-pve` are mutually exclusive. Drop one.

### "Error: kento manages '\<needle\>' directly"

One of the pass-through flags (`--qemu-arg` or `--pve-arg`) has a value
that collides with a flag kento manages itself. The denylists are
deliberately short — they only cover things kento writes itself and
that would silently conflict:

- QEMU: `-kernel`, `-initrd`, `virtiofs`, `rootfs`,
  `memory-backend-memfd`, `memfd-size`, `-chardev`, `-serial`.
- PVE: `rootfs:`, `mp0:`, `lxc.rootfs.path`, `arch:`, `hostname:`.

Most of these have a dedicated kento flag already (`--memory` covers
the memfd size; `--ip` / `--network` covers network keys). If you have
a real need to override one of the denylisted items, file an issue —
the denylist is the escape hatch's one restraint, not a policy
statement.

### "kento-qemu-args line contains whitespace which qm does not tokenize safely"

PVE's `qm` splits the `args:` line on whitespace with no shell-quoting,
so a single `--qemu-arg` value that contains a space would get split
across two QEMU flags at boot. Kento refuses to pass that through
silently. Split the argument yourself:

```
# Instead of:
--qemu-arg '-device virtio-rng-pci,rng=rng0'

# Use:
--qemu-arg '-device' --qemu-arg 'virtio-rng-pci,rng=rng0'
```

Only pve-vm is affected — plain VM's argv is shell-split and handles
embedded whitespace correctly.

### "kento-hook: error: virtiofsd not found"

VM mode requires virtiofsd. Install it:

```
sudo apt install virtiofsd        # Debian/Ubuntu
sudo dnf install virtiofsd        # Fedora
```

On Debian, virtiofsd installs to `/usr/libexec/virtiofsd` which is not
in `$PATH`. Kento searches this location automatically.

### "kento-hook: error: kernel not found" / "kento-hook: error: initramfs not found"

The OCI image is missing boot files required for VM mode:

- `/boot/vmlinuz` — Linux kernel
- `/boot/initramfs.img` — initial ramdisk

These must be baked into the OCI image at build time. See
[VM Mode](vm-mode.md) for image requirements.

### "Error: rootfs already mounted"

The rootfs mount was not cleaned up from a previous start. Stop the
instance to unmount:

```
sudo kento stop <name>
```

If stop doesn't work (e.g., PID files are stale), unmount manually:

```
sudo umount /var/lib/kento/vm/<name>/rootfs
```

### "Error: VM \<name\> is already running"

The VM is already started. Stop it before starting again:

```
sudo kento vm stop <name>
```

## VM boot issues

If the VM starts (QEMU launches) but you can't reach it via SSH:

### 1. Check fstab

Disk-based images often have `PARTUUID=...` entries in `/etc/fstab`
that stall the boot waiting for block devices that don't exist under
virtiofs. Either empty the fstab or add `nofail,x-systemd.device-timeout=1s`
to each block-device entry:

```
# Check inside the composed rootfs (while the instance is stopped):
sudo cat /var/lib/kento/vm/<name>/rootfs/etc/fstab
```

Kento does not patch this at runtime — see the
[image contract](image-contract.md) for what the image must provide.

### 2. Check network configuration

The VM gets a virtio NIC named `en*`. Without DHCP configuration, it
has no IP address and SSH is unreachable. Check for a systemd-networkd
config:

```
ls /var/lib/kento/vm/<name>/rootfs/etc/systemd/network/
```

See [VM Mode](vm-mode.md) for the required network configuration.

### 3. Check user accounts

Locked accounts (`*` or `!` in `/etc/shadow`) can't log in:

```
sudo grep -E '^(root|myuser):' /var/lib/kento/vm/<name>/rootfs/etc/shadow
```

A `*` or `!` after the username means the account is locked.

### 4. Check SSH server

Make sure sshd is installed and enabled in the image:

```
ls /var/lib/kento/vm/<name>/rootfs/usr/sbin/sshd
```

## Overlay mount failures

### "mount: invalid argument" or similar

This usually means util-linux is too old. Kento requires version 2.39+
for the `LIBMOUNT_FORCE_MOUNT2` environment variable, which forces the
old `mount(2)` syscall that supports colon-separated multi-path
`lowerdir` for overlayfs.

Check your version:

```
mount --version
```

### Layer paths gone stale

If podman removes or reorganizes image layers (e.g., after `podman
image prune`), the pre-resolved paths in `kento-layers` become invalid.
The hook script will report missing layer paths at instance start.

Fix by scrubbing:

```
sudo kento scrub <name>
```

This re-resolves layers from the current podman store.
