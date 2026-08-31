"""Tests for Projector (Beamer) native LIRC IR control logic."""

import os
import sys
import struct
import pytest
from unittest.mock import mock_open, patch

os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "test:token")
os.environ.setdefault("PROJECTOR_LIRC_DEVICE", "/dev/lirc0")
os.environ.setdefault("PROJECTOR_ADDRESS", "0x08")
os.environ.setdefault("PROJECTOR_POWER_ON_CODE", "0x03")
os.environ.setdefault("PROJECTOR_POWER_OFF_CODE", "0x00")
os.environ.setdefault("PROJECTOR_POWER_ON_REPEATS", "4")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from kodibot.core import projector as PJ


def test_projector_config_loading():
    """Verify that configuration loaded from environment is mapped correctly."""
    from kodibot.config import CFG

    assert CFG.projector_lirc_device == "/dev/lirc0"
    assert CFG.projector_address == 0x08
    assert CFG.projector_power_on_code == 0x03
    assert CFG.projector_power_off_code == 0x00
    assert CFG.projector_power_on_repeats == 4


def test_display_power_config_defaults():
    """Display power settings fall back to the shipped IR commands."""
    from kodibot.config import CFG

    assert CFG.display_button_label == "📽 Beamer"
    assert CFG.display_power_on_cmd == "python -m kodibot.core.projector on"
    assert CFG.display_power_off_cmd == "python -m kodibot.core.projector off"
    assert CFG.display_command_timeout == 15.0
    assert CFG.ui_state_file == "/data/state/telegram_ui_state.json"
    assert CFG.bot_language == "en"


def test_projector_connect_missing_device():
    """Verify connect() returns False when LIRC device is missing."""
    projector = PJ.ProjectorController()
    with patch("os.path.exists", return_value=False):
        assert projector.connect() is False


def test_projector_connect_success():
    """Verify connect() returns True when LIRC device is present."""
    projector = PJ.ProjectorController()
    with patch("os.path.exists", return_value=True):
        assert projector.connect() is True


def test_nec_wave_structure():
    """Verify that generated NEC waveform via LIRC has the expected pulses and spaces."""
    projector = PJ.ProjectorController()

    mock_file = mock_open()
    with patch("os.path.exists", return_value=True), patch("builtins.open", mock_file):
        res = projector.send_command(0x08, 0x03, repeat_count=1)
        assert res is True

    # Get written binary data
    handle = mock_file()
    handle.write.assert_called_once()
    written_bytes = handle.write.call_args[0][0]

    # Unpack the written integers (unsigned 32-bit ints)
    num_ints = len(written_bytes) // 4
    pulses = list(struct.unpack(f"{num_ints}I", written_bytes))

    # Should contain header, 32 payload bits (each bit has 1 pulse + 1 space = 2 elements), plus 1 stop bit
    # Total elements = 2 + 64 + 1 = 67 elements
    assert len(pulses) == 67

    # Verify header
    assert pulses[0] == 9000  # header pulse
    assert pulses[1] == 4500  # header space

    # Verify stop bit
    assert pulses[-1] == 560  # stop bit pulse

    # Verify bits (alternating 560us pulse and 560us/1690us space)
    payload_elements = pulses[2:-1]
    assert len(payload_elements) == 64
    
    # Pulses (even indices in payload_elements) must be exactly 560
    assert all(p == 560 for p in payload_elements[0::2])
    # Spaces (odd indices) must be either 560 or 1690
    assert all(s in (560, 1690) for s in payload_elements[1::2])


def test_power_on(monkeypatch):
    """Verify that power_on() sends 4 waves with correct intervals."""
    projector = PJ.ProjectorController()
    sent_commands = []

    def mock_send_command(address, command, repeat_count, delay_ms):
        sent_commands.append(
            {
                "address": address,
                "command": command,
                "repeat_count": repeat_count,
                "delay_ms": delay_ms,
            }
        )
        return True

    monkeypatch.setattr(projector, "send_command", mock_send_command)

    res = projector.power_on()
    assert res is True
    assert len(sent_commands) == 1
    assert sent_commands[0]["address"] == 0x08
    assert sent_commands[0]["command"] == 0x03
    from kodibot.config import CFG
    assert sent_commands[0]["repeat_count"] == CFG.projector_power_on_repeats
    assert sent_commands[0]["delay_ms"] == 40


def test_power_off(monkeypatch):
    """Verify that power_off() sends two 4-burst commands separated by 1 second."""
    projector = PJ.ProjectorController()
    sent_commands = []
    slept_durations = []

    def mock_send_command(address, command, repeat_count, delay_ms):
        sent_commands.append(
            {
                "address": address,
                "command": command,
                "repeat_count": repeat_count,
                "delay_ms": delay_ms,
            }
        )
        return True

    monkeypatch.setattr(projector, "send_command", mock_send_command)
    monkeypatch.setattr(PJ.time, "sleep", slept_durations.append)

    res = projector.power_off()
    assert res is True
    # Should send twice
    assert len(sent_commands) == 2
    assert all(c["address"] == 0x08 for c in sent_commands)
    assert all(c["command"] == 0x00 for c in sent_commands)
    assert all(c["repeat_count"] == 4 for c in sent_commands)
    assert all(c["delay_ms"] == 40 for c in sent_commands)

    # Verify that sleep(1.0) was called between sends
    assert slept_durations == [1.0]


def test_send_command_repeats():
    """Verify that send_command actually repeats writes and respects delay."""
    projector = PJ.ProjectorController()

    mock_file = mock_open()
    slept_durations = []
    
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_file), \
         patch("time.sleep", slept_durations.append):
        
        res = projector.send_command(0x08, 0x03, repeat_count=3, delay_ms=50)
        assert res is True

    # Should open file 3 times (once per repeat)
    assert mock_file.call_count == 3
    # Should sleep twice (delay_ms / 1000.0)
    assert slept_durations == [0.05, 0.05]


def test_projector_uses_configured_lirc_device(monkeypatch):
    """The device path comes from configuration, not a hard-coded literal."""
    import dataclasses

    from kodibot.config import CFG

    monkeypatch.setattr(
        PJ, "CFG", dataclasses.replace(CFG, projector_lirc_device="/dev/lirc9")
    )
    controller = PJ.ProjectorController()

    assert controller.device_path == "/dev/lirc9"


def test_main_on_returns_zero_on_success(monkeypatch):
    monkeypatch.setattr(PJ.projector, "power_on", lambda: True)
    assert PJ.main(["projector", "on"]) == 0


def test_main_on_returns_one_on_failure(monkeypatch):
    monkeypatch.setattr(PJ.projector, "power_on", lambda: False)
    assert PJ.main(["projector", "on"]) == 1


def test_main_off_calls_power_off(monkeypatch):
    calls = []
    monkeypatch.setattr(PJ.projector, "power_off", lambda: calls.append("off") or True)
    assert PJ.main(["projector", "off"]) == 0
    assert calls == ["off"]


def test_main_rejects_unknown_action():
    assert PJ.main(["projector", "sideways"]) == 2
    assert PJ.main(["projector"]) == 2
