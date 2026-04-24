# Changelog

All notable changes to kento are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
