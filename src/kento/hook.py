"""Generate per-container LXC hook scripts."""

from pathlib import Path

_TEMPLATE = (Path(__file__).parent / "hook.sh").read_text()


def generate_hook(container_dir: Path, layers: str, name: str,
                  state_dir: Path | None = None) -> str:
    """Return a hook script with baked-in paths for a container.

    state_dir is where upper/work live. Defaults to container_dir if not given.
    """
    sd = state_dir or container_dir
    return (_TEMPLATE
            .replace("@@NAME@@", str(name))
            .replace("@@CONTAINER_DIR@@", str(container_dir))
            .replace("@@STATE_DIR@@", str(sd))
            .replace("@@LAYERS@@", str(layers)))


def write_hook(container_dir: Path, layers: str, name: str,
               state_dir: Path | None = None) -> Path:
    """Generate and write the hook script into the container directory."""
    hook_path = container_dir / "kento-hook"
    hook_path.write_text(generate_hook(container_dir, layers, name, state_dir))
    hook_path.chmod(0o755)
    return hook_path
