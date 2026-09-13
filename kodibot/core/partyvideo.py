"""Bridge to the Kodi addon ``visualization.partyvideo``.

The addon shows a muted video in an endless loop while music plays. It is driven
through Kodi's JSON-RPC and reports back asynchronously over Kodi's WebSocket as
``Other.partyvideo_status``; see the addon's ``docs/bot-api.md``.

This module stays free of Telegram imports. The telegram layer registers a
callback via :func:`set_status_callback`, mirroring ``queue_state.set_ui_callbacks``.
"""

import logging
import re
import threading
from urllib.parse import urlparse

from kodibot.core.kodi_api import kodi_call

log = logging.getLogger(__name__)

ADDON_ID = "visualization.partyvideo"

# Last status reported by the addon. Written by the WebSocket thread, read by the
# telegram layer, so every access goes through _STATUS_LOCK.
LAST_STATUS = {}
_STATUS_LOCK = threading.Lock()
_STATUS_CALLBACK = None

# States in which a video is selected and the addon holds its source file.
ACTIVE_STATES = ("installing_tools", "downloading", "playing")

# Every state the addon documents; anything else is reported as unknown.
KNOWN_STATES = ("idle", "installing_tools", "downloading", "playing", "error")

_YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "music.youtube.com")
_YOUTUBE_PATHS = re.compile(r"^/(watch|shorts/|live/|embed/|v/|[A-Za-z0-9_-]{6,})")


def is_youtube_url(url):
    """True for a plain http(s) YouTube link, without credentials or a port."""
    if not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password or ":" in parsed.netloc:
        return False
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host not in _YOUTUBE_HOSTS:
        return False
    return bool(_YOUTUBE_PATHS.match(parsed.path or "/"))


def execute(*args):
    """Send one command to the addon. ``params`` must be a list: Kodi joins a dict
    with commas and no escaping, which would tear a URL apart."""
    return kodi_call("Addons.ExecuteAddon", {"addonid": ADDON_ID, "params": list(args)})


def play_url(url):
    url = (url or "").strip()
    if not url:
        return None
    return execute("action=play", f"url={url}")


def play_path(path):
    path = (path or "").strip()
    if not path:
        return None
    return execute("action=play", f"path={path}")


def stop():
    return execute("action=stop")


def toggle():
    return execute("action=toggle")


def request_status():
    """Ask the addon to emit a status event; the answer arrives over the WebSocket."""
    return execute("action=status")


def set_status_callback(callback):
    global _STATUS_CALLBACK
    _STATUS_CALLBACK = callback


def handle_status_event(data):
    """Store an ``Other.partyvideo_status`` payload and notify the telegram layer."""
    if not isinstance(data, dict):
        return
    with _STATUS_LOCK:
        LAST_STATUS.clear()
        LAST_STATUS.update(data)
        status = dict(LAST_STATUS)
    callback = _STATUS_CALLBACK
    if callback is None:
        return
    try:
        callback(status)
    except Exception as e:
        # The WebSocket loop must survive a broken UI callback.
        log.warning("partyvideo status callback failed: %s", e)


def status():
    with _STATUS_LOCK:
        return dict(LAST_STATUS)


def active_source():
    """Path or URL the addon currently holds, or "" when nothing is selected.

    The bot checks this before deleting an uploaded file so it never pulls the
    source out from under a running visual.
    """
    current = status()
    if current.get("state") not in ACTIVE_STATES:
        return ""
    return current.get("source") or ""


def is_on():
    """True while a video is selected, regardless of whether it is on screen."""
    return status().get("state") in ACTIVE_STATES
