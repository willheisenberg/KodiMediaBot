"""Tests for the configurable display power command runner."""

import dataclasses
import os
import subprocess
import sys
from unittest.mock import patch

os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "test:token")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from kodibot.core import power


def _with_commands(monkeypatch, on_cmd="cmd-on", off_cmd="cmd-off", timeout=15.0):
    """Replaces the frozen CFG on the power module with a tweaked copy."""
    monkeypatch.setattr(
        power,
        "CFG",
        dataclasses.replace(
            power.CFG,
            display_power_on_cmd=on_cmd,
            display_power_off_cmd=off_cmd,
            display_command_timeout=timeout,
        ),
    )


def _result(returncode, stderr=""):
    return subprocess.CompletedProcess(args="cmd", returncode=returncode, stdout="", stderr=stderr)


class TestRunDisplayPower:
    def test_returns_true_on_exit_zero(self, monkeypatch):
        _with_commands(monkeypatch)
        with patch("subprocess.run", return_value=_result(0)) as run:
            assert power.run_display_power(True) is True
        assert run.call_count == 1

    def test_returns_false_on_nonzero_exit(self, monkeypatch):
        _with_commands(monkeypatch)
        with patch("subprocess.run", return_value=_result(1, "boom")):
            assert power.run_display_power(True) is False

    def test_returns_false_on_timeout(self, monkeypatch):
        _with_commands(monkeypatch)
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 15)):
            assert power.run_display_power(True) is False

    def test_on_uses_the_on_command(self, monkeypatch):
        _with_commands(monkeypatch, on_cmd="turn-it-on")
        with patch("subprocess.run", return_value=_result(0)) as run:
            power.run_display_power(True)
        assert run.call_args.args[0] == "turn-it-on"

    def test_off_uses_the_off_command(self, monkeypatch):
        _with_commands(monkeypatch, off_cmd="turn-it-off")
        with patch("subprocess.run", return_value=_result(0)) as run:
            power.run_display_power(False)
        assert run.call_args.args[0] == "turn-it-off"

    def test_passes_the_configured_timeout(self, monkeypatch):
        _with_commands(monkeypatch, timeout=42.0)
        with patch("subprocess.run", return_value=_result(0)) as run:
            power.run_display_power(True)
        assert run.call_args.kwargs["timeout"] == 42.0

    def test_empty_command_is_a_failure_and_runs_nothing(self, monkeypatch):
        _with_commands(monkeypatch, on_cmd="   ")
        with patch("subprocess.run") as run:
            assert power.run_display_power(True) is False
        assert run.call_count == 0
