"""Install the shared guest-config injection script per container.

``inject.sh`` is a standalone POSIX shell script (no templating) that reads
kento metadata + LXC/PVE config and writes guest-side config into a mounted
rootfs. It is invoked by the LXC hook (and, in subsequent steps, by VM and
PVE-VM code paths).

Copying per container — rather than referencing the package path — keeps
each container self-contained: it survives kento upgrades and uninstalls
the same way ``kento-hook`` does.
"""

from pathlib import Path

_SCRIPT = (Path(__file__).parent / "inject.sh").read_text()


def generate_inject() -> str:
    """Return the inject.sh content verbatim (no substitutions)."""
    return _SCRIPT


def write_inject(container_dir: Path) -> Path:
    """Write the inject script into the container directory."""
    inject_path = container_dir / "kento-inject.sh"
    inject_path.write_text(generate_inject())
    inject_path.chmod(0o755)
    return inject_path
