# Modes

Kento operates in one of four modes, each producing a running system
from the same OCI image layers.

## Noun-verb pattern

The CLI uses a noun-verb pattern to select the instance type:

- `kento lxc <cmd>` — LXC instances (lxc or pve-lxc)
- `kento vm <cmd>` — VM instances (vm or pve-vm)

The noun selects the type; PVE integration is auto-detected based on
whether `/etc/pve` exists.

## Mode detection

- `kento lxc create` on a plain host → **lxc** mode
- `kento lxc create` on a PVE host → **pve-lxc** mode
- `kento vm create` on a plain host → **vm** mode
- `kento vm create` on a PVE host → **pve-vm** mode

Override PVE auto-detection with `--pve` or `--no-pve`:

```
sudo kento lxc create <image> --pve       # Force PVE integration
sudo kento lxc create <image> --no-pve    # Disable PVE integration
sudo kento vm create <image> --no-pve     # Plain VM even on PVE host
```

The mode is recorded in `kento-mode` at create time. All subsequent
commands (start, shutdown, scrub, destroy) read this file — you don't
need to pass the flag again.

## lxc mode

Standard LXC system containers. Created with `kento lxc create` on a
non-PVE host.

- **Start command:** `lxc-start`
- **Access:** `sudo lxc-attach -n <name>`
- **Network bridge:** `lxcbr0` (override with `--network bridge=<name>`)
- **Config location:** `/var/lib/lxc/<name>/config`
- **Memory/CPU:** no limit by default (override with `--memory` / `--cores`)
- **Nesting:** disabled by default (`--allow-nesting` to permit nested containers)
- **AppArmor:** per-container profile via `lxc.apparmor.profile = generated` (see below)
- **Config pass-through:** `--lxc-arg "KEY = VALUE"` appends raw lines to
  the native `config` (plain-LXC only; on PVE use `--pve-arg`). Available
  on `create` / `run` and `kento set`.

The instance runs systemd as PID 1 in a shared kernel namespace.

### AppArmor profile

Kento emits `lxc.apparmor.profile = generated` for plain-LXC. This
is a built-in LXC feature (not PVE-specific): LXC builds a per-
container AppArmor profile that enforces the host/container boundary
but labels in-container processes `:unconfined`. That combination
avoids two modern-systemd/modern-glibc traps that would otherwise
break plain-LXC on recent OCI images:

1. `ImportCredential=` in stock systemd units (256+, Debian 13) needs
   a credentials tmpfs mount that the default
   `lxc-container-default-with-nesting` profile denies — services
   fail with `status=243/CREDENTIALS`, DHCP never acquires IPv4.
2. PAM's `unix_chkpwd` calls glibc's `_dl_protect_relro`, which fails
   under plain `apparmor.profile = unconfined` on some hosts because
   binary-to-profile matching still applies — SSH password logins
   then fail with an obscure RELRO error.

`generated` fixes both without dropping host-boundary confinement.
No flag is required.

**Host requirement.** `generated` is compiled by `apparmor_parser`, so on a
host whose kernel has AppArmor as an active LSM the `apparmor` package must be
installed. LXC only *recommends* `apparmor`, so an image built with recommends
disabled can carry `lxc` but no parser — `generated` then hard-fails at start
with `Cannot use generated profile: apparmor_parser not available` and the
container never boots. Resolve it either way:

- install the `apparmor` package (gives the parser; `generated` then enforces), or
- set `KENTO_APPARMOR_PROFILE=unconfined` to deliberately run without a profile
  (kento's escape hatch; the default is `generated`).

On a kernel where AppArmor is *not* the active LSM, `generated` silently no-ops
and no parser is needed.

### Port forwarding

`--port HOST:GUEST` (lxc and pve-lxc) installs host DNAT/masquerade rules at
start so `localhost:HOST` reaches `GUEST` inside the container. Kento picks a
NAT backend automatically:

- **`nft` (preferred):** rules live in a dedicated `ip kento` nftables table,
  tagged with a `kento:<name>` comment.
- **`iptables` (fallback):** if `nft` is not installed, kento uses the standard
  `iptables` `nat` table (PREROUTING/OUTPUT DNAT + POSTROUTING MASQUERADE),
  with the same `kento:<name>` comment tag.
- **Neither present:** kento skips port forwarding rather than failing the
  start — it writes a `kento-portfwd-error` marker in the container state dir,
  warns on stderr, and the instance still boots (just without forwarding).
  Install `nftables` or `iptables` to enable it.

The chosen backend is recorded in a `kento-portfwd-backend` marker so teardown
at stop removes the rules with the matching tool.

### Nested networking

When `--allow-nesting` is set (any mode), kento drops a
`/etc/systemd/network/10-kento-nested-veth.network` unit into the guest
(`[Match] Kind=veth` + `Name=!eth0` → `[Link] Unmanaged=yes`). This keeps
the guest's own systemd-networkd from reconciling the host-side veths that
nested LXC/docker/podman attach to an in-guest bridge — otherwise networkd
strips them off the bridge moments after start and nested containers lose
their network. The guest's own `eth0` is excluded, so its uplink is
unaffected. The file lands in the writable overlay layer (cleared by
`scrub`) and is inert on guests that do not run systemd-networkd.

## pve-lxc mode

Proxmox VE containers -- same LXC underneath, but integrated with the
PVE management stack. Instances appear in the Proxmox web UI and can
be managed with `pct`. Created with `kento lxc create` on a PVE host
(or with `--pve` on any host).

- **Start command:** `pct start <VMID>`
- **Access:** `pct exec <VMID> -- bash` or the Proxmox web console
- **Network bridge:** `vmbr0` (override with `--network bridge=<name>`)
- **Config location:** `/etc/pve/nodes/<hostname>/lxc/<VMID>.conf`
- **Memory:** 512 MB default (override with `--memory`, configurable via `/etc/kento/lxc.conf`)
- **CPU:** 1 core default (override with `--cores`, configurable via `/etc/kento/lxc.conf`)
- **Nesting:** enabled by default
- **VMID:** auto-assigned (lowest free >= 100), or specify with `--vmid`

### VMID allocation

PVE requires a numeric VMID >= 100 for each instance. Kento reads
`/etc/pve/.vmlist` (or scans config files as fallback) to find used
IDs, then assigns the lowest free one.

The instance directory is named by VMID (e.g., `/var/lib/lxc/100/`)
but the human-readable name is stored in the `kento-name` file. All
kento commands accept the instance name, not the VMID.

### PVE startup sequence

1. PVE generates LXC config with hardcoded rootfs path
2. `lxc-pve-prestart-hook` runs (harmless no-op for kento)
3. Kento's hook fires (`pre-mount`) -- mounts overlayfs
4. LXC bind-mounts the populated rootfs
5. Instance boots with systemd

## vm mode

Full QEMU virtual machines with the guest kernel coming from inside the
OCI image. Created with `kento vm create`. See [VM Mode](vm-mode.md)
for full details.

- **Start command:** QEMU via kento (no external hypervisor manager)
- **Access:** SSH (`ssh -p <port> <user>@localhost`)
- **Network:** QEMU user-mode networking with port forwarding
- **Memory:** 512 MB default (override with `--memory`, configurable via `/etc/kento/vm.conf`)
- **Instance directory:** `/var/lib/kento/vm/<name>/`
- **Requires:** QEMU, virtiofsd, kernel + initramfs in image

VM mode is useful when you need a real kernel -- custom modules, kernel
testing, or full hardware isolation.

## pve-vm mode

QEMU VMs managed through Proxmox VE. Created with `kento vm create` on
a PVE host (or with `--pve`). Uses a PVE hookscript and qm config so
the VM appears in the Proxmox web UI.

- **Start command:** `qm start <VMID>`
- **Access:** `qm terminal <VMID>` or SSH
- **Network:** bridge (`vmbr0` by default) with `net0:` in qm config.
  `--network usermode` is also supported: kento injects a slirp netdev +
  host-port forwarding into the qm `args:` line (no `net0:` field), giving
  the same usermode networking as plain `vm`.
- **Memory:** 512 MB default (override with `--memory`)
- **Instance directory:** `/var/lib/kento/vm/<name>/`
- **Requires:** QEMU, virtiofsd, kernel + initramfs in image, PVE host

## Interactive access

Three commands open or run things inside an instance. Each is available
at all three CLI levels (`kento <cmd>`, `kento lxc <cmd>`, `kento vm
<cmd>`); the mechanism is chosen from the instance's recorded mode.

| | lxc | pve-lxc | vm | pve-vm |
|---|---|---|---|---|
| `attach` / `enter` | `lxc-attach` | `pct enter` | serial console relay | `qm terminal` |
| `exec` | `lxc-attach -- cmd` | `pct exec -- cmd` | use SSH | use SSH |
| `logs` | `journalctl` via exec | `journalctl` via exec | use `attach` / SSH | use `attach` / SSH |

- **`attach` (alias `enter`)** opens the interactive console. For plain
  `vm`, kento connects a pure-Python relay to the guest's serial console
  unix socket; detach with **Ctrl-] then Q**. The relay needs an
  interactive terminal (it errors if stdin is not a tty) and a running
  instance (it errors with a pointer to `kento start` when the serial
  socket is absent). The guest image must run a getty on `ttyS0`
  (`console=ttyS0`) or the console will be blank.
- **`exec`** runs a command inside the instance:
  `kento exec <name> -- <cmd...>` (the `--` is optional). It is
  supported for `lxc` and `pve-lxc` only; on `vm` / `pve-vm` it errors
  with a pointer to use SSH or `kento attach` (there is no in-guest
  agent yet).
- **`logs`** runs `journalctl` inside the guest via the exec mechanism,
  forwarding extra arguments (e.g. `kento logs web -f -n 50`). `lxc` /
  `pve-lxc` only; `vm` / `pve-vm` error with a pointer to `attach` / SSH.

## Suspend / resume

`kento suspend <name>` and `kento resume <name>` pause and un-pause a
running VM's vCPUs — a *pause to RAM*, not a shutdown: the VM process
keeps running and its memory is retained. **VM modes only.**

| | lxc | pve-lxc | vm | pve-vm |
|---|---|---|---|---|
| `suspend` / `resume` | use `stop` / `start` | use `stop` / `start` | QMP `stop` / `cont` | `qm suspend` / `qm resume` |

For `lxc` / `pve-lxc` there is no vCPU to pause, so both commands error
with a pointer to `kento stop` / `kento start`. The instance must be
running. A plain-`vm` suspend uses the `qmp.sock` unix socket and is not
persisted across a host reboot or if the QEMU process dies.

## Changing settings (`set`)

`kento set <name> [flags]` mutates scalar settings on a **stopped**
instance; the change takes effect on the next start (it errors if the
instance is running). Available at all three CLI levels. Per-mode flag
validity:

| Flag | lxc | pve-lxc | vm | pve-vm |
|---|---|---|---|---|
| `--memory` / `--cores` | yes | yes | yes | yes |
| `--mac` | — | — | yes | yes |
| `--qemu-arg` | — | — | yes | yes |
| `--pve-arg` | — | yes | — | yes |
| `--lxc-arg` | yes | — | — | — |

Passing a flag for a mode that does not support it errors before any
change is made. The list flags (`--qemu-arg` / `--pve-arg` / `--lxc-arg`)
replace the stored list when given non-empty values, clear it when given
an empty value (`--qemu-arg ''`), and leave it untouched when omitted.

## Comparison

| | lxc | pve-lxc | vm | pve-vm |
|---|---|---|---|---|
| Noun | `kento lxc` | `kento lxc` | `kento vm` | `kento vm` |
| PVE auto-detect | n/a | Yes | n/a | Yes |
| Guest kernel | Shared (host) | Shared (host) | Own (from image) | Own (from image) |
| Start time | ~1s | ~1s | ~10s | ~10s |
| Access | lxc-attach | pct exec / web UI | SSH | qm terminal / SSH |
| Network | Bridge | Bridge | User-mode (NAT) | Bridge |
| Management UI | None | Proxmox web UI | None | Proxmox web UI |
| Memory default | No limit | 512 MB | 512 MB | 512 MB |
| CPU default | No limit | 1 core | Host CPU | Host CPU |
