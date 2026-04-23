# End-to-end test harnesses

TAP-format shell harnesses that exercise kento against real LXC / PVE /
QEMU on a live host. They complement the pytest unit suite (which mocks
`subprocess` and the podman store).

## `kento-e2e.sh`

The full sweep. Runs on a PVE host with kento installed and podman /
`pct` / `qm` on PATH. Covers:

- **Section A** — core lifecycle (create → start → guest verification →
  scrub → stop → destroy → error paths) for all four modes: `lxc`,
  `pve-lxc`, `vm`, `pve-vm`. Also feature-isolation tests (plain, SSH
  key only, `--memory`, `--cores`, `--port`, `--ip`).
- **Section B** — Yggdrasil image lifecycle (`tenkei-bifrost-kento`).
- **Section C** — cloud-init integration on `droste-hair` (LXC).
- **Section D** — nested-LXC (tier 3): runs a `pve-lxc` outer and
  creates a plain-LXC inner from an in-development kento wheel pushed
  into the outer.

Run on the PVE host as root:

```
sudo ./tests/e2e/kento-e2e.sh
```

Exit code is 0 iff all tests pass. Individual failures also leave
`/tmp/e2e-diag-<N>.log` diagnostic dumps.

### Environment prerequisites

- Root-side podman store has the test images pulled:
  - `localhost/tenkei-bifrost-kento:1.4.2`
  - `ghcr.io/doctorjei/droste-hair:latest` (Section C only)
- `/home/droste/kento-src/` contains a clean copy of the kento source
  tree (Section D builds a wheel from it).
- `localhost/kento-test-minimal:latest` built via
  `tests/fixtures/build.sh` (Section D inner fixture).
- `lxcbr0` bridge exists and `pve-firewall` is either off or
  lxcbr0 is whitelisted.

## `e2e-phase3.sh`

Targeted harness from the pre-1.0 Phase 3 work (port forwarding +
cloud-init). Kept for historical repro and because it's shorter than
the full sweep when debugging a specific regression.

```
sudo ./tests/e2e/e2e-phase3.sh <test-image>
```
