"""Runs the configured display power commands.

The projector/TV power buttons execute an operator-supplied shell command from
the environment, so infrared, CEC, HTTP requests and local scripts are all the
same thing from the bot's point of view.

``shell=True`` is required so operators can write pipes, ``&&`` and environment
variable expansion such as ``$TV_HOST``.  The command string comes from the
operator-controlled ``.env`` and never incorporates input from Telegram users,
so there is no injection path from the bot's users.  This mirrors
``run_cec_power()`` in ``kodi_hifi.py``.
"""

import logging
import subprocess

from kodibot.config import CFG

log = logging.getLogger(__name__)


def run_display_power(on: bool) -> bool:
    """Runs the configured power-on or power-off command.

    Returns True when the command exits zero.  Returns False on a non-zero
    exit, a timeout, an empty command, or an unexpected error.
    """
    action = "on" if on else "off"
    cmd = CFG.display_power_on_cmd if on else CFG.display_power_off_cmd
    if not cmd.strip():
        log.warning("No display power %s command configured", action)
        return False
    try:
        res = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=CFG.display_command_timeout,
        )
    except subprocess.TimeoutExpired:
        log.warning(
            "Display power %s command timed out after %ss: %s",
            action,
            CFG.display_command_timeout,
            cmd,
        )
        return False
    except Exception as e:
        log.warning("Display power %s command error: %s", action, e)
        return False
    if res.returncode != 0:
        log.warning(
            "Display power %s command failed: rc=%d stderr=%s",
            action,
            res.returncode,
            (res.stderr or "").strip(),
        )
        return False
    return True
