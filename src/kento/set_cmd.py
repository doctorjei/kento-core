"""Change scalar settings on a STOPPED kento-managed instance.

`kento set` mutates a handful of scalar config fields (memory, cores, mac,
and the QEMU / PVE pass-through lists) in place and re-emits the affected
config. Changes take effect on the instance's NEXT start.

Design (see plans/set-suspend-resume.md "LOCKED design"): the kento metadata
files under ``container_dir`` are the source of truth. A full config regen is
NOT viable because net_type/bridge are not persisted (generate_pve_config /
generate_qm_config could not be faithfully re-called), so instead we write the
metadata file(s) and then surgically re-emit ONLY the kento-owned scalar lines
in the native ``config`` / PVE ``.conf`` / qm ``.conf``, preserving every
structural and network line.

Field validity (mode strings: "lxc", "pve" (pve-lxc!), "vm", "pve-vm"):
  memory / cores : all four modes.
  mac / qemu_args: VM modes only (vm, pve-vm).
  pve_args       : PVE modes only (pve, pve-vm).
  lxc_args       : plain LXC only ("lxc").

List semantics for qemu_args / pve_args (argparse action="append",
default=None):
  - None (flag absent)                 -> leave the stored file untouched.
  - >=1 non-empty entry                -> REPLACE the file with those entries.
  - provided but all entries empty ('')-> CLEAR (unlink the metadata file).
"""

import logging
import re
from pathlib import Path

from kento import is_running, require_root, resolve_any
from kento.defaults import (LXC_ARG_DENYLIST, PVE_ARG_DENYLIST,
                            QEMU_ARG_DENYLIST)
from kento.errors import ModeError, StateError, ValidationError

logger = logging.getLogger("kento")

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def _classify_list(raw: list[str] | None) -> str:
    """Return one of "skip", "clear", "replace" for a pass-through list arg."""
    if raw is None:
        return "skip"
    provided = [x for x in raw if x != ""]
    return "replace" if provided else "clear"


def _nonempty(raw: list[str]) -> list[str]:
    return [x for x in raw if x != ""]


def _replace_conf_field(content: str, field: str, value: str | None) -> str:
    """Replace the LAST ``<field>: ...`` line in the global section.

    The global section is everything before the first ``[section]`` header.
    If ``value`` is None the field line is removed; otherwise the existing
    line is replaced in place (or appended at the end of the global section
    if absent). Mirrors pve._parse_qm_conf_field's section handling and
    sync_qm_args_to_memory's rewrite loop.
    """
    lines = content.splitlines()

    # Find section boundary (first [header]) and the index of the last
    # matching field line within the global section.
    section_start = len(lines)
    last_idx = -1
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section_start = i
            break
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, sep, _ = stripped.partition(":")
        if sep and key.strip() == field:
            last_idx = i

    new_line = None if value is None else f"{field}: {value}"

    if last_idx >= 0:
        if new_line is None:
            del lines[last_idx]
        else:
            lines[last_idx] = new_line
    elif new_line is not None:
        # Append at the end of the global section.
        lines.insert(section_start, new_line)

    out = "\n".join(lines)
    if content.endswith("\n") and not out.endswith("\n"):
        out += "\n"
    return out


def _replace_config_raw(content: str, key: str, value: str | None) -> str:
    """Replace a native LXC ``config`` line of the form ``<key> = <value>``.

    These use ``key = value`` (spaces around ``=``), unlike PVE's ``key:``.
    Replaces the last occurrence, appends if absent, removes if value None.
    """
    lines = content.splitlines()
    last_idx = -1
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        k = stripped.split("=", 1)[0].strip()
        if k == key:
            last_idx = i

    new_line = None if value is None else f"{key} = {value}"
    if last_idx >= 0:
        if new_line is None:
            del lines[last_idx]
        else:
            lines[last_idx] = new_line
    elif new_line is not None:
        lines.append(new_line)

    out = "\n".join(lines)
    if content.endswith("\n") and not out.endswith("\n"):
        out += "\n"
    return out


def _drop_passthrough_block(content: str, known_lines: list[str]) -> str:
    """Remove kento-pve-args pass-through lines previously appended to a conf.

    The pass-through block is appended verbatim after kento's own lines.
    We remove any line in the global section that exactly matches one of the
    currently-stored pass-through lines. (Called BEFORE the metadata file is
    overwritten so ``known_lines`` reflects the old block.)
    """
    if not known_lines:
        return content
    known = set(known_lines)
    lines = content.splitlines()
    kept = []
    in_global = True
    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_global = False
        if in_global and raw in known:
            continue
        kept.append(raw)
    out = "\n".join(kept)
    if content.endswith("\n") and not out.endswith("\n"):
        out += "\n"
    return out


def set_cmd(name, *, memory=None, cores=None, mac=None,
            qemu_args=None, pve_args=None, lxc_args=None,
            namespace=None) -> int:
    """Mutate scalar settings on a stopped instance. Returns 0 on success, raises on error."""
    require_root()

    container_dir, mode = resolve_any(name, namespace)

    # No fields at all -> usage error.
    if (memory is None and cores is None and mac is None
            and qemu_args is None and pve_args is None and lxc_args is None):
        raise ValidationError(
            "nothing to set. Provide at least one of --memory, "
            "--cores, --mac, --qemu-arg, --pve-arg, --lxc-arg."
        )

    if is_running(container_dir, mode):
        raise StateError(
            f"instance is running. Stop it first: kento stop {name}"
        )

    # --- Field validity (error BEFORE mutating anything) ---
    vm_modes = ("vm", "pve-vm")
    pve_modes = ("pve", "pve-vm")

    if mac is not None and mode not in vm_modes:
        raise ModeError(
            "--mac is not supported for LXC/PVE-LXC instances (no virtio NIC)."
        )
    if qemu_args is not None and mode not in vm_modes:
        raise ModeError(
            "--qemu-arg is not supported for LXC/PVE-LXC instances; "
            "it applies to VM modes only."
        )
    if pve_args is not None and mode not in pve_modes:
        if mode == "lxc":
            raise ModeError(
                "--pve-arg is not supported for plain LXC; it appends "
                "lines to the PVE lxc config and only applies on PVE hosts. "
                "Plain LXC has no PVE config. For plain-LXC native config "
                "pass-through use --lxc-arg."
            )
        else:  # vm
            raise ModeError(
                "--pve-arg is not supported for plain VM; it appends "
                "lines to the PVE qm config and only applies on PVE hosts. "
                "Plain VM has no PVE config."
            )
    if lxc_args is not None and mode != "lxc":
        if mode == "pve":
            raise ModeError(
                "--lxc-arg is not supported on a PVE host. On PVE the "
                "LXC config is the PVE config; use --pve-arg, which carries "
                "raw lxc.* lines."
            )
        else:  # vm / pve-vm
            raise ModeError(
                "--lxc-arg is not applicable to VM modes (no native LXC config)."
            )

    # Denylist: reject --lxc-arg values that collide with kento's structural
    # plain-LXC config lines (mirrors create.py's _validate_lxc_args).
    if lxc_args is not None:
        for arg in lxc_args:
            for needle in LXC_ARG_DENYLIST:
                if needle in arg:
                    raise ValidationError(
                        f"kento manages {needle!r} directly — "
                        f"--lxc-arg {arg!r} would collide with kento's own "
                        "plain-LXC config. Drop the flag or file an issue "
                        "if you need it overridable."
                    )

    # Same denylist enforcement create.py applies for --qemu-arg / --pve-arg
    # (create/set parity): without these, `kento set` would re-emit a
    # denylisted token into the boot config, clobbering kento-owned keys or
    # duplicating -kernel/memfd etc. Empty-string entries are the CLEAR
    # sentinel and are skipped (mirrors the lxc loop, where '' never matches).
    if qemu_args is not None:
        for arg in qemu_args:
            if arg == "":
                continue
            for needle in QEMU_ARG_DENYLIST:
                if needle in arg:
                    raise ValidationError(
                        f"kento manages {needle!r} directly — "
                        f"--qemu-arg {arg!r} would collide with kento's own "
                        "QEMU argv. Drop the flag or file an issue if you "
                        "need it overridable."
                    )

    if pve_args is not None:
        for arg in pve_args:
            if arg == "":
                continue
            for needle in PVE_ARG_DENYLIST:
                if needle in arg:
                    raise ValidationError(
                        f"kento manages {needle!r} directly — "
                        f"--pve-arg {arg!r} would collide with kento's own "
                        "PVE config. Drop the flag or file an issue if you "
                        "need it overridable."
                    )

    if mac is not None and not _MAC_RE.match(mac):
        raise ValidationError(
            f"invalid MAC address {mac!r}; expected format XX:XX:XX:XX:XX:XX."
        )
    if memory is not None and memory <= 0:
        raise ValidationError(
            f"--memory must be a positive integer (MB), got {memory}."
        )
    if cores is not None and cores <= 0:
        raise ValidationError(
            f"--cores must be a positive integer, got {cores}."
        )

    # --- Apply per mode ---
    if mode == "vm":
        _apply_vm(container_dir, memory, cores, mac, qemu_args)
    elif mode == "lxc":
        _apply_lxc(container_dir, memory, cores, lxc_args)
    elif mode == "pve":
        _apply_pve_lxc(container_dir, memory, cores, pve_args)
    elif mode == "pve-vm":
        _apply_pve_vm(container_dir, memory, cores, mac, qemu_args, pve_args)

    logger.info("Updated: %s", name)
    logger.info("  Changes take effect on next start.")
    return 0


def _write_meta(container_dir: Path, field: str, value) -> None:
    (container_dir / f"kento-{field}").write_text(str(value) + "\n")


def _apply_passthrough_meta(container_dir: Path, field: str,
                            raw: list[str] | None) -> str:
    """Apply replace/clear/skip to a kento-<field> metadata file.

    Returns the action taken ("skip"/"clear"/"replace").
    """
    action = _classify_list(raw)
    path = container_dir / f"kento-{field}"
    if action == "clear":
        path.unlink(missing_ok=True)
    elif action == "replace":
        path.write_text("\n".join(_nonempty(raw)) + "\n")
    return action


# ---------------------------------------------------------------------------
# Plain VM: metadata only (vm.py reads memory/cores/mac/qemu_args at start).
# ---------------------------------------------------------------------------

def _apply_vm(container_dir, memory, cores, mac, qemu_args) -> None:
    if memory is not None:
        _write_meta(container_dir, "memory", memory)
    if cores is not None:
        _write_meta(container_dir, "cores", cores)
    if mac is not None:
        _write_meta(container_dir, "mac", mac)
    _apply_passthrough_meta(container_dir, "qemu-args", qemu_args)


# ---------------------------------------------------------------------------
# Plain LXC: metadata + patch native `config` cgroup lines.
# ---------------------------------------------------------------------------

def _apply_lxc(container_dir, memory, cores, lxc_args=None) -> None:
    from kento.pve import _read_passthrough_lines

    config_path = container_dir / "config"
    content = config_path.read_text() if config_path.is_file() else ""
    changed = False
    if memory is not None:
        _write_meta(container_dir, "memory", memory)
        content = _replace_config_raw(
            content, "lxc.cgroup2.memory.max", str(memory * 1048576))
        changed = True
    if cores is not None:
        _write_meta(container_dir, "cores", cores)
        content = _replace_config_raw(
            content, "lxc.cgroup2.cpu.max", f"{cores * 100000} 100000")
        changed = True

    # lxc_args: drop the OLD pass-through block (read before overwriting the
    # metadata file), update/clear kento-lxc-args, then re-append the new
    # block at the end of `config`. Mirrors _apply_pve_lxc's pve_args path.
    lxc_action = _classify_list(lxc_args)
    if lxc_action != "skip":
        old_block = _read_passthrough_lines(container_dir / "kento-lxc-args")
        content = _drop_passthrough_block(content, old_block)
        changed = True
        if lxc_action == "clear":
            (container_dir / "kento-lxc-args").unlink(missing_ok=True)
        else:  # replace
            (container_dir / "kento-lxc-args").write_text(
                "\n".join(_nonempty(lxc_args)) + "\n")
            new_block = _read_passthrough_lines(container_dir / "kento-lxc-args")
            body = content.rstrip("\n")
            content = body + "\n" + "\n".join(new_block) + "\n"

    if changed:
        config_path.write_text(content)


# ---------------------------------------------------------------------------
# PVE-LXC: metadata + surgical rewrite of the PVE .conf.
# ---------------------------------------------------------------------------

def _pve_lxc_conf_path(container_dir: Path):
    from kento.pve import PVE_DIR, _pve_node_name
    vmid_file = container_dir / "kento-vmid"
    vmid = (vmid_file.read_text().strip() if vmid_file.is_file()
            else container_dir.name)
    node = _pve_node_name()
    return PVE_DIR / "nodes" / node / "lxc" / f"{vmid}.conf", int(vmid)


def _apply_pve_lxc(container_dir, memory, cores, pve_args) -> None:
    from kento.pve import write_pve_config, _read_passthrough_lines

    conf_path, vmid = _pve_lxc_conf_path(container_dir)
    content = conf_path.read_text() if conf_path.is_file() else ""

    # Metadata writes first (these feed the hook ns-cgroup at runtime).
    if memory is not None:
        _write_meta(container_dir, "memory", memory)
    if cores is not None:
        _write_meta(container_dir, "cores", cores)

    # If pve_args is being changed, drop the OLD pass-through block before we
    # overwrite the metadata file, then re-append the new block at the end.
    pve_action = _classify_list(pve_args)
    if pve_action != "skip":
        old_block = _read_passthrough_lines(container_dir / "kento-pve-args")
        content = _drop_passthrough_block(content, old_block)

    # Re-emit kento-owned scalar lines, matching generate_pve_config:197-211.
    if memory is not None:
        content = _replace_conf_field(content, "memory", str(memory))
        content = _replace_conf_field(
            content, "lxc.cgroup2.memory.max", str(memory * 1048576))
    if cores is not None:
        content = _replace_conf_field(content, "cores", str(cores))
        content = _replace_conf_field(content, "cpulimit", str(cores))
        content = _replace_conf_field(
            content, "lxc.cgroup2.cpu.max", f"{cores * 100000} 100000")

    if pve_action == "clear":
        (container_dir / "kento-pve-args").unlink(missing_ok=True)
    elif pve_action == "replace":
        (container_dir / "kento-pve-args").write_text(
            "\n".join(_nonempty(pve_args)) + "\n")
        new_block = _read_passthrough_lines(container_dir / "kento-pve-args")
        body = content.rstrip("\n")
        content = body + "\n" + "\n".join(new_block) + "\n"

    write_pve_config(vmid, content)


# ---------------------------------------------------------------------------
# PVE-VM: metadata + surgical rewrite of the qm .conf.
# ---------------------------------------------------------------------------

def _pve_vm_conf_path(container_dir: Path):
    from kento.pve import PVE_DIR, _pve_node_name
    vmid = int((container_dir / "kento-vmid").read_text().strip())
    node = _pve_node_name()
    return PVE_DIR / "nodes" / node / "qemu-server" / f"{vmid}.conf", vmid


def _apply_pve_vm(container_dir, memory, cores, mac, qemu_args,
                  pve_args) -> None:
    from kento.pve import (write_qm_config, sync_qm_args_to_memory,
                           generate_qm_args, _parse_qm_conf_field,
                           _read_passthrough_lines)

    conf_path, vmid = _pve_vm_conf_path(container_dir)
    content = conf_path.read_text() if conf_path.is_file() else ""

    # memory: patch memory: line, write metadata, then resync args: memfd
    # size via the existing helper (which reads the conf back from disk).
    if memory is not None:
        _write_meta(container_dir, "memory", memory)
        content = _replace_conf_field(content, "memory", str(memory))

    if cores is not None:
        _write_meta(container_dir, "cores", cores)
        content = _replace_conf_field(content, "cores", str(cores))

    if mac is not None:
        _write_meta(container_dir, "mac", mac)
        net0 = _parse_qm_conf_field(content, "net0")
        if net0 is not None:
            # Existing format: virtio=<MAC>,bridge=<name>. Swap only the MAC,
            # preserving the bridge (and any other) parts.
            parts = net0.split(",")
            new_parts = []
            matched = False
            for part in parts:
                if part.startswith("virtio="):
                    new_parts.append(f"virtio={mac}")
                    matched = True
                elif part == "virtio":
                    new_parts.append(f"virtio={mac}")
                    matched = True
                else:
                    new_parts.append(part)
            content = _replace_conf_field(content, "net0", ",".join(new_parts))
            if not matched:
                # net0 exists but has no virtio token (e.g. a user-edited
                # non-virtio model). The MAC couldn't be applied to the conf;
                # warn so "Updated" isn't misleading (metadata still written).
                logger.warning(
                    "net0 has no 'virtio' token; the MAC %r "
                    "could not be applied to the existing net0 form. "
                    "kento-mac metadata was updated but the qm config NIC "
                    "was left unchanged.", mac
                )
        # If net0 absent, best-effort: metadata written, skip conf edit.

    # pve_args: drop old block, re-emit new at end (before persisting we read
    # the old block from the still-current metadata file).
    pve_action = _classify_list(pve_args)
    if pve_action != "skip":
        old_block = _read_passthrough_lines(container_dir / "kento-pve-args")
        content = _drop_passthrough_block(content, old_block)

    # qemu_args: write/clear metadata, then regenerate the args: line. Use the
    # memory currently in the conf (post-patch) so memfd size stays correct.
    qemu_action = _classify_list(qemu_args)
    if qemu_action == "clear":
        (container_dir / "kento-qemu-args").unlink(missing_ok=True)
    elif qemu_action == "replace":
        (container_dir / "kento-qemu-args").write_text(
            "\n".join(_nonempty(qemu_args)) + "\n")

    # Write the conf now so sync/generate helpers (which read from disk) see
    # the patched memory:/cores:/net0:.
    write_qm_config(vmid, content)

    if memory is not None:
        # Rewrite args: memfd size= to match the new memory: + re-sync
        # kento-memory/kento-cores from the conf (PVE wins).
        sync_qm_args_to_memory(vmid, container_dir)
        content = conf_path.read_text()
    elif qemu_action != "skip":
        # Regenerate the args: line so the pass-through block reflects the new
        # kento-qemu-args. Use current memory from the conf (or kento-memory).
        mem_raw = _parse_qm_conf_field(content, "memory")
        try:
            mem = int(mem_raw) if mem_raw is not None else None
        except ValueError:
            mem = None
        if mem is None:
            mem_file = container_dir / "kento-memory"
            mem = (int(mem_file.read_text().strip())
                   if mem_file.is_file() else 512)
        new_args = f"args: {generate_qm_args(container_dir, memory=mem)}"
        content = _replace_conf_field(content, "args", None)
        # _replace_conf_field with None removed the old args; append fresh.
        body = content.rstrip("\n")
        # Re-insert args in the global section (append; qm tolerates ordering).
        content = body + "\n" + new_args + "\n"
        write_qm_config(vmid, content)
        content = conf_path.read_text()

    # Re-emit the pve_args pass-through block at the end of the conf.
    if pve_action == "clear":
        (container_dir / "kento-pve-args").unlink(missing_ok=True)
        write_qm_config(vmid, content)
    elif pve_action == "replace":
        (container_dir / "kento-pve-args").write_text(
            "\n".join(_nonempty(pve_args)) + "\n")
        new_block = _read_passthrough_lines(container_dir / "kento-pve-args")
        body = content.rstrip("\n")
        content = body + "\n" + "\n".join(new_block) + "\n"
        write_qm_config(vmid, content)
