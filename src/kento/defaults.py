"""Kento default configuration values."""

from pathlib import Path

# --- Overlay layer cap ---
# Docker overlay2 parity. The kernel caps classic mount(2) options at one
# 4096-byte page; an over-deep lowerdir overruns it and the kernel SILENTLY
# TRUNCATES, corrupting upperdir/workdir and failing the overlay mount. kento
# mounts from short l/<short> symlinks + chdir (like Docker/podman) to stay
# small, and fails closed at create above this many layers rather than letting
# the kernel truncate. Lift once the new mount API (fsconfig, what
# LIBMOUNT_FORCE_MOUNT2 targets) is reliably available — it has no single-page
# limit (currently a silent no-op on some util-linux/kernel combos).
MAX_OVERLAY_LAYERS = 128

# The single-page mount(2) options limit. We compute the exact options string
# kento will emit and refuse to hand the kernel anything that could truncate,
# leaving a 16-byte safety margin. With the l/<short> short-link form the 128
# cap keeps us far under this; the byte check is a backstop for pathological
# state-dir / name lengths.
OVERLAY_OPTS_PAGE_LIMIT = 4096

# Maximum instance-name length. The name flows into STATE_DIR (e.g.
# /var/lib/kento/vm/<name>), which is the upperdir/workdir of the overlay
# mount-options string -- the last otherwise-uncapped contributor to that
# budget (the layer count + byte backstop above cap the rest). It also becomes
# the guest hostname, so HOST_NAME_MAX (64) is the natural ceiling.
MAX_INSTANCE_NAME = 64  # HOST_NAME_MAX; the name becomes the guest hostname, and
                        # bounds the overlay mount-options budget (see layers.py)

# --- LXC defaults ---
LXC_TTY = 2
LXC_MOUNT_AUTO = "proc:mixed sys:mixed cgroup:mixed"
LXC_MOUNT_AUTO_NESTING = "proc:rw sys:rw cgroup:rw"
LXC_NESTING = False

# AppArmor rules that let modern systemd (256+) boot inside an LXC guest. systemd
# sandboxes its own core units (networkd, resolved, logind, journald, ...) with
# PrivateUsers=/PrivateMounts=, which create a user namespace and do bind/move/
# remount/pivot_root mounts. AppArmor 4.x (Debian 13 / kernel 6.x) mediates
# userns_create and these mounts; the LXC-generated profile denies them by default,
# so the guest comes up network-dead. These rules grant exactly that bounded
# sandboxing vocabulary -- narrower than allow_nesting (no nested-container peer
# rules, no raw proc/sys). Injected via lxc.apparmor.raw, which is only honored with
# a generated profile. Validated clean on droste-loom + kanibako-lxc (Debian 13,
# systemd 257) on a real PVE host.
APPARMOR_SYSTEMD_RULES = ("userns,", "mount,", "umount,", "pivot_root,", "mqueue,")

# --- PVE-LXC defaults ---
# PVE has no "unlimited" memory sentinel: it rejects `memory: 0` and
# `memory: max` at schema validation, and it silently backfills its 512 MiB
# schema default whenever the `memory:` field is omitted -- then enforces that
# 512 MiB host-side on the container cgroup (confirmed live on PVE 9.1.6: an
# empty conf yields memory.max = 536870912). So an omitted field is NOT
# unlimited on pve-lxc; it is a surprise 512 MiB cap (real-world OOM-kills).
# This value is PVE's schema ceiling (2^44 - 1 MiB, "signed int max"); it is
# accepted by pct, and the resulting byte value exceeds the cgroup's
# representable maximum so the kernel clamps memory.max to the literal string
# `max` -- i.e. truly unlimited. Chosen over detecting host RAM deliberately,
# so the value survives PVE cluster live-migration / extreme hosts (a host-RAM
# value baked in at create time could under-cap after the container migrates
# to a smaller node).
PVE_LXC_UNLIMITED_MEMORY_MB = 17592186044415

# --- VM defaults ---
VM_MEMORY = 512          # MB
VM_CORES = 1
VM_KVM = True
VM_MACHINE = "q35"
VM_SERIAL = "ttyS0"
VM_DISPLAY = False       # -nographic

# --- Pass-through denylists (v1.2.0 Phase B) ---
# Substrings that, if present anywhere in a --qemu-arg value, get rejected.
# Kept short on purpose: pass-through is an escape hatch, so err on the side
# of permitting. These specifically name flags kento already emits in vm.py
# / pve.py generate_qm_args — re-emitting them would either duplicate or
# conflict with the kento-managed version.
#   -kernel / -initrd : kento owns these (boot from image-provided kernel;
#     future --kernel/--initrd will be dedicated flags).
#   virtiofs / rootfs (inside an arg): kento's virtiofs share — a second
#     -device or -drive naming either would collide with the mount tag.
#   memory-backend-memfd / memfd-size : kento generates this and scrub
#     resyncs its size= to PVE's memory: field.
#   -chardev / -serial : reserved for v1.4.0 VM interactive (serial socket).
QEMU_ARG_DENYLIST = (
    "-kernel",
    "-initrd",
    "virtiofs",
    "rootfs",
    "memory-backend-memfd",
    "memfd-size",
    "-chardev",
    "-serial",
)

# Substrings that, if present in a --pve-arg value, get rejected.
# Target only kento-managed keys that would silently clobber generated
# config: rootfs path, mp0 mount (reserved for virtiofs-equivalent future
# work), arch, hostname. Everything else (tags, onboot, unprivileged,
# features, lxc.* raw keys, etc.) is fair game.
PVE_ARG_DENYLIST = (
    "rootfs:",
    "mp0:",
    "lxc.rootfs.path",
    "arch:",
    "hostname:",
)

# Substrings that, if present in a --lxc-arg value, get rejected. These are
# the keys generate_config() (create.py) emits structurally for plain-LXC's
# native config, plus the two cgroup lines `kento set` manages. Re-emitting
# any of them via pass-through would either duplicate or clobber the
# kento-managed line — the very wiring (rootfs, hooks, network, apparmor,
# mount/tty, resource limits) that makes the instance boot.
#   lxc.uts.name           : container name (kento owns it).
#   lxc.rootfs.path        : overlay rootfs dir (kento owns it).
#   lxc.hook.              : pre-start/post-stop/start-host/version hooks.
#   lxc.net.               : the veth/none NIC wiring (--network owns it).
#   lxc.mount.auto         : the proc/sys/cgroup auto-mounts.
#   lxc.tty.max            : kento default.
#   lxc.apparmor.          : profile/allow_nesting/allow_incomplete.
#   lxc.cgroup2.memory.max : kento manages via --memory / `kento set`.
#   lxc.cgroup2.cpu.max    : kento manages via --cores / `kento set`.
# Everything else (lxc.mount.entry, lxc.environment, lxc.cgroup2.* other
# than the two above, lxc.idmap, lxc.cap.*, etc.) is fair game.
LXC_ARG_DENYLIST = (
    "lxc.uts.name",
    "lxc.rootfs.path",
    "lxc.hook.",
    "lxc.net.",
    "lxc.mount.auto",
    "lxc.tty.max",
    "lxc.apparmor.",
    "lxc.cgroup2.memory.max",
    "lxc.cgroup2.cpu.max",
)


# --- Config file paths ---
CONFIG_DIR = Path("/etc/kento")
LXC_CONFIG_FILE = CONFIG_DIR / "lxc.conf"
VM_CONFIG_FILE = CONFIG_DIR / "vm.conf"

# --- Type parsers ---
_BOOL_TRUE = {"true", "yes", "1", "on"}
_BOOL_FALSE = {"false", "no", "0", "off"}


def _parse_bool(value: str) -> bool:
    low = value.strip().lower()
    if low in _BOOL_TRUE:
        return True
    if low in _BOOL_FALSE:
        return False
    raise ValueError(f"invalid boolean: {value!r}")


def load_config(path: Path) -> dict[str, str]:
    """Read a key=value config file.

    Skips comments (lines starting with #) and blank lines.
    Returns empty dict if the file doesn't exist.
    """
    if not path.is_file():
        return {}
    result: dict[str, str] = {}
    text = path.read_text()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        result[key.strip()] = value.strip()
    return result


def get_vm_defaults() -> dict[str, object]:
    """Return VM defaults, overridden by values from VM_CONFIG_FILE."""
    defaults: dict[str, object] = {
        "memory": VM_MEMORY,
        "cores": VM_CORES,
        "kvm": VM_KVM,
        "machine": VM_MACHINE,
        "serial": VM_SERIAL,
        "display": VM_DISPLAY,
    }
    overrides = load_config(VM_CONFIG_FILE)
    if "memory" in overrides:
        defaults["memory"] = int(overrides["memory"])
    if "cores" in overrides:
        defaults["cores"] = int(overrides["cores"])
    if "kvm" in overrides:
        defaults["kvm"] = _parse_bool(overrides["kvm"])
    if "machine" in overrides:
        defaults["machine"] = overrides["machine"]
    if "serial" in overrides:
        defaults["serial"] = overrides["serial"]
    if "display" in overrides:
        defaults["display"] = _parse_bool(overrides["display"])
    return defaults


def get_lxc_defaults() -> dict[str, object]:
    """Return LXC defaults, overridden by values from LXC_CONFIG_FILE."""
    defaults: dict[str, object] = {
        "tty": LXC_TTY,
        "mount_auto": LXC_MOUNT_AUTO,
        "mount_auto_nesting": LXC_MOUNT_AUTO_NESTING,
        "nesting": LXC_NESTING,
    }
    overrides = load_config(LXC_CONFIG_FILE)
    if "tty" in overrides:
        defaults["tty"] = int(overrides["tty"])
    if "mount_auto" in overrides:
        defaults["mount_auto"] = overrides["mount_auto"]
    if "mount_auto_nesting" in overrides:
        defaults["mount_auto_nesting"] = overrides["mount_auto_nesting"]
    if "nesting" in overrides:
        defaults["nesting"] = _parse_bool(overrides["nesting"])
    return defaults


_LXC_CONF_HEADER = """\
# Kento LXC defaults
# Uncomment and edit to override hardcoded defaults.
# Changes take effect on next container create.
"""

_VM_CONF_HEADER = """\
# Kento VM defaults
# Uncomment and edit to override hardcoded defaults.
# Changes take effect on next VM create.
"""


def ensure_config_files() -> None:
    """Create default config files if they don't already exist."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if not LXC_CONFIG_FILE.exists():
        lines = [_LXC_CONF_HEADER]
        lines.append(f"# tty = {LXC_TTY}")
        lines.append(f"# mount_auto = {LXC_MOUNT_AUTO}")
        lines.append(f"# mount_auto_nesting = {LXC_MOUNT_AUTO_NESTING}")
        lines.append(f"# nesting = {LXC_NESTING}")
        lines.append("")
        LXC_CONFIG_FILE.write_text("\n".join(lines))

    if not VM_CONFIG_FILE.exists():
        lines = [_VM_CONF_HEADER]
        lines.append(f"# memory = {VM_MEMORY}")
        lines.append(f"# cores = {VM_CORES}")
        lines.append(f"# kvm = {VM_KVM}")
        lines.append(f"# machine = {VM_MACHINE}")
        lines.append(f"# serial = {VM_SERIAL}")
        lines.append(f"# display = {VM_DISPLAY}")
        lines.append("")
        VM_CONFIG_FILE.write_text("\n".join(lines))
