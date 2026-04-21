"""Cloud-init NoCloud seed generation for kento containers."""

import hashlib
import json
from pathlib import Path


def detect_cloudinit(layers: str) -> bool:
    """Check if any layer in the colon-separated layer paths has cloud-init."""
    markers = ["usr/bin/cloud-init", "etc/cloud/cloud.cfg"]
    for layer_path in layers.split(":"):
        layer = Path(layer_path)
        for marker in markers:
            if (layer / marker).exists():
                return True
    return False


def generate_meta_data(name: str, instance_id: str) -> str:
    """Generate NoCloud meta-data content."""
    return f"instance-id: {instance_id}\nlocal-hostname: {name}\n"


def generate_user_data(*, timezone: str | None = None,
                       ssh_keys: str | None = None,
                       ssh_key_user: str = "root",
                       ssh_host_key_dir: Path | None = None,
                       env: list[str] | None = None) -> str:
    """Generate NoCloud user-data content.

    ssh_keys is the concatenated authorized_keys content.
    ssh_host_key_dir is the path to the container's ssh-host-keys/ dir.
    """
    lines = ["#cloud-config"]

    if timezone:
        lines.append(f"timezone: {timezone}")

    # SSH authorized keys
    if ssh_keys:
        keys = [k.strip() for k in ssh_keys.strip().splitlines()
                if k.strip() and not k.strip().startswith("#")]
        if keys:
            if ssh_key_user == "root":
                lines.append("ssh_authorized_keys:")
                for key in keys:
                    lines.append(f"  - {key}")
            else:
                lines.append("users:")
                lines.append(f"  - name: {ssh_key_user}")
                lines.append("    ssh_authorized_keys:")
                for key in keys:
                    lines.append(f"      - {key}")

    # SSH host keys
    if ssh_host_key_dir and ssh_host_key_dir.is_dir():
        host_key_lines = _generate_ssh_keys_section(ssh_host_key_dir)
        if host_key_lines:
            lines.append("ssh_keys:")
            lines.extend(host_key_lines)

    # Environment variables via write_files
    if env:
        lines.append("write_files:")
        lines.append("  - path: /etc/environment")
        lines.append("    content: |")
        for e in env:
            lines.append(f"      {e}")

    lines.append("")
    return "\n".join(lines)


def _generate_ssh_keys_section(key_dir: Path) -> list[str]:
    """Generate the ssh_keys cloud-config section from host key files."""
    lines = []
    for key_type in ("rsa", "ecdsa", "ed25519"):
        priv_file = key_dir / f"ssh_host_{key_type}_key"
        pub_file = key_dir / f"ssh_host_{key_type}_key.pub"
        if priv_file.is_file():
            priv_content = priv_file.read_text()
            lines.append(f"  {key_type}_private: |")
            for line in priv_content.splitlines():
                lines.append(f"    {line}")
        if pub_file.is_file():
            pub_content = pub_file.read_text().strip()
            lines.append(f"  {key_type}_public: {pub_content}")
    return lines


def generate_network_config(*, ip: str | None = None,
                            gateway: str | None = None,
                            dns: str | None = None,
                            searchdomain: str | None = None) -> str | None:
    """Generate NoCloud network-config (v2 format). Returns None if no network config needed."""
    if not ip and not dns and not searchdomain:
        return None

    lines = ["network:", "  version: 2", "  ethernets:", "    all:",
             "      match:", '        name: "*"']

    if ip:
        lines.append("      addresses:")
        lines.append(f"        - {ip}")
        if gateway:
            lines.append("      routes:")
            lines.append("        - to: default")
            lines.append(f"          via: {gateway}")
    else:
        lines.append("      dhcp4: true")

    if dns or searchdomain:
        lines.append("      nameservers:")
        if dns:
            lines.append("        addresses:")
            lines.append(f"          - {dns}")
        if searchdomain:
            lines.append("        search:")
            lines.append(f"          - {searchdomain}")

    lines.append("")
    return "\n".join(lines)


def compute_instance_id(name: str, *, ip: str | None = None,
                        gateway: str | None = None,
                        dns: str | None = None,
                        searchdomain: str | None = None,
                        timezone: str | None = None,
                        env: list[str] | None = None,
                        ssh_key_user: str = "root",
                        has_ssh_keys: bool = False,
                        has_ssh_host_keys: bool = False) -> str:
    """Compute a content-hash instance-id from configuration.

    When config changes, the hash changes, causing cloud-init to re-run.
    """
    config = {
        "name": name,
        "ip": ip,
        "gateway": gateway,
        "dns": dns,
        "searchdomain": searchdomain,
        "timezone": timezone,
        "env": sorted(env) if env else None,
        "ssh_key_user": ssh_key_user,
        "has_ssh_keys": has_ssh_keys,
        "has_ssh_host_keys": has_ssh_host_keys,
    }
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
    h = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return f"kento-{h}"


def write_seed(container_dir: Path, *, name: str,
               ip: str | None = None, gateway: str | None = None,
               dns: str | None = None, searchdomain: str | None = None,
               timezone: str | None = None, env: list[str] | None = None,
               ssh_keys: str | None = None, ssh_key_user: str = "root",
               ssh_host_key_dir: Path | None = None) -> None:
    """Generate and write NoCloud seed files to container_dir/cloud-seed/."""
    seed_dir = container_dir / "cloud-seed"
    seed_dir.mkdir(parents=True, exist_ok=True)

    # Compute instance-id
    iid = compute_instance_id(
        name, ip=ip, gateway=gateway, dns=dns, searchdomain=searchdomain,
        timezone=timezone, env=env, ssh_key_user=ssh_key_user,
        has_ssh_keys=bool(ssh_keys),
        has_ssh_host_keys=bool(ssh_host_key_dir and ssh_host_key_dir.is_dir()),
    )

    # meta-data
    (seed_dir / "meta-data").write_text(generate_meta_data(name, iid))

    # user-data
    user_data = generate_user_data(
        timezone=timezone, ssh_keys=ssh_keys, ssh_key_user=ssh_key_user,
        ssh_host_key_dir=ssh_host_key_dir, env=env,
    )
    (seed_dir / "user-data").write_text(user_data)

    # network-config (optional)
    net_config = generate_network_config(ip=ip, gateway=gateway, dns=dns,
                                         searchdomain=searchdomain)
    if net_config:
        (seed_dir / "network-config").write_text(net_config)
    else:
        # Remove stale network-config if it existed from a prior scrub
        (seed_dir / "network-config").unlink(missing_ok=True)
