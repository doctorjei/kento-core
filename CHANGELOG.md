# Changelog

All notable changes to kento are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.5.3] - 2026-06-12

Patch release: one machine-readability feature plus two robustness fixes, each
with a regression test. Versioned as a patch to keep kento's cadence in step
with the sibling projects. Gates: unit 1255, integration 24, E2E 210/210 on
bifrost (regression — phase/iso/image across all four modes).

### Added

- **`kento list --json`** — machine-readable listing. Emits a JSON array with one
  object per instance carrying the same per-instance keys `inspect --json` does
  (`name`, `type`, `mode`, `image`, `status`, plus `vmid` / `mac` / `environment`
  / `ssh_host_key_fingerprints` / `upper_size` when present), so orchestrators can
  enumerate in a single call instead of parsing the columnar output and then
  calling `inspect --json` once per instance (an N+1). Available on the bare
  `list` and on `lxc list` / `vm list`. Zero instances emits `[]`. The
  enrichment fields (and the `ssh-keygen` subprocess that reads fingerprints) are
  computed only in `--json` mode, so the human table path is unchanged in cost.

### Changed

- **`inspect --json` `mode` is now normalized** (`pve` → `pve-lxc`), matching what
  `list` already reports, so the two surfaces agree on the mode string for
  PVE-LXC instances. `type` (the `LXC`/`VM` family) is unchanged.

### Fixed

- **Port-forward teardown could match a sibling instance's rules for dotted
  names.** v1.5.2 anchored the post-stop `kento:<name>` comment match, but the
  name was still interpolated into the `grep -E` pattern unescaped — and a valid
  kento name may contain `.`, an ERE "any character". So tearing down `web.api`
  could also match and delete a running `web1api`'s nft/iptables DNAT rules. The
  teardown now regex-escapes the name before the iptables and nft greps. (The
  install side writes literal comments and is unaffected.)
- **`next_vmid()` could re-hand-out an orphan's VMID.** Allocation consulted only
  PVE's view (`.vmlist` / `*.conf`), so a kento instance whose PVE config was
  destroyed out-of-band — leaving an orphan kento dir — read as free and its VMID
  could be reassigned. `next_vmid()` (and `validate_vmid()`) now also union
  kento's own recorded VMIDs (PVE-LXC dir names + PVE-VM `kento-vmid` files,
  including orphans) into the in-use set. Purely additive and defensive; nothing
  is reaped.

## [1.5.2] - 2026-06-12

Maintenance release from a skeptical top-to-bottom code review (31 confirmed
findings after adversarial verification). Correctness, robustness, and
create/set parity fixes; no new features and no intended behavior changes on
the happy path. Each fix carries a regression test. Gates: unit 1238,
integration 23, E2E 225/225 on bifrost (regression 210 + nested Section D 15).

### Added

- `exec`, `logs`, `attach`, `suspend`, `resume`, and `set` now honor the
  explicit `kento lxc` / `kento vm` scope. Previously these commands discarded
  the namespace and resolved by name across both, so an instance name created
  in both namespaces via `create --force` could not be disambiguated (the
  commands aborted with "ambiguous name" even when the scope was given).
  `resolve_any()` gained an optional `namespace` argument; `namespace=None`
  preserves the prior behavior exactly.

### Fixed

- **Port-forward teardown could delete another instance's rules.** The post-stop
  hook matched its `kento:<name>` rule comment as an unanchored substring, so
  stopping `web` also tore down a still-running `web2`'s nft/iptables DNAT and
  masquerade rules. The match is now anchored to the full comment token.
- **VM start could leak a mount and a virtiofsd process.** On the standalone
  `kento start` / `kento vm start` path, a virtiofsd that never created its
  socket (or died), or a missing/failing `qemu-system-x86_64`, left the overlay
  mounted and virtiofsd running and wedged the next start. `start_vm` now
  pre-checks for QEMU, aborts cleanly if the virtiofsd socket never appears, and
  rolls back (kill virtiofsd, unmount, drop pid files) on any failure.
- **`kento attach` could drop serial-console output.** The VM serial relay
  forwarded socket data with a single `os.write()` that ignored short writes,
  silently truncating console output to a pipe/file (e.g. `kento attach <vm> |
  tee`). It now writes all bytes.
- **`kento set` did not enforce the pass-through denylists `create` does.**
  `--qemu-arg` / `--pve-arg` values reserved for kento (e.g. `-kernel`,
  `memfd-size=`, `rootfs:`, `arch:`, `hostname:`) were accepted by `set` and
  re-emitted into the boot config, duplicating or clobbering kento-owned keys.
  `set` now applies the same `QEMU_ARG_DENYLIST` / `PVE_ARG_DENYLIST` checks,
  and `set --mac` now rejects multicast/broadcast MACs at parse time like
  `create` does.
- **A PVE qm snapshot's `args:` could be corrupted.** `sync_qm_args_to_memory`
  rewrote/dropped every `args:` line across the whole config, including those
  inside `[snapshot]` sections. It now stops at the first section header,
  mirroring the config parser.
- **`kento scrub` could leave a half-scrubbed instance.** The writable layer was
  cleared before re-resolving the image, so a scrub of an instance whose backing
  image was gone wiped `upper`/`work` and then aborted. Image resolution now
  happens first, so a missing image aborts with no side effects.
- **`kento destroy` could leave an orphan directory.** A failure deleting the
  PVE config (pmxcfs error) or parsing a corrupt `kento-vmid` aborted before the
  instance directory was removed. Under `-f`, config cleanup is now best-effort
  and destroy proceeds to remove the directory.
- **`kento list` could crash on a single bad instance.** One unreadable or
  concurrently-destroyed instance directory aborted the whole listing; per-entry
  read errors are now skipped.
- **`kento stop` on an already-stopped PVE instance could hard-fail.** When the
  `pct`/`qm status` query timed out, kento assumed the instance was running and
  issued a shutdown that errored out instead of reporting "Already stopped".
  Stop now tolerates the not-running case.
- **DHCP port-forward worker race.** A detached worker discovering a DHCP lease
  could install NAT rules after the container had already stopped, leaving
  orphan rules. post-stop now writes a cancel sentinel, kills the worker's
  process group, and runs teardown unconditionally.
- Verbose `kento info` no longer misaligns layer sizes against layer paths when
  a layer directory is missing.
- `--env` values are validated as `KEY=VALUE` with no control characters; an
  embedded newline previously broke the generated cloud-init YAML and silently
  dropped later directives.
- `ssh-key-user` is matched as a literal `/etc/passwd` field rather than a
  `grep` regex, so a username containing regex metacharacters can no longer
  resolve to the wrong account.
- `kento-memory` / `kento-cores` are validated before shell arithmetic in the
  start hook, so a corrupt value warns and is skipped instead of aborting the
  hook under `set -e`.
- `run_or_die` reports `PermissionError` / exec-format errors with a branded
  message instead of a traceback; a stale `SUDO_USER` no longer produces an
  uncaught `KeyError`; a missing LXC snippets directory at destroy warns instead
  of silently passing.

## [1.5.1] - 2026-06-11

### Added

- Create-time advisory when `--ssh-key-user` is left at the default `root`
  on a cloud-init image. Cloud images (Debian/Ubuntu cloud) typically
  disable root SSH login, so keys injected for `root` often can't be used to
  connect. kento now prints a non-fatal warning suggesting
  `--ssh-key-user <user>` (e.g. `debian`). Create still proceeds unchanged.

### Changed

- The `--allow-nesting` networkd drop-in kento injects to keep a guest from
  managing its nested host-side veths (`10-kento-nested-veth.network`) now
  matches by `[Match] Name=veth*` instead of `[Match] Kind=veth` + `Name=!eth0`.
  The interface *name* is set at link creation, so the match is race-free; the
  `kind` attribute can lag link-appearance, leaving a window where an image's
  broad `Type=ether` DHCP unit could claim a nested veth and strip its bridge
  master before the unmanaged match applies. `veth*` also naturally excludes
  the guest's own `eth0` uplink, so the separate `Name=!eth0` exclusion is no
  longer needed. Defense-in-depth hardening — not tied to a reproduced failure.
- `kento create` now resolves and validates the OCI image *before* allocating
  a name/VMID or creating any instance directory, so a missing image fails
  with zero filesystem side effects.
- The "image not found" error from `create` now names the local store and
  hints the fix (`kento pull <image>`) instead of a bare message. `create`
  remains network-free by design — it does not implicitly pull.

### Fixed

- PVE-LXC guests from images that bake an over-broad `Kind=veth` unmanaged
  systemd-networkd drop-in (e.g. `10-lxc-veth-unmanaged.network` with
  `[Match] Kind=veth` + `[Link] Unmanaged=yes`) came up with **no network**.
  systemd-networkd assigns each link to the first matching `.network` by
  lexical filename order, and under PVE the guest `eth0` presents
  `Kind=veth`, so the image's `10-`-prefixed unit sorted before kento's
  `10-static.network` and claimed `eth0` as unmanaged before kento's
  addressing config could apply. kento now names its injected addressing
  units `05-kento-static.network` / `05-kento-dhcp.network` so they sort
  first and win in **both** plain-LXC and PVE-LXC modes. Plain-LXC was
  unaffected (its `eth0` does not present `Kind=veth`); this fixes the
  plain-vs-PVE mode divergence. The `--allow-nesting` drop-in
  (`10-kento-nested-veth.network`) is unchanged — it targets `Name=!eth0`
  and governs nested veths, not `eth0`.
- kento treated a pve/pve-vm instance whose PVE config is gone (destroyed
  out-of-band) as "running" — any non-zero `qm`/`pct status` was
  assumed-running — so it appeared `running` in `kento list` and `kento stop`
  hard-errored. kento now recognizes a missing PVE config as not-running
  (shown as `orphan` in `list`); `destroy -f` cleans up the orphaned state.
- A failed `kento vm create` (e.g. image-not-found) left an orphan instance
  directory behind: the dir + assigned VMID were created before image
  resolution, but the abort happened before any metadata was written, so the
  half-built dir was invisible to `list`/`destroy`/`info` yet blocked
  recreate (`instance already exists`). Image resolution now happens before
  any directory is created (see Changed), so a failed create leaves nothing
  behind. Applies to all modes, not just `vm`.
- `kento lxc create`/`run --help` no longer advertises VM-only flags
  (`--qemu-arg`, `--mac`), and `kento vm create`/`run --help` no longer
  advertises the plain-LXC-only `--lxc-arg`. These were always rejected at
  the wrong scope with an explanatory error, but listing them in `--help`
  implied they were accepted. `--pve-arg` stays visible in both scopes (it
  applies to PVE-LXC and PVE-VM).

### Documentation

- Documented that kento creates **privileged** LXC containers by default in
  both `lxc` and `pve-lxc` modes (no `lxc.idmap` / `unprivileged: 1`), the
  reason (the read-only OCI overlay layer store vs. an unprivileged UID
  shift), and that privileged is not unconfined (the `generated` AppArmor
  profile plus namespaces/cgroups still apply). See `docs/modes.md`.

## [1.5.0] - 2026-06-10

### Changed

- Plain-LXC create now pre-flights the default `generated` AppArmor profile.
  When the host kernel has AppArmor active as an LSM but `apparmor_parser`
  is not installed, `generated` cannot be loaded and the container would
  hard-fail at `lxc-start` (`Cannot use generated profile: apparmor_parser
  not available`). kento now detects this at create/config-generation time
  and exits with an actionable error — install the `apparmor` package, or
  set `KENTO_APPARMOR_PROFILE=unconfined` — instead of writing a doomed
  config that fails confusingly later at start. Only the effective
  `generated` profile is gated (explicit `unconfined` needs no parser), and
  only for plain `lxc` mode (PVE handles AppArmor via `pct`; VM modes have
  no LXC config). On a kernel without AppArmor active, `generated` is a
  harmless no-op and the check does nothing.

### Fixed

- Port forwarding now falls back to `iptables` when `nft` is not installed.
  Previously the `start-host` hook installed DNAT/masquerade rules
  exclusively via `nft`; on an iptables-only host the rule installs ran
  unguarded under `set -eu`, so a missing `nft` exited 127 and **aborted
  the entire start-host hook** (the instance failed to come up cleanly),
  while the DHCP worker failed silently. The hook now resolves a NAT
  backend once (`nft` preferred, else `iptables`); if neither binary is
  present it writes a `kento-portfwd-error` marker, warns, and returns
  cleanly instead of aborting the start. The chosen backend is recorded in
  a `kento-portfwd-backend` marker so post-stop teardown deletes the rules
  with the matching tool (nft handle delete vs iptables line-number delete);
  the marker defaults to `nft` when absent for back-compat with pre-1.5.0
  containers.

## [1.4.1] - 2026-06-08

### Fixed

- pve-vm `--network usermode` now injects slirp networking (a QEMU
  user-mode netdev + host-port forwarding) into the qm `args:` line,
  mirroring plain `vm`. Previously usermode was silently dropped on a PVE
  host (only `net_type == "bridge"` emitted a `net0:` line), producing a
  NIC-less VM with no network that still reported running.

### Documentation

- Document that the default plain-LXC `lxc.apparmor.profile = generated`
  requires `apparmor_parser` on a host whose kernel has AppArmor as an
  active LSM. If the parser is absent the container hard-fails at start
  (`Cannot use generated profile: apparmor_parser not available`) rather
  than degrading — install the `apparmor` package, or set
  `KENTO_APPARMOR_PROFILE=unconfined`. Covered in `docs/modes.md`
  ("AppArmor profile") and `docs/troubleshooting.md`; the
  `KENTO_APPARMOR_PROFILE` escape hatch is now documented.

## [1.4.0] - 2026-06-07

### Added

- `kento attach` (alias `enter`) — open an instance's interactive
  console. The mechanism is per-mode: plain `vm` connects a pure-Python
  relay to the guest's serial console over a unix socket; `lxc` uses
  `lxc-attach`, `pve-lxc` uses `pct enter`, and `pve-vm` uses `qm
  terminal`. Available at all three CLI levels (`kento attach`, `kento
  lxc attach`, `kento vm attach`). The plain-vm serial relay needs an
  interactive terminal (errors if stdin is not a tty) and a running
  instance (errors with a pointer to `kento start` when the serial
  socket is absent); detach with **Ctrl-] then Q**. The guest must run
  a getty on `ttyS0` (`console=ttyS0`) or the console shows nothing.
- `kento exec <name> -- <cmd...>` — run a command inside an instance
  (the `--` is optional). Supported for `lxc` (`lxc-attach -- cmd`) and
  `pve-lxc` (`pct exec -- cmd`); on `vm` / `pve-vm` it errors with a
  pointer to use SSH or `kento attach` (no in-guest agent yet).
  Available at all three CLI levels.
- `kento logs <name> [journalctl-args...]` — run `journalctl` inside
  the guest via the exec mechanism, forwarding extra arguments
  (e.g. `kento logs web -f -n 50`). `lxc` / `pve-lxc` only; `vm` /
  `pve-vm` error with a pointer to `attach` / SSH. Available at all
  three CLI levels.
- Plain `vm` mode now starts QEMU with `-display none` plus a serial
  unix socket (`serial.sock`) and a QMP unix socket (`qmp.sock`) under
  the instance directory, replacing the old `-nographic`. The sockets
  are created at start and removed at stop; existing VM instances get
  them on their next start (no migration needed). The serial socket
  backs `kento attach`; the QMP socket is groundwork for future
  suspend/resume. pve-vm already exposed `serial0: socket` (used by
  `qm terminal`).
- `kento set <name> [flags]` — change scalar settings on a **stopped**
  instance; the changes take effect on the instance's next start
  (errors if the instance is running, with a pointer to `kento stop`).
  Available at all three CLI levels (`kento set`, `kento lxc set`,
  `kento vm set`). Flags and per-mode validity: `--memory MB` and
  `--cores N` apply to all four modes; `--mac XX:XX:XX:XX:XX:XX` and
  `--qemu-arg ARG` (repeatable) are VM-modes-only (`vm`, `pve-vm`);
  `--pve-arg 'KEY: VALUE'` (repeatable) is PVE-modes-only (`pve-lxc`,
  `pve-vm`); `--lxc-arg 'KEY = VALUE'` (repeatable) is plain-LXC-only.
  Passing a flag for the wrong mode errors before anything is mutated.
  The metadata files under the instance dir are the source of truth;
  kento surgically re-emits only the kento-owned scalar lines in the
  native `config` / PVE `.conf` / qm `.conf`, preserving every
  structural and network line. List flags (`--qemu-arg` / `--pve-arg` /
  `--lxc-arg`) REPLACE the stored list when given with non-empty
  values, CLEAR it when given only an empty value (`--qemu-arg ''`),
  and leave it untouched when omitted. An empty `set` (no flags) is a
  usage error.
- `--lxc-arg 'KEY = VALUE'` — the fourth config pass-through, alongside
  `--qemu-arg` (VM argv) and `--pve-arg` (PVE config). Appends raw lines
  verbatim to plain-LXC's native `config`, stored in
  `<instance_dir>/kento-lxc-args` (one per line). Available on both
  `kento create` / `run` and `kento set`, repeatable. **Plain-LXC only**:
  on a PVE host use `--pve-arg` (the PVE `.conf` carries raw `lxc.*`
  lines); VM modes have no native LXC config. A denylist
  (`LXC_ARG_DENYLIST` in `defaults.py`) rejects the structural keys kento
  owns — `lxc.uts.name`, `lxc.rootfs.path`, `lxc.hook.`, `lxc.net.`,
  `lxc.mount.auto`, `lxc.tty.max`, `lxc.apparmor.`, and the two cgroup
  lines `set` manages (`lxc.cgroup2.memory.max`, `lxc.cgroup2.cpu.max`).
  Everything else passes through verbatim, last-value-wins. Surfaced in
  `kento info` (JSON always; human output under `-v` when present), on
  par with `--qemu-arg` / `--pve-arg`.
- `kento suspend <name>` / `kento resume <name>` — pause and un-pause a
  running VM's vCPUs (a *pause to RAM*: the VM process keeps running and
  its memory is retained — this is **not** a shutdown). **VM-modes-only**:
  plain `vm` issues QMP `stop` / `cont` over `qmp.sock`; `pve-vm` runs
  `qm suspend` / `qm resume`. `lxc` / `pve-lxc` error with a pointer to
  `kento stop` / `kento start` (no vCPU to pause). Requires the instance
  to be running (errors with a pointer to `kento start` otherwise).
  Available at all three CLI levels. Note: a plain-`vm` suspend is not
  persisted across a host reboot or if the QEMU process dies.
- Nested-bridging support inside guests. When `--allow-nesting` is set,
  kento injects a systemd-networkd drop-in into the guest at
  `/etc/systemd/network/10-kento-nested-veth.network`
  (`[Match] Kind=veth` + `Name=!eth0` → `[Link] Unmanaged=yes`). Without
  it, a networkd-based guest reconciles the host-side veths created by
  nested LXC/docker/podman off their bridge — the same `[Match]
  Type=ether` failure class that strips veth bridge membership ~seconds
  after start — breaking nested container networking. The guest's own
  `eth0` uplink is explicitly excluded (inside an LXC it is itself a
  veth-kind device), so it is untouched. Written only when nesting is
  enabled, into the writable overlay layer (reversible by `scrub`,
  never mutating the image), in all four modes and both injection and
  cloud-init config modes. Default behavior (nesting off) is unchanged.

## [1.3.0] - 2026-06-07

### Added

- `--allow-nesting` flag on `kento <lxc|vm> create` / `run` (all four
  modes). One flag, one concept — "allow this instance to nest things":
  in LXC / PVE-LXC modes it permits the container to run nested
  containers (the `nesting.conf` include, `/dev/fuse` + `/dev/net/tun`
  bind mounts, and PVE `features: nesting=1`); in VM / PVE-VM modes it
  exposes the host CPU's virtualization extensions (vmx/svm) so the guest
  can run hardware-accelerated nested VMs. The setting is persisted in
  `kento-nesting`, preserved across `scrub`, and surfaced in
  `kento info` (and `--json`) for every mode. Default: **off**.
- `kento images` and `kento prune` — safe image garbage collection
  built on the hold-container mechanism. `kento images [--in-use]` is
  read-only and lists kento-managed images (referenced by an instance or
  pinned by a hold) with their referencing instances, hold status, and
  in-use/orphaned classification. `kento prune [--yes]` is a
  safe-by-default alternative to `podman system prune -a`: dry-run unless
  `--yes`, it removes only *orphaned* hold containers (whose instance no
  longer exists) and the images they freed, never touching an image that
  still backs a live instance.
- `kento scrub` and `kento start` now backfill the image-hold container
  if it is missing. Instances created before the hold mechanism existed
  were vulnerable to `podman system prune -a` removing their backing
  layers; they now self-heal on their next start or scrub.

### Changed

- **BREAKING: nesting now defaults to off, and `--nesting` is replaced by
  `--allow-nesting`.** Previously LXC nesting defaulted on (via the
  `--nesting`/`--no-nesting` flag) and VM mode passed `-cpu host`
  verbatim (which exposed vmx/svm on a nesting-enabled host). Both are
  now off by default and gated behind the single `--allow-nesting` flag.
  To restore the old behavior, pass `--allow-nesting` at create time.
  - LXC / PVE-LXC: a default `create` no longer includes `nesting.conf`,
    the fuse/tun bind mounts, or `features: nesting=1`. The
    `apparmor.profile = generated` systemd-256 fix is unaffected (it is
    gated on LXC mode, not on the nesting flag), so guests still boot.
  - VM / PVE-VM: kento now emits `-cpu host,vmx=off,svm=off` by default,
    deterministically masking the virt extensions even on a
    nesting-enabled host. With `--allow-nesting` it emits `-cpu host`.
    For PVE-VM the `-cpu` is injected into kento's `args:` payload and
    re-emitted on `scrub`.
  - Deployments relying on default-on LXC nesting (e.g. nested-container
    sandboxes) must add `--allow-nesting` to their `create` invocations.

### Fixed

- VM start no longer holds the caller's stdin / controlling session.
  `start_vm` launched QEMU (which binds the serial console to stdio under
  `-nographic`) and virtiofsd with stdout/stderr redirected but stdin
  inherited and no new session, so QEMU held the caller's fd 0. Over a
  non-interactive ssh-exec channel or in a pipeline, `kento start` would
  hang until the VM exited. Both daemons now launch with `stdin=DEVNULL`
  and `start_new_session=True`.
- DHCP-mode LXC instances now get an `eth0` network unit. kento injected
  an `eth0` `.network` only when a static IP was set; with bridge
  networking and no `--ip` it relied on the image's own networkd config.
  VM-oriented images match `Name=en*` (predictable NIC naming) while the
  LXC veth is `eth0`, so the container never received a DHCP lease. kento
  now writes a `10-dhcp.network` (`Name=eth0`, `DHCP=yes`) for LXC and
  PVE-LXC bridge-with-DHCP instances; `--network none` is unaffected.

## [1.2.1] - 2026-06-07

### Changed

- `kento list` no longer runs `du -sh upper/` per row by default. On
  long-running containers the per-row `du` dominated wallclock (>5s
  observed for a handful of instances) since `upper/` accumulates many
  small files. The UPPER SIZE column is now opt-in via `--size` / `-s`;
  the same data remains available per-instance via `kento info <name>
  --verbose`.

### Fixed

- `kento vm stop` against a pve-vm instance no longer hangs forever when
  the guest ignores ACPI. The default `qm shutdown` now uses
  `--timeout 30 --forceStop`, falling through to a hard stop after the
  graceful window elapses. New `--timeout N`, `--graceful-only`, and
  (existing) `--force` flags expose the bounded-shutdown knobs; conflicting
  combinations are rejected with a clear error. A warning is emitted when
  qm reports it had to fall through to SIGTERM.

## [1.2.0] - 2026-04-24

Tier 1 test harness and QEMU/PVE pass-through flags. Purely additive —
no existing behavior changes.

### Changed

- Companion project `tenkei` has been renamed to `gemet`. Docs, the VM
  mode overview, and the e2e harness's image references now point at
  `github.com/doctorjei/gemet` and the `gemet-*-kento` image tags. No
  code or behavior change in kento — this is a documentation-level sync
  with the renamed companion repo. Users upgrading from v1.1.0 who were
  looking for the old `tenkei` repo should follow the new name.
- README `Related projects` section and `docs/vm-mode.md` now reflect
  the kento umbrella positioning: kento is the top-level brand, with
  gemet and droste as subprojects and kanibako as an independent
  consumer. Repos remain federated; this is a documentation-only
  clarification of the relationships.

### Added

- Tier 1 integration test harness under `tests/integration/`. Subprocess
  execution of the real generated `kento-hook` against a `tmp_path`
  LXC-style state dir, catching hook-runtime bugs that template-level
  mocks miss. Covers hook-version v0 and v1 invocation shapes, the
  `pre-mount` overlayfs branch (skipped when not root), `post-stop`
  cleanup, the DHCP port-forward worker, and PVE-inner ns cgroup writes.
- `Makefile` with `test`, `test-integration`, and `test-all` targets.
  Default `pytest tests/` still runs only the unit suite; the integration
  tier runs via `make test-integration` and is fast (~0.3 s on the agent
  box).
- `--qemu-arg <string>` pass-through flag on `kento vm create` (repeatable).
  Each value is appended verbatim to the QEMU command line after kento's
  own flags, so `--qemu-arg '-m 2048'` overrides the kento-provided
  `-m 512`. Rejected on `kento lxc create`.
- `--pve-arg <string>` pass-through flag for PVE modes (repeatable). Each
  value is appended verbatim as a line in the generated PVE LXC or qm
  config. Rejected on plain LXC, plain VM, and any invocation with
  `--no-pve`.
- Short denylists (`QEMU_ARG_DENYLIST`, `PVE_ARG_DENYLIST` in
  `defaults.py`) reject pass-through values that would collide with
  kento-managed keys (`-kernel`, `-initrd`, `memfd`, `rootfs:`, `arch:`,
  `hostname:`, `lxc.rootfs.path`, etc.). Everything else is permitted —
  pass-through is an escape hatch, not a vetted API.
- `kento info --verbose` surfaces any `--qemu-arg` / `--pve-arg` values
  the instance was created with under a new "Pass-through flags:"
  section. `kento info --json` gains top-level `qemu_args` and `pve_args`
  keys (always present, empty list when unset) for stable machine
  consumption.
- `KENTO_TEST_NS_CGROUP` environment variable on `hook.sh` as a test-only
  override for the PVE-inner ns cgroup write path. Production invocations
  leave it unset and the hook derives the path from the container ID as
  before.

### Fixed

- `kento vm stop <name>` and `kento vm rm <name>` (and `kento vm scrub`)
  now correctly detect PVE-VM mode instead of hardcoding `mode="vm"` in
  the `vm` CLI scope. Previously the scoped form short-circuited mode
  resolution at `cli.py` `_dispatch_multi`, so the running/stopped check
  for a `pve-vm` instance consulted the plain-VM `kento-qemu-pid` file
  (always absent on PVE-VM) and returned False. Symptom: `kento vm stop`
  printed "Already stopped" on a running PVE-VM while `kento vm list`
  (which reads `kento-mode` correctly) reported "running", and the
  subsequent `kento vm rm` failed with "umount: target is busy" because
  the PVE-owned QEMU still held virtiofsd and the overlay. The scope
  now reads `kento-mode` the same way `list`, `info`, and `resolve_any`
  always did. The top-level shortcut form (`kento stop <name>`) was
  unaffected.
- `kento vm rm -f` and `kento vm stop -f` now retry a busy rootfs
  unmount after clearing stray processes (`fuser -km`) and fall back
  to lazy unmount under `-f`, so a wedged virtiofsd/QEMU no longer
  blocks teardown.
- `is_running()` now applies a 5s timeout to `qm status` and `pct
  status` so an unreachable PVE doesn't hang `stop`/`destroy`
  indefinitely; on timeout we assume the instance may be running and
  attempt the stop rather than short-circuiting.

## [1.1.0] - 2026-04-23

Plain-LXC AppArmor default flipped to `lxc.apparmor.profile = generated`,
fixing two long-standing traps on modern OCI images. Plus a sweeping
edge-case and error-message audit (F1-F19, C1-C9).

### Changed

- Plain-LXC mode now emits `lxc.apparmor.profile = generated` together
  with `allow_nesting=1` and `allow_incomplete=1` by default. `generated`
  is a built-in LXC feature that builds a per-container AppArmor profile
  enforcing the host/container boundary while labeling in-container
  processes as `:unconfined`. The earlier short-lived `--unconfined`
  flag is gone.
- Memory and CPU limits now reach PVE-LXC guests. Kento emits
  `lxc.cgroup2.memory.max` and `lxc.cgroup2.cpu.max` alongside PVE's
  `memory:` / `cpulimit:` shorthand, and on PVE-LXC the hook propagates
  the limits into the inner `ns/` cgroup at start-host time so
  memory-aware runtimes (JVMs, etc.) read the real values from inside
  the container instead of `max`.
- `kento info` resolver errors and several other user-facing messages
  were harmonized around a single "no X named 'N'. Run 'kento list' to
  see..." format. Messages emitted from hook scripts are now prefixed
  with `kento-hook:` or `kento-inject:` so the origin is visible at a
  glance.
- `kento stop` on PVE-VM instances now passes `--timeout 60 --forceStop 1`
  to `qm shutdown`, so guests without `acpid` fall through to a hard stop
  instead of hanging.
- Plain VM defaults to `--network usermode` when no `--network` flag is
  given; plain-VM `--network bridge` is rejected up front with a pointer
  to usermode or PVE as alternatives.
- `start` and `stop` are now idempotent across all four modes: a
  container that is already running (or already stopped) exits 0 with a
  short "Already running/stopped: <name>" message rather than leaking a
  `CalledProcessError` traceback.

### Added

- `KENTO_APPARMOR_PROFILE` environment variable (accepts `generated` or
  `unconfined`, default `generated`). Escape hatch for nested LXC where
  the outer is already confined by `generated` and in-container
  `apparmor_parser` calls are blocked.
- `KENTO_STATE_DIR` environment variable overrides the writable-layer
  base directory. Sidesteps overlay-on-overlay when kento runs inside an
  LXC whose rootfs is itself overlayfs.
- End-to-end test harness moved into the repo at `tests/e2e/kento-e2e.sh`
  (199/201 passing; the two gaps are environmental). TAP output, four
  sections covering plain LXC, pve-lxc, pve-vm, and nested-LXC tier 3.
- `run_or_die` subprocess wrapper, `kento_lock` flock helper, and
  `validate_name` CLI helper. Replace a swath of `subprocess.run(...,
  check=True)` call sites that previously leaked `CalledProcessError`
  tracebacks.

### Fixed

- Plain-LXC containers on modern OCI images (systemd 256+) no longer
  fail with `status=243/CREDENTIALS` / missing DHCP. Root cause was the
  stock `lxc-container-default-with-nesting` AppArmor profile denying
  the credentials tmpfs mount; the new `generated` default allows it.
- PAM `unix_chkpwd` no longer trips glibc's `_dl_protect_relro` check
  under plain AppArmor. SSH password logins work again on plain-LXC.
- PVE-LXC port forwarding now works. PVE silently drops
  `lxc.hook.start-host`, so the nftables DNAT rules never got installed
  when running under `pct`; kento now installs them from `pre-mount` in
  the host network namespace (via `nsenter` when the hook finds itself
  in a container netns) with an idempotency marker so the `start-host`
  path on plain LXC stays safe.
- `kento start --port` no longer hangs on DHCP. The old path called
  `lxc-info -iH` inside the `start-host` hook, which deadlocked against
  the same LXC monitor that was waiting for the hook to return; the
  DHCP branch now spawns a `setsid` worker that polls `lxc-info` after
  the monitor is free.
- Plain-LXC `hook.version=1` invocations (which pass no positional
  args, only `LXC_*` env vars) no longer abort under `set -u` when the
  start-host branch dereferences `$1`. The hook now uses
  `CONTAINER_ID="${LXC_NAME:-${1:-}}"`.
- `kento scrub` on PVE-VM no longer wedges the next `qm start`. Scrub
  was regenerating an LXC-shaped hook over the VM hookscript; it now
  branches on mode and writes the right shape.
- PVE-VM `kento scrub` after `qm set --memory` no longer launches with
  the old memfd size. Scrub re-reads `memory:` from the qm config and
  rewrites `size=` in `args:` accordingly.
- Cloud-init detection now catches images that ship the binary at
  `/usr/sbin/cloud-init` or split the systemd unit onto a layer
  separate from `cloud.cfg`.
- Duplicate instance names across VMIDs and namespaces are now
  rejected. The old check used `(base_dir / name).exists()`, which
  never matched PVE directories (named by VMID) and didn't cross the
  LXC / VM boundary.
- `kento scrub` is crash-safe: the `upper` / `work` clear is now
  rename-then-mkdir-then-rm so a crash mid-scrub never leaves the
  overlayfs mount point missing. Next scrub sweeps any stray `.old`
  dirs.
- Argparse-level validators now reject nonsense values before they
  reach `create()`: `--port` must be `auto` or `HOST:GUEST` in
  `[1,65535]`; `--memory` and `--cores` must be `>= 1`; `--ip` must
  parse via `ipaddress.ip_interface`; `--network bridge=<name>` checks
  `/sys/class/net/<name>` exists; `--mac` rejects multicast and
  broadcast addresses.
- Instance-name validation at both the CLI entry and the resolver
  entry defends against shell injection via hook templates and
  path-traversal via state-directory paths.
- `--ip` / `--gateway` with `usermode`, `host`, or `none` networking
  are rejected up front (previously silently accepted, producing
  broken configs).
- `--mac` on `kento lxc` scope is rejected at the CLI (previously
  silently dropped inside `create()`).
- `--config-mode cloudinit` on an image without cloud-init is now a
  hard error with a pointer to `--config-mode injection`. `auto` still
  falls back silently.
- `kento destroy -f` on a wedged instance continues through cleanup
  even if the stop step fails.
- `podman pull` failures due to a missing `podman` binary now print a
  clean install hint instead of a raw Python traceback.
- `__version__` now reads from `importlib.metadata` at import time, so
  it can't fall out of sync with `pyproject.toml` on version bumps.

## [1.0.2] - 2026-04-14

### Fixed

- virtiofsd now survives PVE hookscript scope teardown on PVE-VM
  instances. PVE runs hookscripts in a systemd scope that reaps all
  children on exit; kento launches virtiofsd under `setsid` so it
  runs in its own session and the VM's rootfs share stays up.

## [1.0.1] - 2026-04-14

### Fixed

- PVE-VM `qm start` no longer hangs indefinitely. The hookscript now
  redirects virtiofsd stdio so PVE's pipes close and the start proceeds.
- `kento vm create` on PVE now pre-validates the snippets storage
  before any filesystem writes, so a misconfigured storage never leaves
  half-populated state with a reserved name. The error message points
  at the exact `pvesm set` command needed for the caller's storage.
- PVE-VM `create()` failures after the instance directory is made are
  now rolled back cleanly.

## [1.0.0] - 2026-04-11

First production release.

Kento composes OCI container images into system containers and QEMU VMs
via overlayfs, reading podman's layer store directly. This release
introduces the noun-verb CLI (`kento lxc <cmd>`, `kento vm <cmd>`) with
four modes (plain LXC, PVE-LXC, plain VM, PVE-VM, auto-detected or
forced via `--pve` / `--no-pve`), the `--memory` / `--cores` resource
flags across all four modes, image hold pinning so composed images
can't be garbage-collected out from under running instances, cloud-init
NoCloud seed support alongside the shell-based injection path, stable
auto-generated MACs for VM and PVE-VM, and the `kento info` /
`kento list` / `kento scrub` instance-management verbs. Python 3.11+,
stdlib-only CLI, POSIX shell hook.
