"""Bridge to the Kodi addon ``visualization.spektra``.

The addon draws audio-reactive scenes (first scene: Eclipse) while music plays. It is
driven through Kodi's JSON-RPC and reports back asynchronously over Kodi's WebSocket as
``Other.spektra_status``; see the addon's ``docs/bot-api.md``.

Kodi shows one visualisation at a time, so Spektra and Party Video exclude each other:
turning Spektra on stops a running Party Video, and Party Video turns Spektra off
before it starts (see ``partyvideo``).

This module stays free of Telegram imports. The telegram layer registers a callback via
:func:`set_status_callback`, like ``partyvideo``.
"""

import logging
import threading

from kodibot.core import partyvideo
from kodibot.core.kodi_api import kodi_call

log = logging.getLogger(__name__)

ADDON_ID = "visualization.spektra"

# Last status reported by the addon. Written by the WebSocket thread, read by the
# telegram layer, so every access goes through _STATUS_LOCK.
LAST_STATUS = {}
_STATUS_LOCK = threading.Lock()
_STATUS_CALLBACK = None


def execute(*args):
    """Send one command to the addon. ``params`` must be a list (see ``partyvideo.execute``)."""
    return kodi_call("Addons.ExecuteAddon", {"addonid": ADDON_ID, "params": list(args)})


def on():
    return execute("action=on")


def off():
    return execute("action=off")


def request_status():
    """Ask the addon to emit a status event; the answer arrives over the WebSocket."""
    return execute("action=status")


def toggle():
    """Turn Spektra on or off, based on its last reported state."""
    if is_on():
        return off()
    if partyvideo.is_on():
        partyvideo.stop()
    return on()


def off_if_on():
    """Make room for another visual: turn Spektra off if it is on."""
    if is_on():
        off()


def set_status_callback(callback):
    global _STATUS_CALLBACK
    _STATUS_CALLBACK = callback


def handle_status_event(data):
    """Store an ``Other.spektra_status`` payload and notify the telegram layer."""
    if not isinstance(data, dict):
        return
    with _STATUS_LOCK:
        LAST_STATUS.clear()
        LAST_STATUS.update(data)
        status = dict(LAST_STATUS)
    if status.get("state") == "error":
        log.warning("spektra error=%s", status.get("error"))
    callback = _STATUS_CALLBACK
    if callback is None:
        return
    try:
        callback(status)
    except Exception as e:
        # The WebSocket loop must survive a broken UI callback.
        log.warning("spektra status callback failed: %s", e)


def status():
    with _STATUS_LOCK:
        return dict(LAST_STATUS)


def is_on():
    """True while Spektra is switched on. A rejected command reports ``state=error``,
    so the ``enabled`` setting is the reliable answer; ``state`` is the fallback."""
    current = status()
    settings = current.get("settings")
    if isinstance(settings, dict) and isinstance(settings.get("enabled"), bool):
        return settings["enabled"]
    return current.get("state") == "on"
