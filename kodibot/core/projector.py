"""Projector (Beamer) infrared controller module.

Controls power status of a projector (e.g. WiMiUS) using the native Linux kernel
LIRC driver (/dev/lirc0) to send precisely timed NEC IR waveforms.
"""

import logging
import os
import struct
import sys
import time
from kodibot.config import CFG

log = logging.getLogger(__name__)

# A held NEC key is one full frame followed by a short repeat code every 108 ms.
NEC_REPEAT_PERIOD_S = 0.108
NEC_FRAME_S = 0.068
NEC_REPEAT_CODE_S = 0.012


class ProjectorController:
    """Manages IR signal generation and transmission for the projector."""

    def __init__(self):
        self.device_path = CFG.projector_lirc_device

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

    @staticmethod
    def _nec_frame(address: int, command: int) -> bytes:
        """Builds one NEC frame as LIRC raw pulses (microsecond durations)."""
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
        return struct.pack(f"{len(pulses)}I", *pulses)

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
            binary_data = self._nec_frame(address, command)

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

    def hold_command(self, address: int, command: int, seconds: float) -> bool:
        """Sends an IR command as a held key (long press) using LIRC.

        One full frame, then NEC repeat codes for the given duration.  Each
        part is a separate write because the kernel rejects a single
        transmission longer than 500 ms.

        Returns:
            True if all transmissions were triggered successfully, False otherwise.
        """
        if not self.connect():
            return False

        try:
            frame = self._nec_frame(address, command)
            repeat_code = struct.pack("3I", 9000, 2250, 560)
            repeats = int(seconds / NEC_REPEAT_PERIOD_S)

            log.info(
                "Holding NEC command for %.1fs via native LIRC interface (%s)...",
                seconds,
                self.device_path,
            )
            with open(self.device_path, "wb") as f:
                f.write(frame)
            time.sleep(NEC_REPEAT_PERIOD_S - NEC_FRAME_S)
            for _ in range(repeats):
                with open(self.device_path, "wb") as f:
                    f.write(repeat_code)
                time.sleep(NEC_REPEAT_PERIOD_S - NEC_REPEAT_CODE_S)
            return True
        except Exception as e:
            log.error("Exception in LIRC transmission: %s", e)
            return False

    def power_on(self) -> bool:
        """Transmits the POWER_ON command.

        NEC protocol, Address 0x08, Command 0x03.
        Sends a rapid burst of repeated transmissions to wake up from standby,
        then holds the key: after a long standby the projector ignores the
        burst and only wakes on a long press.
        """
        log.info("Sending Projector POWER_ON...")
        ok = self.send_command(
            CFG.projector_address,
            CFG.projector_power_on_code,
            repeat_count=CFG.projector_power_on_repeats,
            delay_ms=40,
        )
        if ok and CFG.projector_power_on_hold_seconds > 0:
            ok = self.hold_command(
                CFG.projector_address,
                CFG.projector_power_on_code,
                CFG.projector_power_on_hold_seconds,
            )
        return ok

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


def main(argv: list[str]) -> int:
    """Entry point so IR can be driven as a shell command.

    This is what the default DISPLAY_POWER_ON_CMD / DISPLAY_POWER_OFF_CMD
    invoke, which keeps infrared on the same footing as CEC, HTTP or scripts.
    """
    action = argv[1] if len(argv) > 1 else ""
    if action == "on":
        return 0 if projector.power_on() else 1
    if action == "off":
        return 0 if projector.power_off() else 1
    print("usage: python -m kodibot.core.projector on|off", file=sys.stderr)
    return 2


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(main(sys.argv))
