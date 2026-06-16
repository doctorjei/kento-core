"""Generate per-container LXC hook scripts."""

from pathlib import Path

_TEMPLATE = (Path(__file__).parent / "hook.sh").read_text()


def generate_hook(container_dir: Path, layers: str, name: str,
                  state_dir: Path | None = None) -> str:
    """Return a hook script with baked-in paths for a container.

    state_dir is where upper/work live. Defaults to container_dir if not given.

    LAYERS is baked in chdir-relative ``l/<short>`` form (Docker/podman
    parity) so a deeply layered image's overlay mount(2) options stay under
    the kernel's 4096-byte page limit. OVERLAY_BASE is the podman overlay
    store root; the pre-start mount cd's into it inside a subshell so the
    chdir never leaks to inject.sh / later steps. If the short form can't be
    derived, to_overlay_lowerdir falls back to absolute layers and
    OVERLAY_BASE is "/" (a harmless no-op cd).
    """
    from kento.layers import to_overlay_lowerdir
    sd = state_dir or container_dir
    overlay_base, rel_layers = to_overlay_lowerdir(str(layers))
    if not overlay_base:
        overlay_base = "/"
    return (_TEMPLATE
            .replace("@@NAME@@", str(name))
            .replace("@@CONTAINER_DIR@@", str(container_dir))
            .replace("@@STATE_DIR@@", str(sd))
            .replace("@@OVERLAY_BASE@@", overlay_base)
            .replace("@@LAYERS@@", rel_layers))


def write_hook(container_dir: Path, layers: str, name: str,
               state_dir: Path | None = None) -> Path:
    """Generate and write the hook script into the container directory."""
    hook_path = container_dir / "kento-hook"
    hook_path.write_text(generate_hook(container_dir, layers, name, state_dir))
    hook_path.chmod(0o755)
    return hook_path
