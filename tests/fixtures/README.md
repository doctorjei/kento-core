# Test Fixtures

## minimal-oci

A tiny busybox-based OCI image used by kento's nested-LXC test plans:

- **Tier 2** — real LXC-in-LXC test harness (the "inner" container).
- **Tier 3** — E2E `SECTION D` nested-LXC tests run on the PVE test VM.

The image's `/sbin/init` is a shell script that loops `sleep 3600` forever,
keeping the container alive without pulling in systemd, DHCP clients, or
networkd. This minimizes the moving parts when validating kento's lifecycle
(create / start / stop / destroy) inside a nested LXC.

Base: `busybox:latest` (~4-5 MB compressed). Chosen over alpine because it
is smaller and already ships `/bin/sh` + `mount` without any package
installs.

### Build

```sh
./tests/fixtures/build.sh
```

The script is idempotent — rerunning re-tags the image. Requires `podman`
in PATH and first-build network access to docker.io.

### Consume

Tests reference the image by tag:

```
localhost/kento-test-minimal:latest
```

Example:

```sh
kento lxc create --no-pve \
    localhost/kento-test-minimal:latest --name probe
```

### Notes

- The image lives in root's podman store (kento uses root's store, not
  per-user storage), so `build.sh` may need `sudo` depending on how the
  test harness invokes it.
- No cloud-init, no systemd — so kento falls into the `injection` path.
