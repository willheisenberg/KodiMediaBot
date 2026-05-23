"""Projector (Beamer) infrared controller module.

Controls power status of a projector (e.g. WiMiUS) using the native Linux kernel
LIRC driver (/dev/lirc0) to send precisely timed NEC IR waveforms.
"""

import logging
import os
import struct
import time
from kodibot.config import CFG

log = logging.getLogger(__name__)


class ProjectorController:
    """Manages IR signal generation and transmission for the projector."""

    def __init__(self):
        self.device_path = "/dev/lirc0"

    def connect(self) -> bool:
        """Verifies that the native LIRC device is available.

        Returns True if available, False otherwise.
        """
        if not os.path.exists(self.device_path):
            log.error(
                "LIRC hardware device %s not found. Please ensure that "
                "'dtoverlay=gpio-ir-tx,gpio_pin=17' is enabled in /flash/config.txt "
                "and the container has privileged access.",
                self.device_path,
            )
            return False
        return True

    def send_command(
        self, address: int, command: int, repeat_count: int = 1, delay_ms: int = 40
    ) -> bool:
        """Sends an IR command multiple times with pauses using LIRC.

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

        try:
            # Build LIRC raw pulses (microsecond durations)
            # Starts with header (9ms pulse, 4.5ms space)
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

            # Stop bit (560us pulse)
            pulses.append(560)

            # Convert to binary 32-bit unsigned integers
            binary_data = struct.pack(f"{len(pulses)}I", *pulses)

            log.info("Transmitting NEC command via native LIRC interface (%s)...", self.device_path)
            for i in range(repeat_count):
                if i > 0:
                    time.sleep(delay_ms / 1000.0)
                with open(self.device_path, "wb") as f:
                    f.write(binary_data)
            return True
        except Exception as e:
            log.error("Exception in LIRC transmission: %s", e)
            return False

    def power_on(self) -> bool:
        """Transmits the POWER_ON command.

        NEC protocol, Address 0x08, Command 0x03.
        Sends a rapid burst of repeated transmissions to wake up from standby.
        """
        log.info("Sending Projector POWER_ON...")
        return self.send_command(
            CFG.projector_address,
            CFG.projector_power_on_code,
            repeat_count=CFG.projector_power_on_repeats,
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
