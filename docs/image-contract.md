# Image Contract

Kento composes OCI images into LXC containers and QEMU VMs. It runs
images in environments they may not have been built for: layered
overlayfs rootfs, virtiofs-backed VM disk, no bare-metal block devices,
partial hardware, unprivileged namespaces. Images must be designed to
boot in these environments without undue friction.

This document defines what "kento-compatible" means for an OCI image.

## Principle

**The system must not create hard synchronous dependencies on runtime
resources that may not exist. Optional dependencies must fail
gracefully.**

Concretely, if the image expects a mount, a device, a service, an
interface, or any other runtime resource that is not guaranteed to
exist in kento's environment, that expectation must be expressed in a
way that does not block boot or produce misleading failures.

This principle is checkable. Most of what follows is the principle
applied to specific subsystems.

## What kento does

- **Composes layers** via overlayfs from the OCI image's podman store.
- **Runs the image** as an LXC container or QEMU VM.
- **Injects user-intent configuration** at create/start time (hostname,
  IP, DNS, SSH keys, timezone, env vars) via one of two paths:
  - **Direct file injection** — writes config files into the rootfs
    before init starts (default for minimal images).
  - **Cloud-init seed** — generates a NoCloud seed (ISO for VM, files
    under `/var/lib/cloud/seed/nocloud/` for LXC) if the image has
    cloud-init. [Phase 3.5, planned.]
  - Auto-detected per-image; `--config-mode` overrides.
- **Creates mount point directories** for PVE `mp[n]` bind mounts
  (runtime adaptation to a mount kento introduces, not image patching).

## What kento does NOT do

- **Does not sanitize `/etc/fstab`**, mask systemd units, or otherwise
  patch image content at runtime. If the image has hard dependencies
  that fail in kento's environment, the image violates this contract.
- **Does not create users** implied by flags. `--ssh-key-user droste`
  assumes the `droste` user exists in the image.
- **Does not install packages** to satisfy its injections. If the image
  lacks `tzdata`, timezone injection produces a broken `/etc/localtime`
  symlink.
- **Does not run arbitrary guest-side scripts.** `runcmd:` / `bootcmd:`
  are not exposed by kento. Use cloud-init directly (user-provided
  user-data) if you need this.

## Specific expectations

### Mount units (fstab and .mount)

Entries referencing block devices that may not exist at runtime must
use soft-fail semantics:

```fstab
UUID=abc-123  /boot/efi  vfat  defaults,nofail,x-systemd.device-timeout=1s  0  2
/dev/sda2     /data      ext4  defaults,nofail,x-systemd.device-timeout=1s  0  2
```

Virtual filesystems (`tmpfs`, `proc`, `sysfs`, `devpts`) are fine as-is;
they have no external dependency.

Equivalent for `.mount` units: use `DefaultDependencies=no` and avoid
`RequiresMountsFor=`; use `WantsMountsFor=` for optional mounts.

**Without these flags**, `systemd-fstab-generator` creates `.device`
unit dependencies. `local-fs.target` waits on them. 90 seconds later,
boot fails or enters emergency mode. Kento will not patch around this.

### Services

Services must not create hard dependencies on hardware or filesystems
that may not appear:

- Prefer `Wants=` over `Requires=` for optional dependencies.
- Use `ConditionPathExists=` or `ConditionDirectoryNotEmpty=` to skip
  services cleanly when prerequisites are absent.
- Avoid `BindsTo=` on device units unless the device is guaranteed.
- `network-online.target` should not be blocked indefinitely by
  `systemd-networkd-wait-online.service` — use `--any --timeout=<N>`.

### Networking

- Match network configs by stable selectors, not by interface name:
  ```ini
  [Match]
  Type=ether
  ```
  Not:
  ```ini
  [Match]
  Name=eth0
  ```
  Interface names differ between LXC (often `eth0`) and VM mode
  (often `enp0s2` or similar, depending on QEMU machine type).

- DHCP as default (image-level) is supported. Kento overrides via
  injection of a `10-static.network` file (lexically first, wins over
  any `50-`/`80-`/etc. image defaults).

- If the image ships a DHCP `.network` file, its numeric prefix must
  be greater than `10-` (so kento's static config wins when injected).
  A prefix of `80-dhcp.network` is safe; `05-dhcp.network` is not.

### Hostname

`/etc/hostname` is read at boot by any sensible init system. Kento
writes this file directly (injection mode) or via cloud-init
(`hostname:` module).

### Timezone

Kento sets timezone via `/etc/localtime` (symlink into
`/usr/share/zoneinfo/`) and `/etc/timezone`, and additionally sets
`TZ=<zone>` in `/etc/environment` for app-tier portability (docker-style
consumers honor `$TZ`).

Requires:
- `tzdata` package present (for zoneinfo files).
- `pam_env` in the login stack (for `/etc/environment` to reach sessions).

### SSH

- `openssh-server` installed.
- `PubkeyAuthentication yes` in `sshd_config` (default in most distros).
- sshd must not regenerate host keys on every boot. Standard Debian /
  Ubuntu behavior: `ssh-keygen -A` or equivalent runs once, keys
  persist.
- For `--ssh-key-user NAME`: user `NAME` exists with a home directory
  the rootfs.

### Environment variables

`/etc/environment` is read by `pam_env` at login (sshd, getty, su).
Services do not inherit it — services that need env get it from their
unit file (`Environment=` / `EnvironmentFile=`).

### Subsystems for injection mode

When kento is in `injection` mode (the default for non-cloud-init
images), these subsystems must be present:

- `systemd-networkd` (for `--ip` / `--gateway` / `--dns` via `.network` file)
- `systemd-resolved` (for `--dns` without `--ip`, via
  `/etc/systemd/resolved.conf.d/` drop-in). `/etc/resolv.conf` should
  be managed by resolved (symlink to `stub-resolv.conf` or
  `resolv.conf`).
- `openssh-server` (for `--ssh-key`, `--ssh-host-keys`)
- `tzdata` (for `--timezone`)

### Subsystems for cloud-init mode

When kento is in `cloudinit` mode (auto-detected or forced), the image
ships `cloud-init` and its services are enabled. Kento stays out of the
rootfs — only the NoCloud seed (ISO or seed directory files) is
provided. Everything kento would inject in injection mode is instead
expressed in the cloud-init user-data / meta-data / network-config.

### VM-mode specifics

- `/boot/vmlinuz` and `/boot/initramfs.img` baked into the image (the
  OCI image is the root disk; there is no separate boot device).
  Compose via multi-stage Containerfile:
  ```dockerfile
  FROM tenkei-kernel:<ver> AS kernel

  FROM yggdrasil:<ver>
  COPY --from=kernel /boot/vmlinuz /boot/vmlinuz
  COPY --from=kernel /boot/initramfs.img /boot/initramfs.img
  ```
- Initramfs must be able to `switch_root` to a virtiofs-backed rootfs.
  Tenkei's initramfs handles this; other initramfs implementations
  must support `root=rootfs rootfstype=virtiofs` cmdline.
- `/etc/fstab` in a VM image must follow the same rules as LXC
  (`nofail` for any block device references). Virtiofs mounts the
  rootfs; PARTUUID / UUID entries are not satisfied.

## Cloud-init detection

Kento auto-detects cloud-init at create time by scanning the composed
image lowerdir for:

- `/usr/bin/cloud-init` or `/usr/sbin/cloud-init`
- `/etc/cloud/` directory
- A `cloud-init.service` systemd unit

If any are present, `kento-config-mode` is recorded as `cloudinit`.
Otherwise, `injection`. Override with `--config-mode`.

## Common violations and symptoms

| Symptom | Probable violation |
|---|---|
| Boot hangs 90s on a dependency | fstab entry without `nofail`; systemd-fstab-generator creates a `.device` dependency |
| Boot enters emergency mode on `/boot/efi` or similar | PARTUUID/UUID fstab entry for a device not in the container/VM |
| `--ip` does nothing, DHCP wins | Image's DHCP `.network` file has a numeric prefix `<= 10-` |
| `--timezone` produces broken symlink | `tzdata` missing; `/usr/share/zoneinfo/<zone>` doesn't exist |
| `--dns` ignored when `--ip` absent | `systemd-resolved` not installed or not running; `/etc/resolv.conf` not linked to resolved's stub |
| Env vars from `--env` not in shell | `pam_env` not in the login stack; or target service doesn't inherit from a login session |
| `--ssh-key-user droste` fails | `droste` user not in image |
| VM boots but no network | Image's `.network` file matches on `Name=eth0`; VM interface is named differently (`enp0s2`, etc.) |

## Feedback loop

If kento's injections fail on your image and the image looks correct,
file against kento. If the image has hard dependencies that violate
the principle, fix the image; kento will not patch around it.

The intent is long-term stability: kento stays narrow, images carry
their own contract, and violations are visible and fixable at the
layer where they originated.
