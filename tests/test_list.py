"""Tests for container listing."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from kento.list import list_containers


def _mock_run(args, **kwargs):
    result = subprocess.CompletedProcess(args, 0)
    if "lxc-info" in args:
        result.stdout = "RUNNING"
    elif "du" in args:
        result.stdout = "16K\t/whatever\n"
    return result


@patch("kento.list.subprocess.run", side_effect=_mock_run)
def test_list_shows_containers(mock_run, tmp_path):
    lxc_dir = tmp_path / "mybox"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()
    vm = tmp_path / "vm"

    with patch("kento.list.LXC_BASE", tmp_path), \
         patch("kento.list.VM_BASE", vm):
        result = list_containers()

    assert "mybox" in result
    assert "myimage:latest" in result
    assert "running" in result


@patch("kento.list.subprocess.run", side_effect=_mock_run)
def test_list_with_separate_state_dir(mock_run, tmp_path):
    lxc_dir = tmp_path / "mybox"
    lxc_dir.mkdir()
    state = tmp_path / "user-state" / "mybox"
    state.mkdir(parents=True)
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-state").write_text(str(state) + "\n")
    (state / "upper").mkdir()
    vm = tmp_path / "vm"

    with patch("kento.list.LXC_BASE", tmp_path), \
         patch("kento.list.VM_BASE", vm):
        result = list_containers()

    assert "mybox" in result


@patch("kento.list.subprocess.run")
def test_list_empty(mock_run, tmp_path):
    vm = tmp_path / "vm"
    with patch("kento.list.LXC_BASE", tmp_path), \
         patch("kento.list.VM_BASE", vm):
        result = list_containers()

    assert "no instances found" in result


@patch("kento.list.subprocess.run", side_effect=_mock_run)
def test_list_skips_unreadable_entry(mock_run, tmp_path):
    """One instance dir that raises OSError on read (e.g. a concurrent
    `kento destroy` rmtree racing between glob and read) must be skipped,
    not abort the whole listing and hide the healthy instances."""
    # Healthy instance.
    good = tmp_path / "goodbox"
    good.mkdir()
    (good / "kento-image").write_text("good-image:latest\n")
    (good / "kento-state").write_text(str(good) + "\n")
    (good / "upper").mkdir()

    # Bad instance: kento-image present at glob time but read_text raises.
    bad = tmp_path / "badbox"
    bad.mkdir()
    bad_image = bad / "kento-image"
    bad_image.write_text("bad-image:latest\n")
    (bad / "kento-state").write_text(str(bad) + "\n")
    (bad / "upper").mkdir()

    vm = tmp_path / "vm"

    real_read_text = Path.read_text

    def flaky_read_text(self, *args, **kwargs):
        if self == bad_image:
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return real_read_text(self, *args, **kwargs)

    with patch("kento.list.LXC_BASE", tmp_path), \
         patch("kento.list.VM_BASE", vm), \
         patch.object(Path, "read_text", flaky_read_text):
        result = list_containers()

    # Healthy instance still listed.
    assert "goodbox" in result
    assert "good-image:latest" in result
    # Bad instance skipped, not crashing the listing.
    assert "badbox" not in result


# --- PVE mode tests ---


def _mock_pve_run(args, **kwargs):
    result = subprocess.CompletedProcess(args, 0)
    if "pct" in args and "status" in args:
        result.stdout = "status: running"
    elif "du" in args:
        result.stdout = "8K\t/whatever\n"
    return result


def _mock_mixed_run(args, **kwargs):
    result = subprocess.CompletedProcess(args, 0)
    if "lxc-info" in args:
        result.stdout = "RUNNING"
    elif "pct" in args and "status" in args:
        result.stdout = "status: running"
    elif "du" in args:
        result.stdout = "4K\t/whatever\n"
    return result


@patch("kento.pve_config_exists", return_value=True)
@patch("kento.list.pve_config_exists", return_value=True)
@patch("kento.list.subprocess.run", side_effect=_mock_pve_run)
def test_list_pve_container(mock_run, mock_cfg, mock_cfg2, tmp_path):
    lxc_dir = tmp_path / "100"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-mode").write_text("pve\n")
    (lxc_dir / "kento-name").write_text("webbox\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()
    vm = tmp_path / "vm"

    with patch("kento.list.LXC_BASE", tmp_path), \
         patch("kento.list.VM_BASE", vm):
        result = list_containers()

    assert "webbox" in result
    assert "pve-lxc" in result
    assert "running" in result


@patch("kento.list.pve_config_exists", return_value=False)
@patch("kento.list.subprocess.run", side_effect=_mock_pve_run)
def test_list_pve_orphan(mock_run, mock_cfg, tmp_path):
    """A pve instance whose PVE config is gone shows as 'orphan'."""
    lxc_dir = tmp_path / "100"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-mode").write_text("pve\n")
    (lxc_dir / "kento-name").write_text("webbox\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()
    vm = tmp_path / "vm"

    with patch("kento.list.LXC_BASE", tmp_path), \
         patch("kento.list.VM_BASE", vm):
        result = list_containers()

    assert "webbox" in result
    assert "orphan" in result
    # We must NOT have shelled out to pct status for an orphan.
    for call in mock_run.call_args_list:
        argv = call.args[0] if call.args else call.kwargs.get("args", [])
        assert "pct" not in argv


@patch("kento.pve_config_exists", return_value=True)
@patch("kento.list.pve_config_exists", return_value=True)
@patch("kento.list.subprocess.run", side_effect=_mock_mixed_run)
def test_list_mixed_lxc_and_pve(mock_run, mock_cfg, mock_cfg2, tmp_path):
    # LXC container
    lxc = tmp_path / "mybox"
    lxc.mkdir()
    (lxc / "kento-image").write_text("debian:12\n")
    (lxc / "kento-mode").write_text("lxc\n")
    (lxc / "kento-state").write_text(str(lxc) + "\n")
    (lxc / "upper").mkdir()

    # PVE container
    pve = tmp_path / "100"
    pve.mkdir()
    (pve / "kento-image").write_text("ubuntu:22.04\n")
    (pve / "kento-mode").write_text("pve\n")
    (pve / "kento-name").write_text("webbox\n")
    (pve / "kento-state").write_text(str(pve) + "\n")
    (pve / "upper").mkdir()
    vm = tmp_path / "vm"

    with patch("kento.list.LXC_BASE", tmp_path), \
         patch("kento.list.VM_BASE", vm):
        result = list_containers()

    assert "mybox" in result
    assert "webbox" in result
    # lxc mode shows as "lxc", pve mode shows as "pve-lxc"
    assert "lxc" in result
    assert "pve-lxc" in result


@patch("kento.list.subprocess.run", side_effect=_mock_run)
def test_list_shows_type_column(mock_run, tmp_path):
    lxc_dir = tmp_path / "mybox"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()
    vm = tmp_path / "vm"

    with patch("kento.list.LXC_BASE", tmp_path), \
         patch("kento.list.VM_BASE", vm):
        result = list_containers()

    assert "TYPE" in result
    assert "NAME" in result
    assert "MODE" not in result


# --- VM mode tests ---


def _mock_vm_du_run(args, **kwargs):
    result = subprocess.CompletedProcess(args, 0)
    if "du" in args:
        result.stdout = "4K\t/whatever\n"
    return result


@patch("kento.list.subprocess.run", side_effect=_mock_vm_du_run)
def test_list_vm_container(mock_run, tmp_path):
    lxc = tmp_path / "lxc"
    lxc.mkdir()
    vm = tmp_path / "vm"
    vm.mkdir()
    vm_dir = vm / "testvm"
    vm_dir.mkdir()
    (vm_dir / "kento-image").write_text("vm-image:latest\n")
    (vm_dir / "kento-mode").write_text("vm\n")
    (vm_dir / "kento-name").write_text("testvm\n")
    (vm_dir / "kento-state").write_text(str(vm_dir) + "\n")
    (vm_dir / "upper").mkdir()

    with patch("kento.list.LXC_BASE", lxc), \
         patch("kento.list.VM_BASE", vm), \
         patch("kento.vm.is_vm_running", return_value=False):
        result = list_containers()

    assert "testvm" in result
    assert "vm" in result
    assert "stopped" in result


@patch("kento.list.subprocess.run", side_effect=_mock_vm_du_run)
def test_list_vm_running(mock_run, tmp_path):
    lxc = tmp_path / "lxc"
    lxc.mkdir()
    vm = tmp_path / "vm"
    vm.mkdir()
    vm_dir = vm / "testvm"
    vm_dir.mkdir()
    (vm_dir / "kento-image").write_text("vm-image:latest\n")
    (vm_dir / "kento-mode").write_text("vm\n")
    (vm_dir / "kento-name").write_text("testvm\n")
    (vm_dir / "kento-state").write_text(str(vm_dir) + "\n")
    (vm_dir / "upper").mkdir()

    with patch("kento.list.LXC_BASE", lxc), \
         patch("kento.list.VM_BASE", vm), \
         patch("kento.vm.is_vm_running", return_value=True):
        result = list_containers()

    assert "testvm" in result
    assert "running" in result


def _mock_mixed_all_run(args, **kwargs):
    result = subprocess.CompletedProcess(args, 0)
    if "lxc-info" in args:
        result.stdout = "RUNNING"
    elif "du" in args:
        result.stdout = "4K\t/whatever\n"
    return result


@patch("kento.list.subprocess.run", side_effect=_mock_mixed_all_run)
def test_list_mixed_lxc_pve_vm(mock_run, tmp_path):
    lxc = tmp_path / "lxc"
    lxc.mkdir()
    vm = tmp_path / "vm"
    vm.mkdir()

    # LXC container
    lxc_dir = lxc / "mybox"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("debian:12\n")
    (lxc_dir / "kento-mode").write_text("lxc\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()

    # VM container
    vm_dir = vm / "testvm"
    vm_dir.mkdir()
    (vm_dir / "kento-image").write_text("vm-image:latest\n")
    (vm_dir / "kento-mode").write_text("vm\n")
    (vm_dir / "kento-name").write_text("testvm\n")
    (vm_dir / "kento-state").write_text(str(vm_dir) + "\n")
    (vm_dir / "upper").mkdir()

    with patch("kento.list.LXC_BASE", lxc), \
         patch("kento.list.VM_BASE", vm), \
         patch("kento.vm.is_vm_running", return_value=False):
        result = list_containers()

    assert "mybox" in result
    assert "lxc" in result
    assert "testvm" in result
    assert "vm" in result


# --- TYPE column tests ---


@patch("kento.list.subprocess.run", side_effect=_mock_run)
def test_lxc_mode_shows_type_lxc(mock_run, tmp_path):
    """Containers with mode 'lxc' should display TYPE 'lxc'."""
    lxc_dir = tmp_path / "mybox"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("debian:12\n")
    (lxc_dir / "kento-mode").write_text("lxc\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()
    vm = tmp_path / "vm"

    with patch("kento.list.LXC_BASE", tmp_path), \
         patch("kento.list.VM_BASE", vm):
        result = list_containers()

    lines = result.splitlines()
    # Data line (skip header + separator)
    data_line = lines[2]
    assert "lxc" in data_line


@patch("kento.pve_config_exists", return_value=True)
@patch("kento.list.pve_config_exists", return_value=True)
@patch("kento.list.subprocess.run", side_effect=_mock_pve_run)
def test_pve_mode_shows_type_pve_lxc(mock_run, mock_cfg, mock_cfg2, tmp_path):
    """Containers with mode 'pve' should display TYPE 'pve-lxc'."""
    lxc_dir = tmp_path / "100"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("ubuntu:22.04\n")
    (lxc_dir / "kento-mode").write_text("pve\n")
    (lxc_dir / "kento-name").write_text("pvehost\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()
    vm = tmp_path / "vm"

    with patch("kento.list.LXC_BASE", tmp_path), \
         patch("kento.list.VM_BASE", vm):
        result = list_containers()

    lines = result.splitlines()
    data_line = lines[2]
    assert "pve-lxc" in data_line
    assert "pve-vm" not in data_line


@patch("kento.list.subprocess.run", side_effect=_mock_vm_du_run)
def test_vm_mode_shows_type_vm(mock_run, tmp_path):
    """Containers with mode 'vm' should display TYPE 'vm'."""
    lxc = tmp_path / "lxc"
    lxc.mkdir()
    vm = tmp_path / "vm"
    vm.mkdir()
    vm_dir = vm / "testvm"
    vm_dir.mkdir()
    (vm_dir / "kento-image").write_text("vm-image:latest\n")
    (vm_dir / "kento-mode").write_text("vm\n")
    (vm_dir / "kento-name").write_text("testvm\n")
    (vm_dir / "kento-state").write_text(str(vm_dir) + "\n")
    (vm_dir / "upper").mkdir()

    with patch("kento.list.LXC_BASE", lxc), \
         patch("kento.list.VM_BASE", vm), \
         patch("kento.vm.is_vm_running", return_value=False):
        result = list_containers()

    lines = result.splitlines()
    data_line = lines[2]
    assert "vm" in data_line


# --- Scope filtering tests ---


@patch("kento.list.subprocess.run", side_effect=_mock_mixed_all_run)
def test_scope_none_shows_all(mock_run, tmp_path):
    """scope=None should show both LXC and VM entries."""
    lxc = tmp_path / "lxc"
    lxc.mkdir()
    vm = tmp_path / "vm"
    vm.mkdir()

    lxc_dir = lxc / "mybox"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("debian:12\n")
    (lxc_dir / "kento-mode").write_text("lxc\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()

    vm_dir = vm / "testvm"
    vm_dir.mkdir()
    (vm_dir / "kento-image").write_text("vm-image:latest\n")
    (vm_dir / "kento-mode").write_text("vm\n")
    (vm_dir / "kento-name").write_text("testvm\n")
    (vm_dir / "kento-state").write_text(str(vm_dir) + "\n")
    (vm_dir / "upper").mkdir()

    with patch("kento.list.LXC_BASE", lxc), \
         patch("kento.list.VM_BASE", vm), \
         patch("kento.vm.is_vm_running", return_value=False):
        result = list_containers(scope=None)

    assert "mybox" in result
    assert "testvm" in result


@patch("kento.list.subprocess.run", side_effect=_mock_mixed_all_run)
def test_scope_lxc_shows_only_lxc(mock_run, tmp_path):
    """scope='lxc' should show only LXC/PVE entries, not VMs."""
    lxc = tmp_path / "lxc"
    lxc.mkdir()
    vm = tmp_path / "vm"
    vm.mkdir()

    lxc_dir = lxc / "mybox"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("debian:12\n")
    (lxc_dir / "kento-mode").write_text("lxc\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()

    vm_dir = vm / "testvm"
    vm_dir.mkdir()
    (vm_dir / "kento-image").write_text("vm-image:latest\n")
    (vm_dir / "kento-mode").write_text("vm\n")
    (vm_dir / "kento-name").write_text("testvm\n")
    (vm_dir / "kento-state").write_text(str(vm_dir) + "\n")
    (vm_dir / "upper").mkdir()

    with patch("kento.list.LXC_BASE", lxc), \
         patch("kento.list.VM_BASE", vm), \
         patch("kento.vm.is_vm_running", return_value=False):
        result = list_containers(scope="lxc")

    assert "mybox" in result
    assert "testvm" not in result


# --- PVE-VM mode tests ---


class TestListPveVm:
    @patch("kento.list.subprocess.run", side_effect=_mock_vm_du_run)
    @patch("kento.list.is_running", return_value=False)
    def test_pve_vm_shows_as_vm_type(self, mock_is_running, mock_run, tmp_path):
        """Containers with mode 'pve-vm' should display TYPE 'pve-vm'."""
        lxc = tmp_path / "lxc"
        vm = tmp_path / "vm"
        lxc.mkdir()
        vm.mkdir()
        vm_dir = vm / "test"
        vm_dir.mkdir()
        (vm_dir / "kento-image").write_text("myimage\n")
        (vm_dir / "kento-mode").write_text("pve-vm\n")
        (vm_dir / "kento-name").write_text("test\n")
        (vm_dir / "kento-vmid").write_text("100\n")
        (vm_dir / "kento-state").write_text(str(vm_dir) + "\n")
        (vm_dir / "upper").mkdir()

        with patch("kento.list.LXC_BASE", lxc), \
             patch("kento.list.VM_BASE", vm):
            result = list_containers()

        assert "test" in result
        lines = result.splitlines()
        data_line = lines[2]
        assert "pve-vm" in data_line

    @patch("kento.list.subprocess.run", side_effect=_mock_vm_du_run)
    @patch("kento.list.is_running", return_value=False)
    def test_pve_vm_included_in_vm_scope(self, mock_is_running, mock_run, tmp_path):
        """Containers with mode 'pve-vm' should appear with scope='vm'."""
        lxc = tmp_path / "lxc"
        vm = tmp_path / "vm"
        lxc.mkdir()
        vm.mkdir()
        vm_dir = vm / "test"
        vm_dir.mkdir()
        (vm_dir / "kento-image").write_text("myimage\n")
        (vm_dir / "kento-mode").write_text("pve-vm\n")
        (vm_dir / "kento-name").write_text("test\n")
        (vm_dir / "kento-vmid").write_text("100\n")
        (vm_dir / "kento-state").write_text(str(vm_dir) + "\n")
        (vm_dir / "upper").mkdir()

        with patch("kento.list.LXC_BASE", lxc), \
             patch("kento.list.VM_BASE", vm):
            result = list_containers(scope="vm")

        assert "test" in result

    @patch("kento.list.pve_config_exists", return_value=False)
    @patch("kento.list.subprocess.run", side_effect=_mock_vm_du_run)
    def test_pve_vm_orphan(self, mock_run, mock_cfg, tmp_path):
        """A pve-vm whose PVE config is gone shows as 'orphan' (vmid read
        from kento-vmid; qm status never invoked)."""
        lxc = tmp_path / "lxc"
        vm = tmp_path / "vm"
        lxc.mkdir()
        vm.mkdir()
        vm_dir = vm / "test"
        vm_dir.mkdir()
        (vm_dir / "kento-image").write_text("myimage\n")
        (vm_dir / "kento-mode").write_text("pve-vm\n")
        (vm_dir / "kento-name").write_text("test\n")
        (vm_dir / "kento-vmid").write_text("100\n")
        (vm_dir / "kento-state").write_text(str(vm_dir) + "\n")
        (vm_dir / "upper").mkdir()

        with patch("kento.list.LXC_BASE", lxc), \
             patch("kento.list.VM_BASE", vm):
            result = list_containers()

        assert "test" in result
        assert "orphan" in result
        mock_cfg.assert_called_once_with("100", "pve-vm")

    @patch("kento.list.subprocess.run", side_effect=_mock_vm_du_run)
    def test_pve_vm_excluded_from_lxc_scope(self, mock_run, tmp_path):
        """Containers with mode 'pve-vm' should NOT appear with scope='lxc'."""
        lxc = tmp_path / "lxc"
        vm = tmp_path / "vm"
        lxc.mkdir()
        vm.mkdir()
        vm_dir = vm / "test"
        vm_dir.mkdir()
        (vm_dir / "kento-image").write_text("myimage\n")
        (vm_dir / "kento-mode").write_text("pve-vm\n")
        (vm_dir / "kento-name").write_text("test\n")
        (vm_dir / "kento-state").write_text(str(vm_dir) + "\n")
        (vm_dir / "upper").mkdir()

        with patch("kento.list.LXC_BASE", lxc), \
             patch("kento.list.VM_BASE", vm):
            result = list_containers(scope="lxc")

        assert "test" not in result or "no instances found" in result


@patch("kento.list.subprocess.run", side_effect=_mock_mixed_all_run)
def test_scope_vm_shows_only_vm(mock_run, tmp_path):
    """scope='vm' should show only VM entries, not LXC/PVE."""
    lxc = tmp_path / "lxc"
    lxc.mkdir()
    vm = tmp_path / "vm"
    vm.mkdir()

    lxc_dir = lxc / "mybox"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("debian:12\n")
    (lxc_dir / "kento-mode").write_text("lxc\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()

    vm_dir = vm / "testvm"
    vm_dir.mkdir()
    (vm_dir / "kento-image").write_text("vm-image:latest\n")
    (vm_dir / "kento-mode").write_text("vm\n")
    (vm_dir / "kento-name").write_text("testvm\n")
    (vm_dir / "kento-state").write_text(str(vm_dir) + "\n")
    (vm_dir / "upper").mkdir()

    with patch("kento.list.LXC_BASE", lxc), \
         patch("kento.list.VM_BASE", vm), \
         patch("kento.vm.is_vm_running", return_value=False):
        result = list_containers(scope="vm")

    assert "testvm" in result
    assert "mybox" not in result


# --- show_size opt-in tests (v1.2.1) ---


@patch("kento.list.subprocess.run", side_effect=_mock_run)
def test_list_default_omits_upper_size_column(mock_run, tmp_path):
    """By default the UPPER SIZE column is absent from the header and rows."""
    lxc_dir = tmp_path / "mybox"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()
    vm = tmp_path / "vm"

    with patch("kento.list.LXC_BASE", tmp_path), \
         patch("kento.list.VM_BASE", vm):
        result = list_containers()

    assert "UPPER SIZE" not in result
    assert "mybox" in result
    assert "STATUS" in result


@patch("kento.list.subprocess.run", side_effect=_mock_run)
def test_list_show_size_true_includes_upper_size_column(mock_run, tmp_path):
    """show_size=True restores the UPPER SIZE column."""
    lxc_dir = tmp_path / "mybox"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()
    vm = tmp_path / "vm"

    with patch("kento.list.LXC_BASE", tmp_path), \
         patch("kento.list.VM_BASE", vm):
        result = list_containers(show_size=True)

    assert "UPPER SIZE" in result
    assert "16K" in result


@patch("kento.list.subprocess.run", side_effect=_mock_run)
def test_list_default_does_not_invoke_du(mock_run, tmp_path):
    """Without show_size, subprocess.run is never called with 'du'."""
    lxc_dir = tmp_path / "mybox"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()
    vm = tmp_path / "vm"

    with patch("kento.list.LXC_BASE", tmp_path), \
         patch("kento.list.VM_BASE", vm):
        list_containers()

    for call in mock_run.call_args_list:
        argv = call.args[0] if call.args else call.kwargs.get("args", [])
        assert "du" not in argv, f"du should not be invoked, got call: {argv}"


@patch("kento.list.subprocess.run", side_effect=_mock_run)
def test_list_show_size_invokes_du_per_row(mock_run, tmp_path):
    """With show_size=True, du -sh is called once per instance row."""
    for name in ("alpha", "bravo"):
        d = tmp_path / name
        d.mkdir()
        (d / "kento-image").write_text("img:latest\n")
        (d / "kento-state").write_text(str(d) + "\n")
        (d / "upper").mkdir()
    vm = tmp_path / "vm"

    with patch("kento.list.LXC_BASE", tmp_path), \
         patch("kento.list.VM_BASE", vm):
        list_containers(show_size=True)

    du_calls = [
        call for call in mock_run.call_args_list
        if (call.args and "du" in call.args[0])
        or ("du" in call.kwargs.get("args", []))
    ]
    assert len(du_calls) == 2


@patch("kento.list.subprocess.run", side_effect=_mock_run)
def test_list_default_columns_widths_align_without_size(mock_run, tmp_path):
    """Default output's four columns line up without UPPER SIZE."""
    lxc_dir = tmp_path / "mybox"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()
    vm = tmp_path / "vm"

    with patch("kento.list.LXC_BASE", tmp_path), \
         patch("kento.list.VM_BASE", vm):
        result = list_containers()

    lines = result.splitlines()
    # header + separator + 1 data row
    assert len(lines) == 3
    # Separator is dashes split by "  " into 4 groups (4 columns)
    sep_groups = lines[1].split("  ")
    assert len(sep_groups) == 4
    assert all(set(g) == {"-"} for g in sep_groups)


# --- list --json tests (v1.5.3) ---


@patch("kento.list.subprocess.run", side_effect=_mock_run)
def test_list_json_emits_array_with_expected_keys(mock_run, tmp_path):
    """--json emits a JSON array; each object carries the inspect --json keys."""
    lxc_dir = tmp_path / "mybox"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-mode").write_text("lxc\n")
    (lxc_dir / "kento-name").write_text("mybox\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()
    vm = tmp_path / "vm"

    with patch("kento.list.LXC_BASE", tmp_path), \
         patch("kento.list.VM_BASE", vm):
        data = json.loads(list_containers(as_json=True))

    assert isinstance(data, list)
    assert len(data) == 1
    entry = data[0]
    assert entry["name"] == "mybox"
    assert entry["type"] == "LXC"
    assert entry["mode"] == "lxc"
    assert entry["image"] == "myimage:latest"
    assert entry["status"] == "running"


@patch("kento.list.subprocess.run")
def test_list_json_empty_is_array(mock_run, tmp_path):
    """Zero instances → '[]', NOT the human '(no instances found)' string."""
    vm = tmp_path / "vm"
    with patch("kento.list.LXC_BASE", tmp_path), \
         patch("kento.list.VM_BASE", vm):
        result = list_containers(as_json=True)

    assert "no instances found" not in result
    assert json.loads(result) == []


@patch("kento.pve_config_exists", return_value=True)
@patch("kento.list.pve_config_exists", return_value=True)
@patch("kento.list.subprocess.run", side_effect=_mock_pve_run)
def test_list_json_pve_mode_normalized(mock_run, mock_cfg, mock_cfg2, tmp_path):
    """A pve-lxc instance reports mode == 'pve-lxc' (normalized) in --json,
    matching inspect --json."""
    lxc_dir = tmp_path / "100"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-mode").write_text("pve\n")
    (lxc_dir / "kento-name").write_text("webbox\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()
    vm = tmp_path / "vm"

    with patch("kento.list.LXC_BASE", tmp_path), \
         patch("kento.list.VM_BASE", vm):
        data = json.loads(list_containers(as_json=True))

    assert len(data) == 1
    assert data[0]["mode"] == "pve-lxc"
    assert data[0]["type"] == "LXC"


@patch("kento.list.subprocess.run", side_effect=_mock_run)
def test_list_json_includes_optional_fields_when_present(mock_run, tmp_path):
    """vmid / mac / environment surface in the JSON object when present."""
    vm = tmp_path / "vm"
    vm.mkdir()
    vm_dir = vm / "testvm"
    vm_dir.mkdir()
    (vm_dir / "kento-image").write_text("vm-image:latest\n")
    (vm_dir / "kento-mode").write_text("vm\n")
    (vm_dir / "kento-name").write_text("testvm\n")
    (vm_dir / "kento-vmid").write_text("200\n")
    (vm_dir / "kento-mac").write_text("aa:bb:cc:dd:ee:ff\n")
    (vm_dir / "kento-env").write_text("A=1\nB=2\n")
    (vm_dir / "kento-state").write_text(str(vm_dir) + "\n")
    (vm_dir / "upper").mkdir()
    lxc = tmp_path / "lxc"

    with patch("kento.list.LXC_BASE", lxc), \
         patch("kento.list.VM_BASE", vm), \
         patch("kento.vm.is_vm_running", return_value=False):
        data = json.loads(list_containers(as_json=True))

    assert len(data) == 1
    entry = data[0]
    assert entry["type"] == "VM"
    assert entry["mode"] == "vm"
    assert entry["vmid"] == 200
    assert entry["mac"] == "aa:bb:cc:dd:ee:ff"
    assert entry["environment"] == ["A=1", "B=2"]


@patch("kento.info.is_running", return_value=False)
@patch("kento.list.subprocess.run", side_effect=_mock_run)
def test_list_json_keys_consistent_with_inspect_json(mock_run, mock_running,
                                                     tmp_path, capsys):
    """Key parity: every key emitted by list --json is also emitted by
    inspect --json for the same instance, with matching values."""
    from kento.info import info

    lxc_dir = tmp_path / "mybox"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-mode").write_text("pve\n")
    (lxc_dir / "kento-name").write_text("mybox\n")
    (lxc_dir / "kento-vmid").write_text("100\n")
    (lxc_dir / "kento-mac").write_text("aa:bb:cc:dd:ee:ff\n")
    (lxc_dir / "kento-env").write_text("A=1\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()
    vm = tmp_path / "vm"

    with patch("kento.list.LXC_BASE", tmp_path), \
         patch("kento.list.VM_BASE", vm), \
         patch("kento.list.pve_config_exists", return_value=True):
        list_entry = json.loads(list_containers(as_json=True))
    list_entry = list_entry[0]

    # info() still prints (not yet converted); capture via capsys
    info("mybox", container_dir=lxc_dir, mode="pve-lxc", as_json=True)
    inspect_data = json.loads(capsys.readouterr().out)

    # Every list key is present in inspect output with the same value.
    for key, value in list_entry.items():
        assert key in inspect_data, f"list key {key!r} missing from inspect"
        assert inspect_data[key] == value, (
            f"value mismatch for {key!r}: "
            f"list={value!r} inspect={inspect_data[key]!r}")


@patch("kento.list.subprocess.run", side_effect=_mock_run)
def test_list_json_skips_unreadable_entry(mock_run, tmp_path):
    """A racing destroy drops that instance from the array, never aborts."""
    good = tmp_path / "goodbox"
    good.mkdir()
    (good / "kento-image").write_text("good-image:latest\n")
    (good / "kento-mode").write_text("lxc\n")
    (good / "kento-name").write_text("goodbox\n")
    (good / "kento-state").write_text(str(good) + "\n")
    (good / "upper").mkdir()

    bad = tmp_path / "badbox"
    bad.mkdir()
    bad_image = bad / "kento-image"
    bad_image.write_text("bad-image:latest\n")
    (bad / "kento-state").write_text(str(bad) + "\n")
    (bad / "upper").mkdir()

    vm = tmp_path / "vm"
    real_read_text = Path.read_text

    def flaky_read_text(self, *args, **kwargs):
        if self == bad_image:
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return real_read_text(self, *args, **kwargs)

    with patch("kento.list.LXC_BASE", tmp_path), \
         patch("kento.list.VM_BASE", vm), \
         patch.object(Path, "read_text", flaky_read_text):
        data = json.loads(list_containers(as_json=True))

    names = [e["name"] for e in data]
    assert "goodbox" in names
    assert "badbox" not in names


@patch("kento.list.subprocess.run", side_effect=_mock_run)
def test_list_json_size_includes_upper_size(mock_run, tmp_path):
    """show_size=True includes upper_size in the JSON object; omitted otherwise."""
    lxc_dir = tmp_path / "mybox"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-mode").write_text("lxc\n")
    (lxc_dir / "kento-name").write_text("mybox\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()
    vm = tmp_path / "vm"

    with patch("kento.list.LXC_BASE", tmp_path), \
         patch("kento.list.VM_BASE", vm):
        no_size = json.loads(list_containers(as_json=True))
    assert "upper_size" not in no_size[0]

    with patch("kento.list.LXC_BASE", tmp_path), \
         patch("kento.list.VM_BASE", vm):
        with_size = json.loads(list_containers(as_json=True, show_size=True))
    assert with_size[0]["upper_size"] == "16K"
