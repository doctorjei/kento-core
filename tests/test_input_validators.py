"""Tests for CLI input validators: port, memory, cores, IP, and bridge existence.

Covers F6 (validate user input) and F18 (--port auto crash in VM branch of create.py).
"""

import argparse
from unittest.mock import MagicMock, patch

import pytest

from kento.cli import (
    _validate_cores,
    _validate_ip,
    _validate_memory,
    _validate_port,
    main,
)


# ---- _validate_port ----------------------------------------------------------

class TestValidatePort:

    def test_explicit_host_guest(self):
        assert _validate_port("10022:22") == "10022:22"

    def test_auto_returns_unchanged(self):
        assert _validate_port("auto") == "auto"

    def test_non_numeric_parts_raises(self):
        with pytest.raises(argparse.ArgumentTypeError):
            _validate_port("abc:def")

    def test_out_of_range_host_raises(self):
        with pytest.raises(argparse.ArgumentTypeError):
            _validate_port("99999:22")

    def test_port_zero_rejected(self):
        with pytest.raises(argparse.ArgumentTypeError):
            _validate_port("0:22")

    def test_missing_colon_rejected(self):
        with pytest.raises(argparse.ArgumentTypeError):
            _validate_port("10022")

    def test_too_many_parts_rejected(self):
        with pytest.raises(argparse.ArgumentTypeError):
            _validate_port("10022:22:33")

    def test_guest_zero_rejected(self):
        with pytest.raises(argparse.ArgumentTypeError):
            _validate_port("10022:0")

    def test_negative_port_rejected(self):
        with pytest.raises(argparse.ArgumentTypeError):
            _validate_port("-1:22")

    def test_guest_out_of_range(self):
        with pytest.raises(argparse.ArgumentTypeError):
            _validate_port("22:70000")


# ---- _validate_memory --------------------------------------------------------

class TestValidateMemory:

    def test_positive_integer(self):
        assert _validate_memory("512") == 512

    def test_zero_rejected(self):
        with pytest.raises(argparse.ArgumentTypeError):
            _validate_memory("0")

    def test_negative_rejected(self):
        with pytest.raises(argparse.ArgumentTypeError):
            _validate_memory("-1")

    def test_non_numeric_rejected(self):
        with pytest.raises(argparse.ArgumentTypeError):
            _validate_memory("abc")

    def test_large_value_accepted(self):
        assert _validate_memory("131072") == 131072


# ---- _validate_cores ---------------------------------------------------------

class TestValidateCores:

    def test_positive_integer(self):
        assert _validate_cores("4") == 4

    def test_zero_rejected(self):
        with pytest.raises(argparse.ArgumentTypeError):
            _validate_cores("0")

    def test_negative_rejected(self):
        with pytest.raises(argparse.ArgumentTypeError):
            _validate_cores("-1")

    def test_non_numeric_rejected(self):
        with pytest.raises(argparse.ArgumentTypeError):
            _validate_cores("abc")

    def test_single_core(self):
        assert _validate_cores("1") == 1


# ---- _validate_ip ------------------------------------------------------------

class TestValidateIp:

    def test_cidr_accepted(self):
        assert _validate_ip("192.168.0.10/24") == "192.168.0.10/24"

    def test_another_cidr_accepted(self):
        assert _validate_ip("10.0.0.1/8") == "10.0.0.1/8"

    def test_garbage_rejected(self):
        with pytest.raises(argparse.ArgumentTypeError):
            _validate_ip("999.999.999.999/foo")

    def test_not_an_ip_rejected(self):
        with pytest.raises(argparse.ArgumentTypeError):
            _validate_ip("not an ip")

    def test_bare_ip_accepted(self):
        # ipaddress.ip_interface accepts a bare IP as /32. That's fine —
        # the caller can use `kento info` to see the resolved address.
        assert _validate_ip("192.168.0.10") == "192.168.0.10"


# ---- Bridge existence (CLI-level) -------------------------------------------

class TestBridgeExistence:

    def test_nonexistent_bridge_rejected(self, capsys):
        """--network bridge=<nonexistent> exits with 'does not exist'."""
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "create", "debian:13",
                  "--name", "x",
                  "--network", "bridge=definitely_does_not_exist_9999"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "does not exist" in err

    def test_existing_bridge_accepted(self):
        """Monkeypatched _bridge_exists=True passes through to create()."""
        mock_create = MagicMock()
        with patch("kento.create.create", mock_create), \
             patch("kento._bridge_exists", return_value=True):
            main(["lxc", "create", "--network", "bridge=fakebr0",
                  "--name", "x", "debian:13"])
        mock_create.assert_called_once()
        assert mock_create.call_args[1]["bridge"] == "fakebr0"


# ---- CLI-level rejection (argparse wires validators correctly) ---------------

class TestCliLevelRejection:

    def test_cli_rejects_bad_port(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "create", "--port", "abc:def", "--name", "x",
                  "debian:13"])
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "invalid port" in err or "must be integers" in err

    def test_cli_rejects_negative_memory(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "create", "--memory", "-1", "--name", "x",
                  "debian:13"])
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "invalid memory" in err or ">= 1" in err

    def test_cli_rejects_zero_cores(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "create", "--cores", "0", "--name", "x",
                  "debian:13"])
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "invalid cores" in err or ">= 1" in err

    def test_cli_rejects_bad_ip(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["lxc", "create", "--ip", "999.999.999.999/foo",
                  "--name", "x", "debian:13"])
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "invalid IP" in err


# ---- F18: `kento vm create --port auto` must not crash ----------------------

class TestF18VmPortAuto:

    @patch("kento.vm._port_is_free", return_value=True)
    @patch("kento.create.resolve_layers", return_value="/a:/b")
    @patch("kento.create.require_root")
    def test_vm_port_auto_reaches_allocate(self, mock_root, mock_layers,
                                            mock_free, tmp_path):
        """VM usermode with port='auto' allocates via allocate_port, not split(':')."""
        from kento.create import create
        vm_dir = tmp_path / "vm"
        vm_dir.mkdir()

        with patch("kento.create.VM_BASE", vm_dir), \
             patch("kento.create.upper_base", return_value=vm_dir / "vmauto"):
            # Before F18 fix this would crash with ValueError: not enough
            # values to unpack from "auto".split(":").
            create("myimage:latest", name="vmauto", mode="vm",
                   port="auto", net_type="usermode")

        port_file = vm_dir / "vmauto" / "kento-port"
        assert port_file.is_file()
        port = port_file.read_text().strip()
        # allocate_port() returns 10022 for the first free port.
        host, guest = port.split(":")
        assert int(host) >= 10022
        assert guest == "22"
