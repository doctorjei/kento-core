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

### "Error: plain LXC mode requires '--unconfined' due to the systemd 256+ credentials bug"

Modern systemd (256+, shipped in Debian 13 and equivalent) adds
`ImportCredential=` directives to stock units. These require a
credentials tmpfs mount under `/run/credentials/`, which the default
LXC AppArmor profile (`lxc-container-default-with-nesting`) denies.

In plain-LXC mode the result is that systemd-journald,
systemd-tmpfiles-setup, systemd-networkd, systemd-logind all exit
with `status=243/CREDENTIALS`, DHCP never acquires an IPv4 lease,
and the container is severely degraded even though it technically
"started."

PVE-LXC is not affected: `pct` sets `apparmor.profile = generated`,
which builds a per-container profile where in-container processes
run effectively unconfined (host boundary still enforced). Kento
cannot replicate `generated` on plain LXC without custom
apparmor_parser plumbing.

For plain LXC today the only fix is to drop AppArmor confinement
entirely. Kento gates this behind an explicit flag:

```
sudo kento lxc create --unconfined <image>
```

This adds to the LXC config:

```
lxc.include = /usr/share/lxc/config/common.conf
lxc.apparmor.profile = unconfined
lxc.apparmor.allow_nesting = 1
```

`unconfined` removes a layer of defense-in-depth — do not use it for
untrusted workloads. Alternatives that keep confinement:

- `kento lxc create --pve <image>` — PVE-LXC mode, AppArmor-confined
  at the host boundary via `apparmor.profile = generated`
- `kento vm create <image>` — VM mode, isolation via QEMU instead of
  AppArmor

### "Error: --unconfined is only for plain LXC"

`--unconfined` is a workaround for a plain-LXC-only issue. PVE-LXC
already works via `apparmor.profile = generated`, and VM modes don't
use AppArmor at all. Drop the flag if you're on a PVE host or using
`kento vm`.

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
