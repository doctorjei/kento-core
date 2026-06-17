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
- **Privilege:** privileged by default -- container root maps to host root,
  no UID/GID shift (see below)
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

**Modern-systemd userns mounts.** systemd 256+ also sandboxes its own core
units (systemd-networkd, resolved, logind, journald, …) with `PrivateUsers=` /
`PrivateMounts=`, which create a user namespace and do bind/move/remount/
pivot_root mounts. Under AppArmor 4.x (Debian 13 / trixie) the generated
profile mediates `userns_create` and these mounts and denies them by default —
the guest then boots with sshd up but **no IP**. Kento grants exactly that
bounded vocabulary via `lxc.apparmor.raw` — `userns,`, `mount,`, `umount,`,
`pivot_root,`, `mqueue,` — which is strictly tighter than `allow_nesting` (no
nested-container peer rules, no raw proc/sys). This applies to **both** LXC
modes (plain-lxc and pve-lxc) when nesting is off; with `--allow-nesting` the
runtime nesting profile already grants it, so the narrow set is skipped. (pve-lxc
gets a `lxc.apparmor.profile: generated` line of its own — kento creates
`ostype: unmanaged` containers, which carry no PVE-managed profile.)

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

**Create-time pre-flight.** kento checks for this at `create` time: if the host
kernel has AppArmor active but `apparmor_parser` is absent, `kento lxc create`
fails immediately with the same two-way remediation (install `apparmor`, or set
`KENTO_APPARMOR_PROFILE=unconfined`) rather than letting the instance hard-fail
later at start. Explicit `unconfined` needs no parser and is never blocked.

### Privilege level

Kento creates **privileged** containers by default in both LXC modes. This
falls out of the config kento generates:

- **plain lxc:** no `lxc.idmap` line is emitted, so there is no UID/GID
  remapping -- container root (UID 0) is host root (UID 0).
- **pve-lxc:** no `unprivileged: 1` key is written; `pct` treats the absent
  key as privileged (`unprivileged: 0`).

The reason is the OCI layer store. Kento reads podman's layers directly and
stacks them read-only via overlayfs -- it does not copy or rewrite them. An
unprivileged container needs a UID shift (idmap), which would mismatch the
ownership baked into those shared layers; making it work would require
idmapped mounts or a chown pass, neither of which fits kento's "don't
duplicate or modify layers" principle. Privileged side-steps that.

Privileged does **not** mean unconfined: in plain-lxc the `generated` AppArmor
profile (above) still enforces the host/container boundary, and namespaces and
cgroups apply in both modes. The trade-off is that container root is uid 0 on
the host, so a kernel-level container escape lands as host root. Run untrusted
or multi-tenant workloads in `vm`/`pve-vm` mode instead.

#### Limits

Kento enforces two hard create-time limits. Both **fail closed at create** with
a clear error rather than letting a bad value reach the kernel and surface as a
cryptic later failure. The numbers are not arbitrary — each is pinned to a real
kernel ceiling, explained below.

##### Max overlay layers per image: 128

Kento builds the overlayfs `lowerdir` from the podman store's layer
directories. The kernel caps the options string of a classic `mount(2)` call at
a single **4096-byte page**; a deeply layered image whose absolute
`<store>/<id>/diff` paths (~104 bytes each) exceed that page would be **silently
truncated** by the kernel mid-path, and the overlay mount then fails with
cryptic errors like `overlayfs: failed to resolve '<truncated path>': -2` or
`workdir and upperdir must be separate subtrees` (surfacing as a PVE pre-start
hookscript exit code 32).

To avoid this, kento mounts exactly the way Docker (`overlay2`) and podman
themselves do: each layer has a short `l/<SHORTID>` symlink in the store, and
the mount `chdir`s into the overlay root and uses **relative** `l/<short>`
entries (~28 bytes each) instead of full absolute paths. The `chdir` is scoped
to a subshell (LXC/VM hooks) or the mount subprocess (`vm.py`) so it never leaks
to later steps.

On top of that, kento **caps images at 128 overlay layers** (`MAX_OVERLAY_LAYERS`,
matching Docker overlay2's `maxDepth`) and **fails closed at create** if an
image is deeper, with an actionable message — rather than letting the kernel
truncate. A defensive byte assert additionally refuses any options string that
would come within 16 bytes of the 4096-byte limit (a backstop for pathological
state-dir / instance-name lengths; it should never fire given the 128 cap).
Squash or flatten an over-deep image to fewer layers.

> **TODO:** the 128-layer cap can be lifted once the new mount API (`fsconfig`,
> what `LIBMOUNT_FORCE_MOUNT2=always` is meant to trigger) is reliably
> available — it has no single-page options limit. That env var is currently a
> silent no-op on some util-linux / kernel combinations (it falls back to
> classic `mount(2)` and truncates anyway), so the cap stays for now.

##### Max instance name: 64 characters

Kento writes the instance name as the guest **hostname** (`hostname: <name>`),
and Linux caps a hostname at `HOST_NAME_MAX` = **64** (the kernel
`utsname.nodename` field). A longer name is simply an invalid hostname — PVE
rejects it, and on a plain guest `sethostname()` fails at boot. So 64 is the
binding ceiling, and kento **fails closed at create** with a clear `name too
long, max 64`-style error. This applies to both an explicit `--name` and an
auto-generated name.

The 64-char limit also keeps the overlay mount-options string (above) bounded:
the name appears **twice** in that string — once in the `upperdir` path and once
in the `workdir` path — so it counts double toward the 4096-byte budget. It is
the last otherwise-uncapped contributor to that budget, but the hostname limit
is by far the tighter constraint, so that is the one kento documents and
enforces.

#### `--unprivileged` (lxc and pve-lxc)

`kento create --unprivileged` opts an LXC container into an unprivileged
configuration where container root (UID/GID 0) maps to an unprivileged host
UID/GID range (default 100000:65536) instead of host root.

**Mechanism — per-layer idmapped bind mounts.** Rather than copying or
rewriting the shared read-only image layers, kento idmaps each OCI lower layer
individually using a per-layer idmapped bind mount (`X-mount.idmap`), then
stacks an overlayfs over those idmapped lowers with `userxattr`. The result is
that the merged rootfs presents host UID 100000 where the image stores 0, which
is exactly what the container's userns expects.

- **plain-lxc:** kento emits `lxc.idmap u 0 100000 65536` /
  `g 0 100000 65536` in the LXC config, and its `lxc.hook.pre-start` (which
  runs as real root in the host namespace) builds the per-layer idmapped
  overlay before `lxc-start` hands the rootfs to the container.
- **pve-lxc:** kento sets `unprivileged: 1` in the PVE config so PVE owns the
  user namespace and provides honest accounting, the correct AppArmor profile,
  and its own idmap lines in `/var/lib/lxc/<vmid>/config`. The overlay is built
  from kento's `lxc.hook.pre-start` hook — for an *unprivileged* container this
  is the only hook that runs in the host's **initial** namespace as real root.
  `lxc.hook.pre-mount` and `lxc.hook.mount` both run inside the container's
  **child** user namespace (where host root is mapped to UID 100000), so
  creating a per-layer idmapped bind mount there fails with `EPERM`; pre-start
  runs before the userns is entered and has the full host UID map and
  capabilities. (This differs from the *privileged* pve-lxc path, which has no
  userns and so mounts from `lxc.hook.pre-mount` — see "PVE startup sequence".)
  The hook reads PVE's idmap range from the runtime config (`$LXC_CONFIG_FILE`),
  falling back to a `kento-idmap-range` state file kento writes at create time,
  because PVE may not have populated `lxc.idmap` into the runtime config by the
  time pre-start fires. The overlay-mount idempotency guard checks the rootfs
  *fstype* (it mounts only if `$ROOTFS` is not already an `overlay`) rather than
  a bare `mountpoint` check, because PVE's own `lxc-pve-prestart-hook`
  bind-mounts the dir rootfs before kento's hook runs — a mountpoint guard would
  see that bind mount, wrongly skip kento's overlay, and leave an empty rootfs.
  Because the rootfs is kento's own overlay (not a PVE-managed storage volume),
  PVE does not attempt to chown or double-idmap it.

**Requirements (fail-closed).** `--unprivileged` requires:
- Linux kernel **5.19+** — idmapped overlay lower mounts (mainline since 5.19).
- util-linux **2.40+** — `X-mount.idmap` in the `mount(8)` utility (the
  privileged path only needs 2.39).

Kento probes these requirements at `create` time and **fails closed** with a
clear error on an incapable system rather than silently falling back to a
privileged container.

**Not supported for `vm` / `pve-vm`** — VM guests are already hardware-isolated;
idmap is an LXC concept. `--unprivileged` is rejected for those modes.

**ACL caveat.** In unprivileged mode, POSIX ACLs *baked into read-only image
layers* are not honored (the idmapped lowers do not propagate ACL xattrs from
the on-disk layer). ACLs set at *runtime* on the container's own writable files
— for example, journald setting default ACLs on `/var/log/journal`, or an
application setting ACLs on a data directory — work normally. Standard OCI
images do not ship read-only-layer ACLs, so this limitation does not arise in
practice.

The default in every mode remains **privileged**.

> The other privilege control is the `--pve-arg`/`--lxc-arg` pass-throughs
> (e.g. `--pve-arg 'unprivileged: 1'` on PVE), which kento does not validate
> against the overlayfs setup above — prefer `--unprivileged`, which does.

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
(`[Match] Name=veth*` → `[Link] Unmanaged=yes`). This keeps the guest's own
systemd-networkd from reconciling the host-side veths that nested
LXC/docker/podman attach to an in-guest bridge — otherwise networkd strips
them off the bridge moments after start and nested containers lose their
network. The match is by interface *name* (`veth*`), which is set at link
creation, so it is race-free — unlike a `Kind=veth` match, whose `kind`
attribute can lag link-appearance and leave a window for a broad
`Type=ether` DHCP unit to claim the veth first. `veth*` also naturally
excludes the guest's own `eth0` uplink, so it is unaffected. The file lands
in the writable overlay layer (cleared by `scrub`) and is inert on guests
that do not run systemd-networkd.

## pve-lxc mode

Proxmox VE containers -- same LXC underneath, but integrated with the
PVE management stack. Instances appear in the Proxmox web UI and can
be managed with `pct`. Created with `kento lxc create` on a PVE host
(or with `--pve` on any host).

- **Start command:** `pct start <VMID>`
- **Access:** `pct exec <VMID> -- bash` or the Proxmox web console
- **Network bridge:** `vmbr0` (override with `--network bridge=<name>`)
- **Config location:** `/etc/pve/nodes/<hostname>/lxc/<VMID>.conf`
- **Memory:** no limit by default (override with `--memory`). Note: PVE has no
  "unlimited" sentinel and silently backfills its 512 MiB schema default on an
  omitted `memory:` field, so kento instead emits PVE's schema-ceiling value,
  which the kernel clamps to cgroup `memory.max = max` (truly unlimited).
- **CPU:** 1 core default (override with `--cores`, configurable via `/etc/kento/lxc.conf`)
- **Nesting:** enabled by default
- **Privilege:** privileged by default -- kento writes no `unprivileged: 1`
  key, and `pct` treats the absent key as privileged (see "Privilege level"
  under lxc mode); opt into unprivileged mode with `--unprivileged`
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
3. Kento's hook fires (`pre-mount` for privileged; `pre-start` for
   `--unprivileged`, the only hook running in the host's initial namespace as
   real root) -- mounts overlayfs (with per-layer idmapped binds in
   unprivileged mode)
4. LXC bind-mounts the populated rootfs
5. Instance boots with systemd

## vm mode

Full QEMU virtual machines with the guest kernel coming from inside the
OCI image. Created with `kento vm create`. See [VM Mode](vm-mode.md)
for full details.

- **Start command:** QEMU via kento (no external hypervisor manager)
- **Access:** SSH (`ssh -p <port> <user>@localhost`)
- **Network:** QEMU user-mode networking with port forwarding
- **Memory:** 1024 MB default (override with `--memory`, configurable via `/etc/kento/vm.conf`)
- **CPU:** Host CPU count. `--cores` is **clamped down to the node's logical
  CPU count** (with a warning) — QEMU/PVE refuse more vCPUs than the host has, so
  an over-request would create an unstartable VM. The same clamp applies on
  `kento set --cores`. Over-requesting `--memory` only warns (KVM permits
  overcommit) and is never clamped.
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
- **Memory:** 1024 MB default (override with `--memory`)
- **CPU:** Host CPU count. `--cores` is **clamped down to the node's logical CPU
  count** (with a warning), at create time and on `kento set --cores` — `qm`
  hard-refuses more vCPUs than the node has. Over-requesting `--memory` only warns
  and is never clamped.
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
| Memory default | No limit | No limit¹ | 1024 MB | 1024 MB |
| CPU default | No limit | 1 core | Host CPU | Host CPU |

¹ plain-lxc is truly unlimited (an omitted limit → liblxc default). pve-lxc
cannot rely on omission — PVE backfills its 512 MiB schema default — so kento
emits PVE's schema-ceiling value, which clamps the cgroup `memory.max` to `max`.
