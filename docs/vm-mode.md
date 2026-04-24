# VM Mode

VM mode boots OCI images as full QEMU virtual machines using virtiofs
to share the composed rootfs. Unlike LXC/PVE modes, the guest runs its
own kernel — useful for workloads that need a real kernel, custom
modules, or full hardware isolation.

## Gemet

VM mode relies on [gemet](https://github.com/doctorjei/gemet), a
kento subproject that provides a minimal Linux kernel and initramfs
for booting OCI images as VMs. Gemet's initramfs mounts a virtiofs
share as the root filesystem and calls `switch_root` into `/sbin/init`
— that's it. The kernel and initramfs are baked into the OCI image at
`/boot/vmlinuz` and `/boot/initramfs.img`.

Kento handles the lifecycle (overlayfs, virtiofsd, QEMU); gemet
provides the boot payload.

## Requirements

In addition to the [standard prerequisites](getting-started.md), VM mode
requires:

- **QEMU** (`qemu-system-x86_64`) with KVM support
- **virtiofsd** — on Debian, installed at `/usr/libexec/virtiofsd` (not
  in `$PATH`). Kento searches fallback locations automatically.

Verify KVM is available:

```
ls /dev/kvm
```

## OCI image requirements

VM mode is stricter about what the OCI image contains. The image must
have:

### Kernel and initramfs

```
/boot/vmlinuz
/boot/initramfs.img
```

These are passed directly to QEMU via `-kernel` and `-initrd`. Without
them, kento will refuse to start the VM.

### /etc/fstab must not hard-depend on absent devices

Disk-based images often have `PARTUUID=...` entries that cause boot
to hang 90s waiting for a block device that doesn't exist in a
virtiofs-backed VM. The fstab should be empty, or every block-device
entry should use `nofail,x-systemd.device-timeout=1s` to fail
gracefully:

```
UUID=abc-123  /boot/efi  vfat  defaults,nofail,x-systemd.device-timeout=1s  0  2
```

This is part of the broader [image contract](image-contract.md) —
kento does not patch images at runtime. If an image hangs on fstab,
the image violates the contract.

### Network configuration

The VM gets a virtio NIC named `en*`. Configure DHCP via
systemd-networkd:

```ini
# /etc/systemd/network/80-dhcp.network
[Match]
Name=en*

[Network]
DHCP=yes
```

Enable the service:

```
systemctl enable systemd-networkd
```

Without network configuration, the VM boots but has no connectivity —
SSH will be unreachable.

### User account with login access

The image needs at least one user account with a password or SSH keys
set. Accounts created with `useradd` without `-p` are locked by
default (`root:*` or `user:!` in `/etc/shadow`) — locked accounts
cannot log in via console or SSH.

Set a password in the image build:

```dockerfile
RUN echo 'myuser:mypassword' | chpasswd
RUN usermod -aG sudo myuser
```

## Creating a VM instance

```
sudo kento vm create <image> [--name <name>] [--port H:G]
```

VM mode is selected by using the `vm` noun. On a PVE host, this
automatically uses pve-vm mode; use `--no-pve` to force plain vm mode.

### Static IP

The `--ip` flag works for VM mode too, injecting a static network
configuration into the guest:

```
sudo kento vm create <image> --ip 192.168.1.50/24 --gateway 192.168.1.1
```

### Memory and CPU

Use `--memory` and `--cores` to override the defaults:

```
sudo kento vm create <image> --memory 1024 --cores 2
```

### Port forwarding

By default, kento allocates the next free host port starting from 10022
and forwards it to guest port 22 (SSH). The allocation scans existing
`kento-port` files to avoid conflicts.

Override with `--port`:

```
sudo kento vm create myimage --port 2222:22
```

## Starting and connecting

```
sudo kento vm start my-vm
```

The start sequence:

1. Mounts overlayfs at `<dir>/rootfs` using OCI layers + writable upper
2. Validates `/boot/vmlinuz` and `/boot/initramfs.img` exist in rootfs
3. Starts virtiofsd (shares rootfs via Unix socket)
4. Starts QEMU with the kernel, initramfs, and virtiofs root device
5. Writes PID files and port mapping

After start, kento prints the SSH command:

```
Started: my-vm
  SSH: ssh -p 10022 root@localhost
```

The VM typically becomes reachable via SSH within 10-15 seconds.

### SSH access

```
ssh -p <host-port> <user>@localhost
```

Or with password auth:

```
sshpass -p <password> ssh -o StrictHostKeyChecking=no -p <host-port> <user>@localhost
```

## Stopping

```
sudo kento vm stop my-vm
```

Sends SIGTERM to QEMU and virtiofsd, waits for them to exit (with
SIGKILL fallback after 5 seconds), then unmounts the rootfs.

## Troubleshooting

### VM starts but SSH is unreachable

Check in order:

1. **Network config** — is systemd-networkd configured and enabled?
2. **fstab** — does it have PARTUUID entries? These stall the boot.
3. **User account** — is the password locked? Check `/etc/shadow`.
4. **SSH server** — is sshd installed and enabled?

See [Troubleshooting](troubleshooting.md) for details on each check
and for error message reference.

## Runtime layout

```
/var/lib/kento/vm/<name>/
├── rootfs/              # Overlayfs mount point (mounted at start)
├── upper/               # Writable layer
├── work/                # Overlayfs workdir
├── kento-image          # OCI image name
├── kento-layers         # Pre-resolved layer paths
├── kento-state          # Path to writable layer directory
├── kento-mode           # "vm"
├── kento-name           # Instance name
├── kento-port           # Host:guest port mapping (e.g., "10022:22")
├── kento-qemu-pid       # QEMU PID (present when running)
├── kento-virtiofsd-pid  # virtiofsd PID (present when running)
└── virtiofsd.sock       # virtiofsd Unix socket (present when running)
```
