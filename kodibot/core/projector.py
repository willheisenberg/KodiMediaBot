"""Projector (Beamer) infrared controller module.

Controls power status of a projector (e.g. WiMiUS) using pigpio to send
precisely timed NEC IR waveforms modulated at a 38kHz carrier frequency.
"""

import logging
import os
import struct
import time
from kodibot.config import CFG

try:
    import pigpio
except ImportError:
    pigpio = None

log = logging.getLogger(__name__)


class ProjectorController:
    """Manages IR signal generation and transmission for the projector."""

    def __init__(self):
        self.pi = None
        self.gpio = CFG.projector_gpio
        self.use_lirc = False

    def connect(self) -> bool:
        """Connects to the native LIRC driver or fallback pigpiod daemon.

        Returns True if connected successfully, False otherwise.
        """
        # 1. Try to use native Linux kernel LIRC driver (/dev/lirc0) first.
        # This is the modern, hardware-agnostic standard (works on Pi 3, 4, 5 and LibreELEC).
        if os.path.exists("/dev/lirc0"):
            if not self.use_lirc:
                log.info("LIRC hardware device /dev/lirc0 detected. Using native kernel IR driver.")
                self.use_lirc = True
            return True

        self.use_lirc = False

        # 2. Fall back to pigpio daemon
        if pigpio is None:
            log.error("pigpio library is not installed/available on this system.")
            return False

        if self.pi and self.pi.connected:
            return True

        try:
            log.info(
                "Connecting to pigpio daemon at %s:%s...",
                CFG.pigpio_host,
                CFG.pigpio_port,
            )
            self.pi = pigpio.pi(CFG.pigpio_host, CFG.pigpio_port)
            
            # If failed to connect and targeting localhost, try to start pigpiod internally
            if not self.pi.connected and CFG.pigpio_host in ("127.0.0.1", "localhost"):
                log.info("pigpiod daemon not running. Attempting to start it internally inside the container...")
                try:
                    import subprocess
                    # Start pigpiod daemon which daemonizes itself by default
                    subprocess.Popen(["pigpiod"])
                    time.sleep(1.0)  # Wait for daemon to initialize
                    # Try connecting again
                    self.pi = pigpio.pi(CFG.pigpio_host, CFG.pigpio_port)
                except Exception as daemon_err:
                    log.error("Failed to start internal pigpiod daemon: %s", daemon_err)

            if not self.pi.connected:
                log.error(
                    "Failed to connect to pigpio daemon at %s:%s",
                    CFG.pigpio_host,
                    CFG.pigpio_port,
                )
                self.pi = None
                return False
                
            log.info("Successfully connected to pigpio.")
            self.pi.set_mode(self.gpio, pigpio.OUTPUT)
            return True
        except Exception as e:
            log.error("Exception connecting to pigpio: %s", e)
            self.pi = None
            return False

    def disconnect(self):
        """Disconnects from the pigpiod daemon."""
        if self.pi:
            try:
                self.pi.stop()
            except Exception:
                pass
            self.pi = None

    def _build_nec_wave(self, address: int, command: int):
        """Constructs the pigpio pulse wave for a single NEC transmission.

        Args:
            address: 8-bit address integer
            command: 8-bit command integer

        Returns:
            The created pigpio wave ID, or None if failed.
        """
        if not self.connect():
            return None

        carrier_hz = 38000
        period = 1000000.0 / carrier_hz
        on_us = int(period / 2.0)
        off_us = int(period - on_us)

        def carrier(duration_us):
            cycles = int(duration_us / period)
            p = []
            for _ in range(cycles):
                p.append(pigpio.pulse(1 << self.gpio, 0, on_us))
                p.append(pigpio.pulse(0, 1 << self.gpio, off_us))
            return p

        def space(duration_us):
            return [pigpio.pulse(0, 0, int(duration_us))]

        pulses = []

        # 1. Header (9ms pulse, 4.5ms space)
        pulses.extend(carrier(9000))
        pulses.extend(space(4500))

        # 2. 32-bit payload: address (8), ~address (8), command (8), ~command (8)
        # Transmitted LSB first.
        inv_address = (~address) & 0xFF
        inv_command = (~command) & 0xFF
        payload = address | (inv_address << 8) | (command << 16) | (inv_command << 24)

        for i in range(32):
            bit = (payload >> i) & 1
            # Each bit starts with a 560us pulse
            pulses.extend(carrier(560))
            if bit == 1:
                # Logical '1': 1690us space
                pulses.extend(space(1690))
            else:
                # Logical '0': 560us space
                pulses.extend(space(560))

        # 3. Stop bit (560us pulse)
        pulses.extend(carrier(560))

        # 4. Trailing space/gap (40ms)
        pulses.extend(space(40000))

        try:
            self.pi.wave_clear()
            self.pi.wave_add_generic(pulses)
            wave_id = self.pi.wave_create()
            return wave_id
        except Exception as e:
            log.error("Failed to build pigpio wave: %s", e)
            return None

    def send_command(
        self, address: int, command: int, repeat_count: int = 1, delay_ms: int = 40
    ) -> bool:
        """Sends an IR command multiple times with pauses using LIRC or pigpio.

        Args:
            address: 8-bit address
            command: 8-bit command
            repeat_count: number of times to repeat the transmission
            delay_ms: pause duration in milliseconds between repetitions

        Returns:
            True if all transmissions were triggered successfully, False otherwise.
        """
        if not self.connect():
            return False

        # 1. Native LIRC sending (Preferred)
        if self.use_lirc:
            try:
                # Build LIRC raw pulses (microsecond durations)
                # Starts with header
                pulses = [9000, 4500]

                # 32-bit payload: address (8), ~address (8), command (8), ~command (8)
                inv_address = (~address) & 0xFF
                inv_command = (~command) & 0xFF
                payload = address | (inv_address << 8) | (command << 16) | (inv_command << 24)

                for i in range(32):
                    bit = (payload >> i) & 1
                    pulses.append(560)  # Pulse
                    if bit == 1:
                        pulses.append(1690)  # Space
                    else:
                        pulses.append(560)  # Space

                # Stop bit
                pulses.append(560)

                # Convert to binary 32-bit unsigned integers
                binary_data = struct.pack(f"{len(pulses)}I", *pulses)

                log.info("Transmitting NEC command via native LIRC interface (/dev/lirc0)...")
                for i in range(repeat_count):
                    if i > 0:
                        time.sleep(delay_ms / 1000.0)
                    with open("/dev/lirc0", "wb") as f:
                        f.write(binary_data)
                return True
            except Exception as e:
                log.error("Exception in LIRC transmission: %s", e)
                return False

        # 2. pigpio sending (Fallback)
        try:
            wave_id = self._build_nec_wave(address, command)
            if wave_id is None or wave_id < 0:
                log.error("Failed to create NEC wave.")
                return False

            success = True
            for i in range(repeat_count):
                if i > 0:
                    time.sleep(delay_ms / 1000.0)

                log.debug(
                    "Transmitting NEC wave_id=%s (attempt %d/%d) on GPIO %s",
                    wave_id,
                    i + 1,
                    repeat_count,
                    self.gpio,
                )
                self.pi.wave_send_once(wave_id)
                # Wait until transmission completes
                while self.pi.wave_tx_busy():
                    time.sleep(0.005)

            # Cleanup wave from pigpio memory
            self.pi.wave_delete(wave_id)
            return success
        except Exception as e:
            log.error("Exception in pigpio send_command: %s", e)
            return False

    def power_on(self) -> bool:
        """Transmits the POWER_ON command.

        NEC protocol, Address 0x08, Command 0x03.
        Sends a rapid burst of 4 transmissions with 40ms gaps to ensure wake up.
        """
        log.info("Sending Projector POWER_ON...")
        return self.send_command(
            CFG.projector_address,
            CFG.projector_power_on_code,
            repeat_count=4,
            delay_ms=40,
        )

    def power_off(self) -> bool:
        """Transmits the POWER_OFF command twice (with a 1 second delay).

        NEC protocol, Address 0x08, Command 0x00.
        Each of the 2 transmissions internally sends a rapid 4-time burst.
        """
        log.info("Sending Projector POWER_OFF (Double Salve)...")
        # First 4-burst transmission
        ok1 = self.send_command(
            CFG.projector_address,
            CFG.projector_power_off_code,
            repeat_count=4,
            delay_ms=40,
        )
        # Wait 1 second
        time.sleep(1.0)
        # Second 4-burst transmission for confirmation
        ok2 = self.send_command(
            CFG.projector_address,
            CFG.projector_power_off_code,
            repeat_count=4,
            delay_ms=40,
        )
        return ok1 and ok2


# Singleton instance
projector = ProjectorController()
