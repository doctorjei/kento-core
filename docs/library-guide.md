# Kento as a library

`kento-core` ships an importable, typed object model. This guide is the front
door for a Python consumer that does `import kento` — it creates and manages LXC
containers and QEMU/KVM virtual machines directly, without shelling out to the
`kento` CLI.

Two things to know up front:

- **Classes only.** The library boundary is the typed object model
  (`SystemContainer`, `VirtualMachine`, the `Image` and reference families, the
  `Result` family). There is no JSON at the library edge — JSON is a concern of
  the CLI's output layer, not of a library consumer.
- **`Result`-native.** Predictable outcomes (a name that does not exist, a
  create that fails validation, a graceful stop that times out) are returned as
  a [`Result`](#handling-results) value, **not** raised. Exceptions are reserved
  for *panics* — programming errors and broken invariants. So you branch on the
  returned `Result`; you do not wrap every call in `try`/`except`.

The full per-symbol API reference is generated from the docstrings with
[pdoc](#api-reference) — this guide is the narrative map on top of it.

> **Root.** Creating, starting, and destroying real instances touches
> `/var/lib/lxc`, `/var/lib/kento`, LXC/QEMU, and the network — so these
> operations require **root** (run your program as root or under `sudo`).
> `kento.require_root()` raises `StateError` if you are not root; the runtime
> paths enforce it for you.

---

## Quickstart

Create a VM from an OCI image, start it, inspect it, and destroy it — checking
the `Result` at each step:

```python
import kento
from kento import VirtualMachine

# create() returns a Result[VirtualMachine] — it does NOT raise on a
# predictable failure (bad image, name clash, validation error).
result = VirtualMachine.create("web", "docker.io/library/debian:trixie",
                               start=True)

if result.is_error():
    # Error carries no value; inspect its conditions to see what went wrong.
    for c in result.conditions:
        print(f"{c.severity.name}: {c.kind.value}: {c.message}")
    raise SystemExit(1)

vm = result.unwrap()          # the VirtualMachine handle (Ok or Warning → value)
print(vm.name, vm.status)     # e.g. "web" Status.RUNNING

# Lifecycle methods likewise return Result[None].
vm.stop().unwrap()            # graceful stop (raises ResultError only if it failed)
vm.destroy(force=True).unwrap()
```

`SystemContainer` (LXC) has the same shape:

```python
from kento import SystemContainer

ct = SystemContainer.create("ct1", "docker.io/library/debian:trixie").unwrap()
ct.start().unwrap()
print(ct.exec(["hostname"]).unwrap())   # runs in the guest, returns exit code
ct.destroy(force=True).unwrap()
```

---

## The object model at a glance

Everything a consumer touches is re-exported flat from the `kento` package
(`from kento import VirtualMachine`, `from kento import OciReference`, …).

### Instance family — the live handles

- **`Instance`** — the abstract base. Its classmethods
  (`get`, `list`, `adopt`) are *polymorphic*: called on `Instance` they span
  both namespaces; called on a subclass they narrow to that kind.
- **`SystemContainer`** — an LXC / PVE-LXC system container.
- **`VirtualMachine`** — a QEMU/KVM virtual machine (VM / PVE-VM).

You never instantiate these directly — you obtain a handle from a classmethod
(`create`, `transient`, `get`, `list`, `adopt`). `Instance.create()` /
`Instance.transient()` on the abstract base raise (call the concrete kind).

### Image & reference families — inert value types

All frozen, all importable, no I/O on construction:

- **References** (`SourceReference` subclasses): `OciReference` (an `oci://`
  image ref), `UrlReference` (an `http(s)://` locator), plus `Endpoint` and
  `Digest`. `SourceReference.parse(str)` returns a `Result`.
- **Images** (`Image` subclasses): `OciImage` (podman-store backed),
  `LocalDirectoryImage` (a fetched-and-extracted `.txz` rootfs — the URL-VM
  representation), and `LayeredImage` / `VolumeImage` / `CompositeImage`.
  `Instance.image()` resolves a handle's boot source to a concrete `Image`.
- **Records / pins**: `ImageRecord`, `ManagedStatus`, `Hold`, `Layer`,
  `DiskFormat`.

### Configuration value types

Typed inputs you pass to `create`:

- `NetworkConnection` / `NetworkMode` — the network attachment.
- `PlatformProfile` / `PlatformMode` — the STANDARD-vs-PVE axis (+ vmid, pve
  args).
- `StorageMode` — the root-storage strategy (`OVERLAY` in 1.0).
- `Status` — the observed lifecycle state (`RUNNING` / `STOPPED` / … — read
  only).

### Diagnosis value types

`Diagnosis` / `Finding` (+ `DiagnosisDomain`, `CheckLevel`, `ReclaimReport`) —
returned by `kento.diagnose()` and `instance.diagnose()`.

### The Result family

`Result` and its three subclasses `Ok` / `Warning` / `Error`, plus `Condition`,
`Severity`, `ConditionKind`, and `ResultError`. See the next section.

---

## Handling results

Almost every verb on the surface returns a `Result[T]`. The family is a
three-way split (frozen value types — a `Condition` is plain data, never an
exception):

- **`Ok[T]`** — clean success. Carries `value`; may carry sub-warning
  (`INFO`/`NOTE`) conditions.
- **`Warning[T]`** — success *with caveats*. Carries **both** `value` and at
  least one `WARNING` condition (e.g. a URL that redirected to cleartext).
- **`Error`** — failure. Carries `conditions` (at least one `ERROR`) and **no
  `value` attribute at all** — reading `.value` off an `Error` is an
  `AttributeError`, not a `None` you forgot to check.

The idiomatic pattern — check, then use:

```python
r = VirtualMachine.get("web")

if r.is_ok():                 # True for Ok AND Warning (a value is present)
    vm = r.value              # or r.unwrap()
    # surface any caveats without stopping
    for c in r.conditions:
        log.warning("%s: %s", c.kind.value, c.message)
else:                          # r.is_error()
    err = r.conditions[0]
    log.error("%s: %s", err.kind.value, err.message)
```

Each `Condition` carries:

- `severity` — a `Severity` (`INFO < NOTE < WARNING < ERROR`).
- `kind` — a `ConditionKind`; its `.value` is a stable snake_case string
  (`"instance_not_found"`, `"validation"`, `"size_exceeded"`, …) you can branch
  on programmatically.
- `message` — human-readable text.
- `context` — an immutable mapping of structured extras (e.g.
  `{"url": ..., "cap": ..., "got": ...}`).

### `unwrap()` — crossing back to exceptions

`unwrap()` is the single sanctioned bridge from the `Result` channel back to the
exception channel:

- `Ok` / `Warning` → returns `value`.
- `Error` → raises `ResultError` (built from the first `ERROR` condition; the
  full `conditions` tuple is attached as `.conditions`).

Use `unwrap()` when you *want* a failure to become an exception (scripts,
`transient` teardown). Use `unwrap_or(default)` to substitute a default on
`Error`. Use the `is_ok()` / `is_error()` branch when you want to handle the
failure inline.

---

## URL-VM from the library

A `VirtualMachine` can boot directly from a `.txz` rootfs fetched over
`https://`, with the kernel and initramfs fetched from their own URLs — no OCI
store involved. This is **VM-only** and the rootfs is **ephemeral** (fetched and
extracted at start, discarded on destroy).

```python
from kento import VirtualMachine

vm = VirtualMachine.create(
    "urlvm",
    "https://host.example/rootfs.txz",          # image: an https .txz rootfs
    kernel="https://host.example/vmlinuz",       # a URL ...
    initramfs="https://host.example/gemet-initramfs.img",
    start=True,
).unwrap()
```

Notes:

- **`image`** accepts a `str` (OCI ref *or* an `https://…/rootfs.txz` URL for VM
  modes), an `OciReference`, or an `Image`. An `https://` rootfs on a
  `SystemContainer` (LXC) is rejected — it is a VM-only source.
- **`kernel`** and **`initramfs`** each accept a **local filesystem path OR an
  `https://` URL string**, independently. A local file is copied into the
  instance directory; a URL is fetched into it. `None` (the default) falls back
  to the in-image `/boot`. (The gemet reference kernel/initramfs boot a flat
  rootfs with no `/lib/modules`; that is the caller's image contract, not
  enforced by kento.)
- **Fetch cap.** URL fetches are size-capped. The default cap is **2 GiB**,
  overridable with the `KENTO_URL_MAX_BYTES` environment variable. Exceeding it
  surfaces a `Condition` with `kind` `size_exceeded`.
- **Redirect.** An `https://` URL that a server redirects *down* to cleartext
  `http://` is followed, but surfaced as a `Warning` (`kind`
  `insecure_redirect`) so the cleartext hop is visible.

Because `create()` returns a `Result`, a fetch/extract failure comes back as an
`Error` (e.g. `kind` `fetch_failed`, `http_error`, `extract_failed`) rather than
an exception — branch on it exactly as any other create outcome.

---

## Lookup & lifecycle

### Finding instances

```python
from kento import Instance, SystemContainer, VirtualMachine

Instance.get("web")            # Result[Instance] — the concrete kind for "web"
VirtualMachine.get("web")      # Result[VirtualMachine] — narrows to the VM namespace
Instance.list()                # Result[list[Instance]] — every instance, both kinds
SystemContainer.list()         # Result[list[SystemContainer]] — LXC only
```

`get` on the base spans both namespaces (an ambiguous name present as both an
LXC and a VM is an `Error`); on a subclass it narrows, so a `create --force`
duplicate resolves to that kind. `list` is total over the store — a single
corrupt/mid-destroy entry is skipped (with a log), never fatal to the listing.

`adopt(name)` heals an orphaned PVE instance (state dir survives, `.conf`
destroyed out-of-band) and returns its handle; it does not auto-start (call
`start()` afterward).

### Lifecycle on a handle

Each returns a `Result`:

- `start() -> Result[None]` — boot (idempotent; re-resolves and caches status).
- `stop(*, timeout=None, force=False) -> Result[None]` — graceful by default
  (**never** hard-kills; a still-running guest yields an `Error` with `kind`
  `stop_timeout`). `force=True` hard-kills (immediately, or after a `timeout`
  grace window).
- `destroy(*, force=False) -> Result[None]` — remove the instance + its writable
  layer. `force=False` on a running instance is an `Error` (`invalid_state`);
  `force=True` stops first. After a successful destroy the handle is **dead** —
  any further call returns `Error(instance_not_found)`.
- `attach() -> Result[None]` — interactive console; **blocks** until you detach.
  The wrapped tool's exit code is captured on `attach_exit_code`.
- `exec(command, *, tty=False, user=None, env=None) -> Result[int]`
  (`SystemContainer` only) — run a command in the guest; the `Ok` value is the
  **exit code**, and a non-zero code is *not* an error (it is normal
  information). VMs have no in-guest agent — use SSH.
- `logs(*, follow=False, lines=None, args=()) -> Result[Iterator[str]]`
  (`SystemContainer` only) — a line iterator over the guest journal.
- `refresh() -> None` — re-read this handle's snapshot in place.
- `diagnose() -> Diagnosis` — this instance's findings.

VM-only: `suspend()` / `resume()` (pause vCPUs to RAM — `Result[None]`).

### Settable properties (stopped-only, unless noted)

Assign a whole typed value; the setter persists it (and, for `forwards`, applies
it live on a running instance):

```python
from kento import ForwardProtocol

vm.hostname = "web01"                          # stopped-only
vm.resources = {"memory": 2048, "cores": 2}    # LXC live-capable; VM stopped-only
# forwards: {HostBinding: GuestTarget}
#   HostBinding = (ForwardProtocol, host_ip_or_None, host_port)
#   GuestTarget = (guest_ip_or_None, guest_port)
vm.forwards = {(ForwardProtocol.TCP, None, 8080): (None, 80)}  # 8080 → guest 80
```

`hostname`, `network`, `resources`, `forwards`, `extra_args` are settable (plus
`qemu_args` on `VirtualMachine`, `lxc_args` on `SystemContainer`). `status`,
`name`, `sources`, `storage`, `created`, `directory` and friends are getter-only
(observed identity/state). A setter on a *running* instance that requires a stop
raises `StateError` (the persist path is not a `Result`).

---

## Transient (ephemeral) instances

`transient(...)` is a context manager with the **same parameters as `create`**;
the instance is **guaranteed torn down on exit** (`destroy(force=True)` runs
whether the block exits normally or via an exception):

```python
from kento import VirtualMachine

with VirtualMachine.transient("scratch", "docker.io/library/debian:trixie",
                              start=True) as vm:
    ...                        # use vm here
# vm is destroyed here — even if the block raised
```

`transient` is the **only** context-manager entry — a plain `create()`/`get()`
handle is not a context manager, so `with VirtualMachine.create(...)` raises
`TypeError`. Because a `@contextmanager` cannot return a `Result`, `transient`
`unwrap()`s internally: a failed create (or a failed teardown) **raises** out of
the `with`.

---

## API reference

The full per-symbol reference is generated from the docstrings with
[pdoc](https://pdoc.dev):

```
pip install -e '.[docs]'      # installs pdoc (the `docs` extra)
make docs                     # renders HTML into docs/api/  (git-ignored)
```

Then open `docs/api/index.html`. The rendered HTML is a build artifact and is
not committed — the docstrings in `src/kento/` are the source of truth.
