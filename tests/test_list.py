"""Tests for container listing."""

import subprocess
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
def test_list_shows_containers(mock_run, tmp_path, capsys):
    lxc_dir = tmp_path / "mybox"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()
    vm = tmp_path / "vm"

    with patch("kento.list.LXC_BASE", tmp_path), \
         patch("kento.list.VM_BASE", vm):
        list_containers()

    output = capsys.readouterr().out
    assert "mybox" in output
    assert "myimage:latest" in output
    assert "running" in output


@patch("kento.list.subprocess.run", side_effect=_mock_run)
def test_list_with_separate_state_dir(mock_run, tmp_path, capsys):
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
        list_containers()

    output = capsys.readouterr().out
    assert "mybox" in output


@patch("kento.list.subprocess.run")
def test_list_empty(mock_run, tmp_path, capsys):
    vm = tmp_path / "vm"
    with patch("kento.list.LXC_BASE", tmp_path), \
         patch("kento.list.VM_BASE", vm):
        list_containers()

    output = capsys.readouterr().out
    assert "no kento-managed containers found" in output


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


@patch("kento.list.subprocess.run", side_effect=_mock_pve_run)
def test_list_pve_container(mock_run, tmp_path, capsys):
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
        list_containers()

    output = capsys.readouterr().out
    assert "webbox" in output
    assert "pve" in output
    assert "running" in output


@patch("kento.list.subprocess.run", side_effect=_mock_mixed_run)
def test_list_mixed_lxc_and_pve(mock_run, tmp_path, capsys):
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
        list_containers()

    output = capsys.readouterr().out
    assert "mybox" in output
    assert "lxc" in output
    assert "webbox" in output
    assert "pve" in output


@patch("kento.list.subprocess.run", side_effect=_mock_run)
def test_list_shows_mode_column(mock_run, tmp_path, capsys):
    lxc_dir = tmp_path / "mybox"
    lxc_dir.mkdir()
    (lxc_dir / "kento-image").write_text("myimage:latest\n")
    (lxc_dir / "kento-state").write_text(str(lxc_dir) + "\n")
    (lxc_dir / "upper").mkdir()
    vm = tmp_path / "vm"

    with patch("kento.list.LXC_BASE", tmp_path), \
         patch("kento.list.VM_BASE", vm):
        list_containers()

    output = capsys.readouterr().out
    assert "MODE" in output
    assert "CONTAINER" in output


# --- VM mode tests ---


def _mock_vm_du_run(args, **kwargs):
    result = subprocess.CompletedProcess(args, 0)
    if "du" in args:
        result.stdout = "4K\t/whatever\n"
    return result


@patch("kento.list.subprocess.run", side_effect=_mock_vm_du_run)
def test_list_vm_container(mock_run, tmp_path, capsys):
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
        list_containers()

    output = capsys.readouterr().out
    assert "testvm" in output
    assert "vm" in output
    assert "stopped" in output


@patch("kento.list.subprocess.run", side_effect=_mock_vm_du_run)
def test_list_vm_running(mock_run, tmp_path, capsys):
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
        list_containers()

    output = capsys.readouterr().out
    assert "testvm" in output
    assert "running" in output


def _mock_mixed_all_run(args, **kwargs):
    result = subprocess.CompletedProcess(args, 0)
    if "lxc-info" in args:
        result.stdout = "RUNNING"
    elif "du" in args:
        result.stdout = "4K\t/whatever\n"
    return result


@patch("kento.list.subprocess.run", side_effect=_mock_mixed_all_run)
def test_list_mixed_lxc_pve_vm(mock_run, tmp_path, capsys):
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
        list_containers()

    output = capsys.readouterr().out
    assert "mybox" in output
    assert "lxc" in output
    assert "testvm" in output
    assert "vm" in output
