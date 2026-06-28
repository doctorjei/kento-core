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

# net_type values that support port forwarding, by family. Usermode forwards
# via QEMU slirp hostfwd (VM modes); bridge forwards via iptables/nft DNAT on
# the host (LXC/PVE). host/none have no per-instance NIC to forward to.
_PORT_TYPES = ("usermode", "bridge")


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


# ---------------------------------------------------------------------------
# Network identity: resolve-current -> apply-delta -> re-emit
# ---------------------------------------------------------------------------

def _read_line(container_dir: Path, field: str) -> str | None:
    p = container_dir / f"kento-{field}"
    if p.is_file():
        v = p.read_text().strip()
        return v or None
    return None


def _parse_kento_net(container_dir: Path) -> dict:
    """Parse the kento-net metadata file (ip=/gateway=/dns=/searchdomain=)."""
    out = {"ip": None, "gateway": None, "dns": None, "searchdomain": None}
    p = container_dir / "kento-net"
    if not p.is_file():
        return out
    for line in p.read_text().strip().splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k in out:
            out[k] = v or None
    return out


def _parse_lxc_net_fallback(container_dir: Path) -> dict:
    """Derive net identity from a plain-LXC native ``config`` (pre-N1)."""
    out = {"type": "none", "bridge": None, "ip": None, "gateway": None}
    config = container_dir / "config"
    if not config.is_file():
        return out
    net_type = None
    for raw in config.read_text().splitlines():
        s = raw.strip()
        if "=" not in s or s.startswith("#"):
            continue
        k, v = (x.strip() for x in s.split("=", 1))
        if k == "lxc.net.0.type":
            net_type = v
        elif k == "lxc.net.0.link":
            out["bridge"] = v
        elif k == "lxc.net.0.ipv4.address":
            out["ip"] = v
        elif k == "lxc.net.0.ipv4.gateway":
            out["gateway"] = v
    if net_type == "veth" and out["bridge"]:
        out["type"] = "bridge"
    elif net_type == "none":
        out["type"] = "host"
    else:
        out["type"] = "none"
    return out


def _parse_pve_net_fallback(container_dir: Path, mode: str) -> dict:
    """Derive net identity from a PVE .conf (pre-N1)."""
    out = {"type": "none", "bridge": None, "ip": None, "gateway": None}
    try:
        if mode == "pve":
            conf_path, _ = _pve_lxc_conf_path(container_dir)
        else:
            conf_path, _ = _pve_vm_conf_path(container_dir)
    except Exception:
        return out
    if not conf_path.is_file():
        return out
    content = conf_path.read_text()
    if "lxc.net.0.type: none" in content:
        out["type"] = "host"
        return out
    from kento.pve import _parse_qm_conf_field
    net0 = _parse_qm_conf_field(content, "net0")
    if net0 is None:
        # pve-vm usermode has no net0; treat as usermode for pve-vm else none.
        out["type"] = "usermode" if mode == "pve-vm" else "none"
        return out
    parts = dict(
        p.split("=", 1) for p in net0.split(",") if "=" in p)
    if "bridge" in parts:
        out["type"] = "bridge"
        out["bridge"] = parts["bridge"]
    if parts.get("ip") and parts["ip"] != "dhcp":
        out["ip"] = parts["ip"]
    if parts.get("gw"):
        out["gateway"] = parts["gw"]
    return out


def _resolve_net_identity(container_dir: Path, mode: str) -> dict:
    """Return the CURRENT network identity of an instance.

    Keys: type, bridge, ip, gateway, dns, searchdomain, port, hostname.

    Prefers kento metadata (kento-net-type / kento-bridge / kento-net /
    kento-port / kento-hostname). For pre-N1 instances that predate
    kento-net-type, falls back to parsing the live config to derive
    type/bridge/ip/gateway. Defensive throughout — unknowns default sensibly.
    """
    net = _parse_kento_net(container_dir)
    ident = {
        "type": _read_line(container_dir, "net-type"),
        "bridge": _read_line(container_dir, "bridge"),
        "ip": net["ip"],
        "gateway": net["gateway"],
        "dns": net["dns"],
        "searchdomain": net["searchdomain"],
        "port": _read_line(container_dir, "port"),
        "hostname": (_read_line(container_dir, "hostname")
                     or _read_line(container_dir, "name")
                     or container_dir.name),
    }

    if ident["type"] is None:
        # Pre-N1: no recorded type. Parse the live config.
        if mode == "lxc":
            fb = _parse_lxc_net_fallback(container_dir)
        elif mode in ("pve", "pve-vm"):
            fb = _parse_pve_net_fallback(container_dir, mode)
        else:  # plain vm: no bridge possible; usermode is the create default.
            fb = {"type": "usermode", "bridge": None,
                  "ip": None, "gateway": None}
        ident["type"] = fb["type"]
        if ident["bridge"] is None:
            ident["bridge"] = fb["bridge"]
        if ident["ip"] is None:
            ident["ip"] = fb["ip"]
        if ident["gateway"] is None:
            ident["gateway"] = fb["gateway"]

    return ident


def _parse_network_arg(network: str | None) -> tuple[str | None, str | None]:
    """Parse a --network value into (net_type, bridge_name).

    Accepts the same surface as create's --network: "bridge",
    "bridge=<name>", "host", "usermode", "none". Returns (None, None) when
    no --network was given. Mode-vs-type validity is enforced later against
    the resolved identity (so the message reflects the actual instance).
    """
    if network is None:
        return None, None
    if network in ("host", "usermode", "none", "bridge"):
        return network, None
    if network.startswith("bridge="):
        name = network.split("=", 1)[1]
        if not name:
            raise ValidationError(
                "--network bridge=<name> requires a bridge name."
            )
        return "bridge", name
    raise ValidationError(
        f"unknown --network value {network!r}; expected one of "
        "bridge, bridge=<name>, host, usermode, none."
    )


def _net_state_dir(container_dir: Path) -> Path | None:
    p = container_dir / "kento-state"
    if p.is_file():
        return Path(p.read_text().strip())
    return None


def _validate_net_identity(new: dict, *, mode: str, ip_provided: bool,
                           gateway_provided: bool, port_action: str) -> None:
    """Validate a fully-resolved NEW identity against the mode matrix.

    Raises ModeError / ValidationError. Called BEFORE any mutation.
    """
    t = new["type"]
    vm_modes = ("vm", "pve-vm")
    bridge_ok = ("lxc", "pve", "pve-vm")  # NOT plain vm (no tap in start_vm)
    usermode_ok = ("vm", "pve-vm")
    host_ok = ("lxc", "pve")

    if t == "bridge" and mode not in bridge_ok:
        raise ModeError(
            "--network bridge is not supported for plain VM "
            "(no host bridge attach in the plain-VM start path; use pve-vm "
            "for bridged VM networking, or --network usermode)."
        )
    if t == "usermode" and mode not in usermode_ok:
        raise ModeError(
            "--network usermode is only supported for VM modes (vm, pve-vm); "
            f"{mode} uses bridge/host/none networking."
        )
    if t == "host" and mode not in host_ok:
        raise ModeError(
            "--network host is not supported for VM modes; a VM cannot share "
            "the host network namespace. Use usermode, bridge (pve-vm), or none."
        )
    # 'none' is valid in every mode.

    # ip/gateway require bridge networking.
    if ip_provided and new["ip"] is not None and t != "bridge":
        raise ValidationError(
            "--ip requires bridge networking (--network bridge=<name>); "
            f"the resulting network type is {t!r}."
        )
    if gateway_provided and new["ip"] is None:
        raise ValidationError(
            "--gateway requires a static --ip (a gateway has no meaning "
            "without a static address)."
        )

    # port: only meaningful where there's a forwardable NIC.
    if port_action == "replace" and t not in _PORT_TYPES:
        raise ModeError(
            f"--port is not supported for --network {t!r}; port forwarding "
            "requires usermode (VM) or bridge (LXC/PVE DNAT) networking."
        )


def set_cmd(name, *, memory=None, cores=None, mac=None,
            qemu_args=None, pve_args=None, lxc_args=None,
            network=None, ip=None, gateway=None, dns=None,
            hostname=None, port=None,
            namespace=None) -> int:
    """Mutate scalar settings on a stopped instance. Returns 0 on success, raises on error."""
    require_root()

    container_dir, mode = resolve_any(name, namespace)

    # No fields at all -> usage error.
    if (memory is None and cores is None and mac is None
            and qemu_args is None and pve_args is None and lxc_args is None
            and network is None and ip is None and gateway is None
            and dns is None and hostname is None and port is None):
        raise ValidationError(
            "nothing to set. Provide at least one of --memory, "
            "--cores, --mac, --qemu-arg, --pve-arg, --lxc-arg, --network, "
            "--ip, --gateway, --dns, --hostname, --port."
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

    # Clamp requested vCPUs to node capacity for VM modes (same rule as create:
    # qm/QEMU refuse more vCPUs than the host has, so an over-request would
    # leave an unstartable guest). Reuses the create-time helper. The memory
    # arg drives only the overcommit warning; pass the effective memory (the
    # new value if being set, else the instance's current value, else 0 so the
    # check is a no-op) so set never spuriously warns about memory.
    if cores is not None and mode in ("vm", "pve-vm"):
        from kento.create import _clamp_vm_capacity
        if memory is not None:
            eff_mem = memory
        else:
            cur_mem = _read_line(container_dir, "memory")
            eff_mem = int(cur_mem) if cur_mem else 0
        cores = _clamp_vm_capacity(cores, eff_mem, mode=mode)

    # --- Network delta: resolve-current -> apply-overrides -> validate ---
    net_provided = (network is not None or ip is not None or gateway is not None
                    or dns is not None or hostname is not None
                    or port is not None)
    net_delta = None
    if net_provided:
        # Parse --network "type[/bridge]" sentinel into (net_type, bridge).
        # The CLI passes a pre-parsed value "type" or "type=bridgename".
        req_type, req_bridge = _parse_network_arg(network)

        current = _resolve_net_identity(container_dir, mode)
        new = dict(current)
        if req_type is not None:
            new["type"] = req_type
            # A network type change resets the bridge unless one was supplied.
            if req_type == "bridge":
                if req_bridge is not None:
                    new["bridge"] = req_bridge
                # else keep the current bridge (may be None -> emitters guard)
            else:
                new["bridge"] = None
        elif req_bridge is not None:
            new["bridge"] = req_bridge

        if ip is not None:
            if ip == "dhcp":
                new["ip"] = None
                new["gateway"] = None  # gateway is meaningless without static ip
            else:
                new["ip"] = ip
        if gateway is not None:
            new["gateway"] = gateway or None
        if dns is not None:
            new["dns"] = dns or None
        if hostname is not None:
            new["hostname"] = hostname

        port_action = _classify_list(port)
        if port_action == "replace":
            # Validate + dedup the full set declaratively via the §5.7A boundary
            # parser (Block 02): accepts N specs, protocol-aware (tcp/udp), and
            # rejects a duplicate (protocol, host_addr, host_port) with a clear
            # error. Address forms raise ForwardAddressNotImplemented at the
            # boundary (1.0 never persists them).
            from kento._network import parse_forwards
            parse_forwards(_nonempty(port))

        _validate_net_identity(
            new, mode=mode,
            ip_provided=ip is not None,
            gateway_provided=gateway is not None,
            port_action=port_action,
        )
        net_delta = {"new": new, "port": port, "port_action": port_action,
                     "ip_provided": ip is not None, "dns_provided": dns is not None,
                     "hostname_provided": hostname is not None,
                     "ip_value": ip}

    # --- Apply per mode ---
    if mode == "vm":
        _apply_vm(container_dir, memory, cores, mac, qemu_args, net_delta)
    elif mode == "lxc":
        _apply_lxc(container_dir, memory, cores, lxc_args, net_delta)
    elif mode == "pve":
        _apply_pve_lxc(container_dir, memory, cores, pve_args, net_delta)
    elif mode == "pve-vm":
        _apply_pve_vm(container_dir, memory, cores, mac, qemu_args, pve_args,
                      net_delta)

    logger.info("Updated: %s", name)
    logger.info("  Changes take effect on next start.")
    return 0


def _write_meta(container_dir: Path, field: str, value) -> None:
    (container_dir / f"kento-{field}").write_text(str(value) + "\n")


def _persist_net_meta(container_dir: Path, new: dict) -> None:
    """Write kento-net-type / kento-bridge / kento-net from the NEW identity."""
    _write_meta(container_dir, "net-type", new["type"])
    bridge_file = container_dir / "kento-bridge"
    if new["type"] == "bridge" and new.get("bridge"):
        bridge_file.write_text(new["bridge"] + "\n")
    else:
        bridge_file.unlink(missing_ok=True)

    # kento-net holds the static-only lines (ip/gateway) plus dns/searchdomain.
    net_parts = []
    if new.get("ip"):
        net_parts.append(f"ip={new['ip']}")
    if new.get("gateway"):
        net_parts.append(f"gateway={new['gateway']}")
    if new.get("dns"):
        net_parts.append(f"dns={new['dns']}")
    if new.get("searchdomain"):
        net_parts.append(f"searchdomain={new['searchdomain']}")
    net_file = container_dir / "kento-net"
    if net_parts:
        net_file.write_text("\n".join(net_parts) + "\n")
    else:
        net_file.unlink(missing_ok=True)


def _emit_guest_net_dropins(container_dir: Path, new: dict, mode: str) -> None:
    """Re-emit the overlay guest drop-ins (05-static / 90-resolved / hostname).

    Mirrors create.py: a static ip writes 05-kento-static.network (and removes
    any stale 90-kento.conf); dns-only writes 90-kento.conf; neither -> remove
    both. hostname always writes /etc/hostname. Needs kento-state.
    """
    from kento.create import _inject_hostname, _inject_network_config

    state_dir = _net_state_dir(container_dir)
    if state_dir is None:
        # No overlay (rare); guest drop-ins are best-effort.
        return

    net_dir = state_dir / "upper" / "etc" / "systemd" / "network"
    static = net_dir / "05-kento-static.network"
    resolved_dir = state_dir / "upper" / "etc" / "systemd" / "resolved.conf.d"
    resolved = resolved_dir / "90-kento.conf"

    if new.get("ip"):
        _inject_network_config(state_dir, new["ip"], new.get("gateway"),
                               new.get("dns"), new.get("searchdomain"),
                               mode=mode)
        resolved.unlink(missing_ok=True)
    else:
        static.unlink(missing_ok=True)
        if new.get("dns") or new.get("searchdomain"):
            resolved_dir.mkdir(parents=True, exist_ok=True)
            lines = ["[Resolve]"]
            if new.get("dns"):
                lines.append(f"DNS={new['dns']}")
            if new.get("searchdomain"):
                lines.append(f"Domains={new['searchdomain']}")
            lines.append("")
            resolved.write_text("\n".join(lines))
        else:
            resolved.unlink(missing_ok=True)

    if new.get("hostname"):
        _inject_hostname(state_dir, new["hostname"])
        _write_meta(container_dir, "hostname", new["hostname"])


def _apply_port_meta(container_dir: Path, port, port_action: str) -> None:
    """Replace/clear kento-port from a --port list arg.

    ``set --port`` is a DECLARATIVE full-set replace (§5.7B): the given specs
    ARE the desired set, written one §5.7A line each. ``--port ''`` (clear)
    unlinks the file. Specs are re-rendered through the Block-02 parser so the
    on-disk form is canonical and deduped (validation already ran in set_cmd()).
    """
    path = container_dir / "kento-port"
    if port_action == "clear":
        path.unlink(missing_ok=True)
    elif port_action == "replace":
        from kento._network import parse_forwards, render_forward_spec
        forwards = parse_forwards(_nonempty(port))
        lines = [render_forward_spec(b, t) for b, t in forwards.items()]
        path.write_text("".join(line + "\n" for line in lines))


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

def _apply_vm(container_dir, memory, cores, mac, qemu_args,
              net_delta=None) -> None:
    if memory is not None:
        _write_meta(container_dir, "memory", memory)
    if cores is not None:
        _write_meta(container_dir, "cores", cores)
    if mac is not None:
        _write_meta(container_dir, "mac", mac)
    _apply_passthrough_meta(container_dir, "qemu-args", qemu_args)

    if net_delta is not None:
        new = net_delta["new"]
        # Plain VM: only usermode/none reach here (validated). NIC + port are
        # read from kento-port at start (vm.py); ip/gateway already rejected.
        _apply_port_meta(container_dir, net_delta["port"],
                         net_delta["port_action"])
        # The slirp NIC is usermode-only (vm.py emits it purely on kento-port).
        # Switching to none must drop kento-port, else the VM keeps a slirp NIC.
        if new["type"] != "usermode":
            (container_dir / "kento-port").unlink(missing_ok=True)
        _emit_guest_net_dropins(container_dir, new, "vm")
        _persist_net_meta(container_dir, new)


# ---------------------------------------------------------------------------
# Plain LXC: metadata + patch native `config` cgroup lines.
# ---------------------------------------------------------------------------

def _rewrite_lxc_net(content: str, new: dict) -> str:
    """Rewrite the lxc.net.0.* block to match the NEW identity.

    bridge -> type=veth + link + flags=up (+ ipv4.address/gateway if static);
    host   -> type=none, no link/flags/ipv4;
    none   -> remove all lxc.net.0.* lines.
    """
    keys = ("lxc.net.0.type", "lxc.net.0.link", "lxc.net.0.flags",
            "lxc.net.0.ipv4.address", "lxc.net.0.ipv4.gateway")
    # Start from a clean slate: drop every existing net.0 line, then re-add.
    for k in keys:
        content = _replace_config_raw(content, k, None)

    t = new["type"]
    if t == "bridge" and new.get("bridge"):
        content = _replace_config_raw(content, "lxc.net.0.type", "veth")
        content = _replace_config_raw(content, "lxc.net.0.link", new["bridge"])
        content = _replace_config_raw(content, "lxc.net.0.flags", "up")
        if new.get("ip"):
            content = _replace_config_raw(
                content, "lxc.net.0.ipv4.address", new["ip"])
            if new.get("gateway"):
                content = _replace_config_raw(
                    content, "lxc.net.0.ipv4.gateway", new["gateway"])
    elif t == "host":
        content = _replace_config_raw(content, "lxc.net.0.type", "none")
    # t == "none": leave all removed.
    return content


def _apply_lxc(container_dir, memory, cores, lxc_args=None,
               net_delta=None) -> None:
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

    if net_delta is not None:
        new = net_delta["new"]
        content = _rewrite_lxc_net(content, new)
        changed = True
        _apply_port_meta(container_dir, net_delta["port"],
                         net_delta["port_action"])
        _emit_guest_net_dropins(container_dir, new, "lxc")
        _persist_net_meta(container_dir, new)

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


def _rewrite_pve_lxc_net(content: str, new: dict) -> str:
    """Rewrite net0/hostname/nameserver/searchdomain in a PVE-LXC .conf.

    Mirrors generate_pve_config: bridge -> net0: name=eth0,bridge=...,
    (ip=/gw= when static),type=veth; host -> lxc.net.0.type: none; none ->
    neither.
    """
    # Clear both possible network forms first.
    content = _replace_conf_field(content, "net0", None)
    content = _replace_conf_field(content, "lxc.net.0.type", None)

    t = new["type"]
    if t == "bridge" and new.get("bridge"):
        ip_part = f",ip={new['ip']}" if new.get("ip") else ""
        gw_part = (f",gw={new['gateway']}"
                   if new.get("ip") and new.get("gateway") else "")
        content = _replace_conf_field(
            content, "net0",
            f"name=eth0,bridge={new['bridge']}{ip_part}{gw_part},type=veth")
    elif t == "host":
        content = _replace_conf_field(content, "lxc.net.0.type", "none")

    # nameserver / searchdomain / hostname.
    content = _replace_conf_field(
        content, "nameserver", new.get("dns"))
    content = _replace_conf_field(
        content, "searchdomain", new.get("searchdomain"))
    if new.get("hostname"):
        content = _replace_conf_field(content, "hostname", new["hostname"])
    return content


def _apply_pve_lxc(container_dir, memory, cores, pve_args,
                   net_delta=None) -> None:
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

    if net_delta is not None:
        new = net_delta["new"]
        content = _rewrite_pve_lxc_net(content, new)
        _apply_port_meta(container_dir, net_delta["port"],
                         net_delta["port_action"])
        _emit_guest_net_dropins(container_dir, new, "pve")
        _persist_net_meta(container_dir, new)

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
                  pve_args, net_delta=None) -> None:
    from kento.pve import (write_qm_config, sync_qm_args_to_memory,
                           generate_qm_args, _parse_qm_conf_field,
                           _read_passthrough_lines)

    conf_path, vmid = _pve_vm_conf_path(container_dir)
    content = conf_path.read_text() if conf_path.is_file() else ""

    # Net delta (computed up front so the args: regen below picks up an updated
    # kento-port). bridge -> rewrite net0 (preserve mac); usermode/none ->
    # remove net0 and let generate_qm_args emit the slirp NIC from kento-port.
    net_args_regen = False
    if net_delta is not None:
        new = net_delta["new"]
        # Port metadata first — generate_qm_args reads kento-port for hostfwd.
        _apply_port_meta(container_dir, net_delta["port"],
                         net_delta["port_action"])
        if net_delta["port_action"] != "skip":
            net_args_regen = True
        if new["type"] == "bridge" and new.get("bridge"):
            mac_val = (container_dir / "kento-mac").read_text().strip() \
                if (container_dir / "kento-mac").is_file() else None
            if mac is not None:
                mac_val = mac
            net0_val = (f"virtio={mac_val},bridge={new['bridge']}"
                        if mac_val else f"virtio,bridge={new['bridge']}")
            content = _replace_conf_field(content, "net0", net0_val)
        else:
            # usermode / none: no qm net0; slirp NIC (if any) rides args:.
            content = _replace_conf_field(content, "net0", None)
        # The slirp NIC is usermode-only for VM modes: generate_qm_args emits it
        # purely on kento-port's presence (mirroring create, which writes
        # kento-port only for usermode). A switch AWAY from usermode must drop
        # kento-port, else the regenerated args: would keep a stale slirp NIC —
        # alongside a bridge net0 (two NICs fighting over id=net0 -> broken
        # boot) or on a none-network instance.
        if new["type"] != "usermode":
            (container_dir / "kento-port").unlink(missing_ok=True)
        # Any network type/bridge change requires args: reconsideration (the
        # slirp NIC appears/disappears with the resolved type).
        net_args_regen = True
        # Guest-side: static ip (bridge), dns, hostname via overlay drop-ins.
        _emit_guest_net_dropins(container_dir, new, "pve-vm")
        _persist_net_meta(container_dir, new)

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
        # kento-memory/kento-cores from the conf (PVE wins). This already
        # regenerates the full args: line (incl. the slirp hostfwd from
        # kento-port), so a concurrent net/port change is reflected too.
        sync_qm_args_to_memory(vmid, container_dir)
        content = conf_path.read_text()
    elif qemu_action != "skip" or net_args_regen:
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
                   if mem_file.is_file() else 1024)
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
