"""Tests for Projector (Beamer) IR control logic."""

import os
import sys
import pytest

os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "test:token")
os.environ.setdefault("PIGPIO_HOST", "127.0.0.1")
os.environ.setdefault("PIGPIO_PORT", "8888")
os.environ.setdefault("PROJECTOR_GPIO", "17")
os.environ.setdefault("PROJECTOR_PROTOCOL", "NEC")
os.environ.setdefault("PROJECTOR_ADDRESS", "0x08")
os.environ.setdefault("PROJECTOR_POWER_ON_CODE", "0x03")
os.environ.setdefault("PROJECTOR_POWER_OFF_CODE", "0x00")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from kodibot.core import projector as PJ

# Dummy module variables for monkeypatching pigpio
pi = None
pulse = None
OUTPUT = None


class DummyPulse:
    """Mock pigpio.pulse."""

    def __init__(self, gpio_on, gpio_off, delay):
        self.gpio_on = gpio_on
        self.gpio_off = gpio_off
        self.delay = delay


class MockPi:
    """Mock pigpio.pi client."""

    def __init__(self, host=None, port=None):
        self.connected = True
        self.host = host
        self.port = port
        self.gpios = {}
        self.waves = {}
        self.next_wave_id = 0
        self.last_added_pulses = []
        self.sent_waves = []
        self.busy_count = 0

    def set_mode(self, gpio, mode):
        self.gpios[gpio] = mode

    def wave_clear(self):
        self.last_added_pulses = []

    def wave_add_generic(self, pulses):
        self.last_added_pulses.extend(pulses)

    def wave_create(self):
        wave_id = self.next_wave_id
        self.waves[wave_id] = list(self.last_added_pulses)
        self.next_wave_id += 1
        return wave_id

    def wave_delete(self, wave_id):
        self.waves.pop(wave_id, None)

    def wave_send_once(self, wave_id):
        self.sent_waves.append(wave_id)
        # Simulate transmission taking some time
        self.busy_count = 2

    def wave_tx_busy(self):
        if self.busy_count > 0:
            self.busy_count -= 1
            return True
        return False

    def stop(self):
        self.connected = False


@pytest.fixture
def mock_pigpio(monkeypatch):
    """Fixture that mocks the pigpio library."""
    monkeypatch.setattr(PJ, "pigpio", sys.modules[__name__])
    monkeypatch.setattr(sys.modules[__name__], "pi", MockPi)
    monkeypatch.setattr(sys.modules[__name__], "pulse", DummyPulse)
    monkeypatch.setattr(sys.modules[__name__], "OUTPUT", "OUTPUT")


def test_projector_config_loading():
    """Verify that configuration loaded from environment is mapped correctly."""
    from kodibot.config import CFG

    assert CFG.pigpio_host == "127.0.0.1"
    assert CFG.pigpio_port == 8888
    assert CFG.projector_gpio == 17
    assert CFG.projector_protocol == "NEC"
    assert CFG.projector_address == 0x08
    assert CFG.projector_power_on_code == 0x03
    assert CFG.projector_power_off_code == 0x00


def test_projector_connect(mock_pigpio):
    """Verify that connect() successfully initializes pigpio."""
    projector = PJ.ProjectorController()
    assert projector.pi is None

    connected = projector.connect()
    assert connected is True
    assert projector.pi is not None
    assert projector.pi.connected is True
    assert projector.pi.gpios[projector.gpio] == "OUTPUT"

    # Secondary connection should reuse existing
    original_pi = projector.pi
    assert projector.connect() is True
    assert projector.pi is original_pi

    # Disconnect should clear connection
    projector.disconnect()
    assert projector.pi is None


def test_nec_wave_structure(mock_pigpio):
    """Verify that generated NEC waveform has the expected structure and pulses."""
    projector = PJ.ProjectorController()
    assert projector.connect() is True

    wave_id = projector._build_nec_wave(0x08, 0x03)
    assert wave_id is not None
    assert wave_id in projector.pi.waves

    pulses = projector.pi.waves[wave_id]
    assert len(pulses) > 0

    # The first pulse should turn the GPIO pin ON (using 1 << gpio)
    gpio_mask = 1 << projector.gpio
    assert pulses[0].gpio_on == gpio_mask
    assert pulses[0].gpio_off == 0

    # Verify that there are carrier pulses (delay around 13us) and space pulses (delay 560us/1690us)
    carrier_delays = [p.delay for p in pulses if p.gpio_on != 0]
    space_delays = [p.delay for p in pulses if p.gpio_on == 0]

    assert all(d == 13 for d in carrier_delays)
    assert any(d == 4500 for d in space_delays)
    assert any(d == 1690 for d in space_delays)
    assert any(d == 560 for d in space_delays)
    assert any(d == 40000 for d in space_delays)


def test_power_on(mock_pigpio, monkeypatch):
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
    assert sent_commands[0]["repeat_count"] == 4
    assert sent_commands[0]["delay_ms"] == 40


def test_power_off(mock_pigpio, monkeypatch):
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


def test_send_command_execution(mock_pigpio):
    """Verify that send_command actually runs wave_send_once and cleans up."""
    projector = PJ.ProjectorController()
    assert projector.connect() is True

    # Send a command that repeats 2 times
    res = projector.send_command(0x08, 0x03, repeat_count=2, delay_ms=5)
    assert res is True

    # Check that wave was sent exactly twice
    assert len(projector.pi.sent_waves) == 2
    # Ensure that waves are deleted from pigpio memory to prevent leaks
    assert len(projector.pi.waves) == 0
