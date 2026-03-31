"""Kento default configuration values."""

from pathlib import Path

# --- LXC defaults ---
LXC_TTY = 2
LXC_MOUNT_AUTO = "proc:mixed sys:mixed cgroup:mixed"
LXC_MOUNT_AUTO_NESTING = "proc:rw sys:rw cgroup:rw"
LXC_NESTING = True

# --- VM defaults ---
VM_MEMORY = 512          # MB
VM_CORES = 1
VM_KVM = True
VM_MACHINE = "q35"
VM_SERIAL = "ttyS0"
VM_DISPLAY = False       # -nographic

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
