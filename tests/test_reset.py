"""Tests for container reset."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from kento.reset import reset


def _mock_run_stopped(args, **kwargs):
    result = subprocess.CompletedProcess(args, 0)
    if "lxc-info" in args:
        result.stdout = "STOPPED"
    elif "mountpoint" in args:
        result.returncode = 1
    return result


def _mock_run_running(args, **kwargs):
    result = subprocess.CompletedProcess(args, 0)
    if "lxc-info" in args:
        result.stdout = "RUNNING"
    return result


@patch("kento.reset.subprocess.run", side_effect=_mock_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_clears_upper_and_work(mock_root, mock_layers, mock_run,
                                      tmp_path):
    lxc_dir = tmp_path / "test"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    upper = lxc_dir / "upper"
    upper.mkdir()
    (upper / "somefile").write_text("data")
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir):
        reset("test")

    assert upper.is_dir()
    assert not (upper / "somefile").exists()
    assert (lxc_dir / "kento-layers").read_text().strip() == "/new/upper:/new/lower"
    assert (lxc_dir / "kento-hook").exists()


@patch("kento.reset.subprocess.run", side_effect=_mock_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_with_separate_state_dir(mock_root, mock_layers, mock_run,
                                        tmp_path):
    lxc_dir = tmp_path / "test"
    lxc_dir.mkdir()
    state = tmp_path / "user-state" / "test"
    state.mkdir(parents=True)
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-state").write_text(str(state) + "\n")
    (lxc_dir / "rootfs").mkdir()
    upper = state / "upper"
    upper.mkdir()
    (upper / "somefile").write_text("data")
    (state / "work").mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir):
        reset("test")

    assert upper.is_dir()
    assert not (upper / "somefile").exists()
    hook = (lxc_dir / "kento-hook").read_text()
    assert str(state) in hook


@patch("kento.reset.subprocess.run", side_effect=_mock_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_preserves_kento_qemu_args(mock_root, mock_layers, mock_run,
                                          tmp_path):
    """B2: scrub must leave kento-qemu-args intact so pass-through survives."""
    lxc_dir = tmp_path / "test"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()
    original = "-device=virtio-rng-pci\n-cpu=max\n"
    (lxc_dir / "kento-qemu-args").write_text(original)

    with patch("kento.reset.resolve_container", return_value=lxc_dir):
        reset("test")

    assert (lxc_dir / "kento-qemu-args").read_text() == original


@patch("kento.reset.subprocess.run", side_effect=_mock_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_preserves_kento_pve_args(mock_root, mock_layers, mock_run,
                                         tmp_path):
    """B3: scrub must leave kento-pve-args intact so pass-through survives."""
    lxc_dir = tmp_path / "test"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()
    original = "tags: kento-test\nonboot: 1\n"
    (lxc_dir / "kento-pve-args").write_text(original)

    with patch("kento.reset.resolve_container", return_value=lxc_dir):
        reset("test")

    assert (lxc_dir / "kento-pve-args").read_text() == original


@patch("kento.reset.subprocess.run", side_effect=_mock_run_running)
@patch("kento.reset.require_root")
def test_reset_refuses_running(mock_root, mock_run, tmp_path):
    lxc_dir = tmp_path / "test"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")

    with patch("kento.reset.resolve_container", return_value=lxc_dir):
        with pytest.raises(SystemExit):
            reset("test")


@patch("kento.reset.require_root")
def test_reset_nonexistent(mock_root, tmp_path):
    with patch("kento.reset.resolve_container",
               side_effect=SystemExit(1)):
        with pytest.raises(SystemExit):
            reset("nonexistent")


# --- PVE mode tests ---


def _mock_pve_run_stopped(args, **kwargs):
    result = subprocess.CompletedProcess(args, 0)
    if "pct" in args and "status" in args:
        result.stdout = "status: stopped"
    elif "mountpoint" in args:
        result.returncode = 1
    return result


def _mock_pve_run_running(args, **kwargs):
    result = subprocess.CompletedProcess(args, 0)
    if "pct" in args and "status" in args:
        result.stdout = "status: running"
    return result


@patch("kento.reset.subprocess.run", side_effect=_mock_pve_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_pve_clears_upper_and_work(mock_root, mock_layers, mock_run,
                                          tmp_path):
    lxc_dir = tmp_path / "100"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-mode").write_text("pve\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    upper = lxc_dir / "upper"
    upper.mkdir()
    (upper / "somefile").write_text("data")
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir):
        reset("mybox")

    assert upper.is_dir()
    assert not (upper / "somefile").exists()
    assert (lxc_dir / "kento-layers").read_text().strip() == "/new/upper:/new/lower"


@patch("kento.reset.subprocess.run", side_effect=_mock_pve_run_running)
@patch("kento.reset.require_root")
def test_reset_pve_refuses_running(mock_root, mock_run, tmp_path):
    lxc_dir = tmp_path / "100"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-mode").write_text("pve\n")

    with patch("kento.reset.resolve_container", return_value=lxc_dir):
        with pytest.raises(SystemExit):
            reset("mybox")


def _make_pve_container(tmp_path, vmid="100"):
    lxc_dir = tmp_path / vmid
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-mode").write_text("pve\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()
    return lxc_dir


@patch("kento.reset.subprocess.run", side_effect=_mock_pve_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_pve_with_port_regenerates_snippets_wrapper(
        mock_root, mock_layers, mock_run, tmp_path):
    lxc_dir = _make_pve_container(tmp_path, vmid="100")
    (lxc_dir / "kento-port").write_text("10022:22\n")
    snippets = tmp_path / "snippets"
    snippets.mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir), \
         patch("kento.vm_hook.find_snippets_dir",
               return_value=(snippets, "local")):
        reset("mybox")

    wrapper = snippets / "kento-lxc-100.sh"
    assert wrapper.is_file()
    content = wrapper.read_text()
    assert str(lxc_dir / "kento-hook") in content


@patch("kento.reset.subprocess.run", side_effect=_mock_pve_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_pve_with_memory_regenerates_snippets_wrapper(
        mock_root, mock_layers, mock_run, tmp_path):
    lxc_dir = _make_pve_container(tmp_path, vmid="101")
    (lxc_dir / "kento-memory").write_text("512\n")
    snippets = tmp_path / "snippets"
    snippets.mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir), \
         patch("kento.vm_hook.find_snippets_dir",
               return_value=(snippets, "local")):
        reset("mybox")

    wrapper = snippets / "kento-lxc-101.sh"
    assert wrapper.is_file()


@patch("kento.reset.subprocess.run", side_effect=_mock_pve_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_pve_with_cores_regenerates_snippets_wrapper(
        mock_root, mock_layers, mock_run, tmp_path):
    lxc_dir = _make_pve_container(tmp_path, vmid="102")
    (lxc_dir / "kento-cores").write_text("2\n")
    snippets = tmp_path / "snippets"
    snippets.mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir), \
         patch("kento.vm_hook.find_snippets_dir",
               return_value=(snippets, "local")):
        reset("mybox")

    wrapper = snippets / "kento-lxc-102.sh"
    assert wrapper.is_file()


@patch("kento.reset.subprocess.run", side_effect=_mock_pve_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_pve_without_resource_metadata_skips_snippets(
        mock_root, mock_layers, mock_run, tmp_path):
    """No port/memory/cores metadata: wrapper regeneration is skipped.

    find_snippets_dir must NOT be called in the no-flag path — if the
    storage lookup were attempted we'd fail hard even though there's
    nothing to write.
    """
    lxc_dir = _make_pve_container(tmp_path, vmid="103")

    with patch("kento.reset.resolve_container", return_value=lxc_dir), \
         patch("kento.vm_hook.find_snippets_dir",
               side_effect=AssertionError(
                   "find_snippets_dir should not be called")):
        reset("mybox")


# --- VM mode tests ---


def _mock_vm_run_stopped(args, **kwargs):
    result = subprocess.CompletedProcess(args, 0)
    if "mountpoint" in args:
        result.returncode = 1
    return result


@patch("kento.reset.subprocess.run", side_effect=_mock_vm_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_vm_clears_upper_and_work(mock_root, mock_layers, mock_run,
                                         tmp_path):
    lxc_dir = tmp_path / "testvm"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-mode").write_text("vm\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    upper = lxc_dir / "upper"
    upper.mkdir()
    (upper / "somefile").write_text("data")
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir), \
         patch("kento.vm.is_vm_running", return_value=False):
        reset("testvm")

    assert upper.is_dir()
    assert not (upper / "somefile").exists()
    assert (lxc_dir / "kento-layers").read_text().strip() == "/new/upper:/new/lower"


@patch("kento.reset.subprocess.run", side_effect=_mock_vm_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_vm_no_hook_regenerated(mock_root, mock_layers, mock_run,
                                       tmp_path):
    lxc_dir = tmp_path / "testvm"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-mode").write_text("vm\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir), \
         patch("kento.vm.is_vm_running", return_value=False):
        reset("testvm")

    assert not (lxc_dir / "kento-hook").exists()


@patch("kento.reset.subprocess.run", side_effect=_mock_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_pve_vm_regenerates_vm_hook(mock_root, mock_layers, mock_run,
                                           tmp_path):
    """pve-vm scrub must regenerate the VM hookscript, not the LXC hook.

    Before this fix scrub called `write_hook()` for any mode != "vm",
    which overwrote the VM hookscript with the LXC shell hook. `qm start`
    then failed in pre-start with `3: parameter not set` because the LXC
    hook expects a 3rd arg (hook-type) but qm only passes VMID and PHASE.
    """
    lxc_dir = tmp_path / "testpvevm"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-mode").write_text("pve-vm\n")
    (lxc_dir / "kento-vmid").write_text("100\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir), \
         patch("kento.reset.is_running", return_value=False):
        reset("testpvevm")

    hook = lxc_dir / "kento-hook"
    assert hook.exists()
    content = hook.read_text()
    # VM hook shape: uses $1/$2 positional args, has a "pre-start" case
    assert 'VMID="$1"' in content
    assert 'PHASE="$2"' in content
    # LXC hook shape uses $3 for hook type — must NOT be present
    assert 'LXC_HOOK_TYPE' not in content


@patch("kento.reset.subprocess.run", side_effect=_mock_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_pve_vm_rewrites_memfd_size_after_memory_change(
        mock_root, mock_layers, mock_run, tmp_path):
    """pve-vm scrub must re-read `memory:` from the qm config and
    rewrite the memfd `size=` inside `args:` to match.

    Repro: create with default 512M -> user runs
    `qm set --memory 2048` -> memory: updates but args:size= doesn't ->
    kento's pre-start validator refuses to boot. Scrub should fix it.
    """
    pve = tmp_path / "pve"
    conf_dir = pve / "nodes" / "mynode" / "qemu-server"
    conf_dir.mkdir(parents=True)
    qm_conf = conf_dir / "100.conf"
    lxc_dir = tmp_path / "testpvevm"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-mode").write_text("pve-vm\n")
    (lxc_dir / "kento-vmid").write_text("100\n")
    (lxc_dir / "kento-memory").write_text("512\n")
    (lxc_dir / "kento-cores").write_text("1\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()
    # Simulate user having run `qm set --memory 2048`:
    # memory: updated, args:size= still stale at 512M.
    qm_conf.write_text(
        "name: testpvevm\n"
        "ostype: l26\n"
        "memory: 2048\n"
        "cores: 1\n"
        f"args: -enable-kvm -kernel {lxc_dir}/rootfs/boot/vmlinuz "
        f"-object memory-backend-memfd,id=mem,size=512M,share=on -numa node,memdev=mem\n"
        "serial0: socket\n"
    )

    with patch("kento.reset.resolve_container", return_value=lxc_dir), \
         patch("kento.reset.is_running", return_value=False), \
         patch("kento.pve.PVE_DIR", pve), \
         patch("kento.pve._pve_node_name", return_value="mynode"):
        reset("testpvevm")

    new = qm_conf.read_text()
    assert "size=2048M" in new, new
    assert "size=512M" not in new
    assert "memory: 2048" in new
    # Kento metadata now reflects PVE's authoritative values.
    assert (lxc_dir / "kento-memory").read_text().strip() == "2048"
    assert (lxc_dir / "kento-cores").read_text().strip() == "1"


@patch("kento.reset.subprocess.run", side_effect=_mock_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_pve_vm_syncs_kento_metadata_from_qm_config(
        mock_root, mock_layers, mock_run, tmp_path):
    """If kento-memory and qm config disagree (user edited via qm set),
    PVE's value wins — kento metadata files get rewritten to match."""
    pve = tmp_path / "pve"
    conf_dir = pve / "nodes" / "mynode" / "qemu-server"
    conf_dir.mkdir(parents=True)
    qm_conf = conf_dir / "100.conf"
    lxc_dir = tmp_path / "pvevm2"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-mode").write_text("pve-vm\n")
    (lxc_dir / "kento-vmid").write_text("100\n")
    (lxc_dir / "kento-memory").write_text("512\n")
    (lxc_dir / "kento-cores").write_text("1\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()
    qm_conf.write_text(
        "memory: 4096\n"
        "cores: 8\n"
        f"args: -object memory-backend-memfd,id=mem,size=512M,share=on\n"
    )

    with patch("kento.reset.resolve_container", return_value=lxc_dir), \
         patch("kento.reset.is_running", return_value=False), \
         patch("kento.pve.PVE_DIR", pve), \
         patch("kento.pve._pve_node_name", return_value="mynode"):
        reset("pvevm2")

    assert (lxc_dir / "kento-memory").read_text().strip() == "4096"
    assert (lxc_dir / "kento-cores").read_text().strip() == "8"


@patch("kento.reset.subprocess.run", side_effect=_mock_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_pve_vm_missing_qm_config_does_not_mask_scrub(
        mock_root, mock_layers, mock_run, tmp_path):
    """If the qm config is missing (destroyed between steps), sync is a
    no-op — scrub should not crash and should still regenerate the hook."""
    pve = tmp_path / "pve"
    (pve / "nodes" / "mynode" / "qemu-server").mkdir(parents=True)
    # No qm config file written.
    lxc_dir = tmp_path / "pvevm3"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-mode").write_text("pve-vm\n")
    (lxc_dir / "kento-vmid").write_text("100\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir), \
         patch("kento.reset.is_running", return_value=False), \
         patch("kento.pve.PVE_DIR", pve), \
         patch("kento.pve._pve_node_name", return_value="mynode"):
        reset("pvevm3")

    assert (lxc_dir / "kento-hook").exists()


@patch("kento.reset.subprocess.run", side_effect=_mock_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_reinjects_static_ip(mock_root, mock_layers, mock_run,
                                    tmp_path):
    lxc_dir = tmp_path / "test"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "kento-net").write_text("ip=192.168.0.160/22\ngateway=192.168.0.1\ndns=8.8.8.8\n")
    upper = lxc_dir / "upper"
    upper.mkdir()
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir):
        reset("test")

    unit = (lxc_dir / "upper" / "etc" / "systemd" / "network" /
            "10-static.network").read_text()
    assert "Address=192.168.0.160/22" in unit
    assert "Gateway=192.168.0.1" in unit
    assert "DNS=8.8.8.8" in unit


@patch("kento.reset.subprocess.run", side_effect=_mock_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_reinjects_hostname(mock_root, mock_layers, mock_run, tmp_path):
    lxc_dir = tmp_path / "test"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "kento-name").write_text("myhost\n")
    (lxc_dir / "upper").mkdir()
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir):
        reset("myhost")

    hostname = (lxc_dir / "upper" / "etc" / "hostname").read_text()
    assert hostname.strip() == "myhost"


@patch("kento.reset.subprocess.run", side_effect=_mock_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_reinjects_timezone(mock_root, mock_layers, mock_run, tmp_path):
    lxc_dir = tmp_path / "test"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "kento-tz").write_text("Asia/Tokyo\n")
    (lxc_dir / "upper").mkdir()
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir):
        reset("test")

    assert (lxc_dir / "upper" / "etc" / "timezone").read_text().strip() == "Asia/Tokyo"
    localtime = lxc_dir / "upper" / "etc" / "localtime"
    assert localtime.is_symlink()


@patch("kento.reset.subprocess.run", side_effect=_mock_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_reinjects_env(mock_root, mock_layers, mock_run, tmp_path):
    lxc_dir = tmp_path / "test"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "kento-env").write_text("FOO=bar\nBAZ=qux\n")
    (lxc_dir / "upper").mkdir()
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir):
        reset("test")

    env = (lxc_dir / "upper" / "etc" / "environment").read_text()
    assert "FOO=bar" in env
    assert "BAZ=qux" in env


@patch("kento.reset.subprocess.run", side_effect=_mock_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_no_net_file_skips_injection(mock_root, mock_layers, mock_run,
                                            tmp_path):
    lxc_dir = tmp_path / "test"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir):
        reset("test")

    assert not (lxc_dir / "upper" / "etc").exists()


@patch("kento.reset.require_root")
def test_reset_vm_refuses_running(mock_root, tmp_path):
    lxc_dir = tmp_path / "testvm"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-mode").write_text("vm\n")

    with patch("kento.reset.resolve_container", return_value=lxc_dir), \
         patch("kento.vm.is_vm_running", return_value=True):
        with pytest.raises(SystemExit):
            reset("testvm")


# --- Port forwarding state cleanup (Phase 3) ---


@patch("kento.reset.subprocess.run", side_effect=_mock_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_removes_portfwd_active(mock_root, mock_layers, mock_run,
                                       tmp_path):
    """scrub removes stale kento-portfwd-active file."""
    lxc_dir = tmp_path / "test"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-layers").write_text("/old/path\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "kento-portfwd-active").write_text("10022:22:10.0.0.5\n")
    (lxc_dir / "upper").mkdir()
    (lxc_dir / "work").mkdir()
    (lxc_dir / "rootfs").mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir):
        reset("test")

    assert not (lxc_dir / "kento-portfwd-active").exists()


# --- F12: crash-safe upper/work clear ---


from kento.reset import _safe_clear_dir


def test_safe_clear_dir_creates_empty_when_missing(tmp_path):
    target = tmp_path / "upper"
    _safe_clear_dir(target)
    assert target.is_dir()
    assert list(target.iterdir()) == []


def test_safe_clear_dir_clears_existing_content(tmp_path):
    target = tmp_path / "upper"
    target.mkdir()
    (target / "a").write_text("one")
    (target / "b").mkdir()
    (target / "b" / "c").write_text("two")

    _safe_clear_dir(target)

    assert target.is_dir()
    assert list(target.iterdir()) == []
    assert not (tmp_path / "upper.old").exists()


def test_safe_clear_dir_sweeps_stale_old(tmp_path):
    """A leftover .old from a prior interrupted scrub is removed."""
    target = tmp_path / "upper"
    target.mkdir()
    (target / "fresh").write_text("x")
    stale = tmp_path / "upper.old"
    stale.mkdir()
    (stale / "leftover").write_text("y")

    _safe_clear_dir(target)

    assert target.is_dir()
    assert list(target.iterdir()) == []
    assert not stale.exists()


def test_safe_clear_dir_crash_after_rename_leaves_old_recoverable(tmp_path):
    """If mkdir fails after rename, .old still holds the data — next run recovers."""
    target = tmp_path / "upper"
    target.mkdir()
    (target / "keepme").write_text("data")

    real_mkdir = Path.mkdir

    def boom(self, *args, **kwargs):
        if self == target:
            raise OSError("simulated crash after rename")
        return real_mkdir(self, *args, **kwargs)

    with patch.object(Path, "mkdir", boom):
        with pytest.raises(OSError):
            _safe_clear_dir(target)

    stale = tmp_path / "upper.old"
    assert stale.exists()
    assert (stale / "keepme").read_text() == "data"

    # Second run: no crash, .old is swept and target is recreated empty.
    _safe_clear_dir(target)
    assert target.is_dir()
    assert list(target.iterdir()) == []
    assert not stale.exists()


@patch("kento.reset.subprocess.run", side_effect=_mock_run_stopped)
@patch("kento.reset.resolve_layers", return_value="/new/upper:/new/lower")
@patch("kento.reset.require_root")
def test_reset_sweeps_stale_old_from_prior_crash(mock_root, mock_layers,
                                                  mock_run, tmp_path):
    """reset() cleans up .old dirs left behind by a prior interrupted scrub."""
    lxc_dir = tmp_path / "test"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "rootfs").mkdir()
    (lxc_dir / "upper").mkdir()
    (lxc_dir / "upper" / "live").write_text("1")
    (lxc_dir / "upper.old").mkdir()
    (lxc_dir / "upper.old" / "leftover").write_text("2")
    (lxc_dir / "work").mkdir()

    with patch("kento.reset.resolve_container", return_value=lxc_dir):
        reset("test")

    assert (lxc_dir / "upper").is_dir()
    assert list((lxc_dir / "upper").iterdir()) == []
    assert not (lxc_dir / "upper.old").exists()
