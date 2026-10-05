"""Handover between the bot and Spotify Connect (the service.soloist add-on).

The add-on plays Spotify into Kodi as an RTP stream.  Either side may
interrupt the other:

* Spotify taking over parks the bot's queue (or radio) instead of dropping
  it: the queue keeps its position and the radio station is remembered.
* The bot takes back over when the Spotify stream stops, or when Spotify
  stays paused for PAUSE_TAKEBACK_SEC.  The queue then resumes where it was
  interrupted, a parked radio station is restarted.
* Anything the bot starts itself ends the handover and releases the Spotify
  Connect device, so Spotify cannot start again underneath it.

This module only holds the handover state; queue_state drives it.  It is
free of Kodi and Telegram imports so the timing logic is testable alone.
"""

import threading
import time

# Must match the add-on's RTP port setting (default 23433).
STREAM_URL = "rtp://127.0.0.1:23433"
ADDON_ID = "service.soloist"
# Sent by the add-on via NotifyAll right before it starts the stream, so the
# bot hears about the takeover before Kodi reports the old item stopped.
TAKEOVER_EVENT = "Other.soloist_takeover"
# Sent by the bot via JSONRPC.NotifyAll when it stops playback or starts its
# own: the add-on then releases the Spotify Connect device, so the app moves
# playback back to the phone instead of keeping the box selected (paused).
RELEASE_MESSAGE = "soloist_release"

PAUSE_TAKEBACK_SEC = 30.0
# Switching items, Kodi reports the outgoing one stopped before the Spotify
# stream starts; that gap must not read as "Spotify ended".
SETTLE_SEC = 5.0

_LOCK = threading.Lock()
_active = False
_since = 0.0
_paused_since = None
_radio = None


def is_stream(file_url) -> bool:
    return bool(file_url) and file_url.rstrip("/") == STREAM_URL


def is_stream_item(item) -> bool:
    return is_stream((item or {}).get("file"))


def is_takeover_event(method, params) -> bool:
    return method == TAKEOVER_EVENT and (params or {}).get("sender") == ADDON_ID


def artist_title(item):
    """(artists, title) of the Spotify stream, from the info tag the add-on sets."""
    item = item or {}
    artist = item.get("artist") or []
    if isinstance(artist, str):
        artist = [artist]
    return ", ".join(a for a in artist if a), item.get("title") or ""


def display_name(item) -> str:
    """Panel text for the Spotify stream."""
    artists, title = artist_title(item)
    if title and artists:
        return f"Spotify: {title} – {artists}"
    if title:
        return f"Spotify: {title}"
    return "Spotify"


def begin(radio=None, now=None) -> bool:
    """Start a handover; returns False if one is already running.

    `radio` is the station that was playing ({"url", "title"}) or None.  A
    second call (the play event after the takeover notification) keeps the
    first snapshot.
    """
    global _active, _since, _paused_since, _radio
    with _LOCK:
        if _active:
            return False
        _active = True
        _since = time.monotonic() if now is None else now
        _paused_since = None
        _radio = dict(radio) if radio else None
        return True


def is_active() -> bool:
    with _LOCK:
        return _active


def parked_radio():
    with _LOCK:
        return dict(_radio) if _active and _radio else None


def set_paused(paused: bool, now=None):
    global _paused_since
    with _LOCK:
        if not _active:
            return
        if not paused:
            _paused_since = None
        elif _paused_since is None:
            _paused_since = time.monotonic() if now is None else now


def settled(now=None) -> bool:
    with _LOCK:
        now = time.monotonic() if now is None else now
        return _active and now - _since >= SETTLE_SEC


def paused_too_long(now=None) -> bool:
    with _LOCK:
        if not _active or _paused_since is None:
            return False
        now = time.monotonic() if now is None else now
        return now - _paused_since >= PAUSE_TAKEBACK_SEC


def end():
    """Finish the handover; returns the parked radio station, if any."""
    global _active, _paused_since, _radio
    with _LOCK:
        radio = _radio if _active else None
        _active = False
        _paused_since = None
        _radio = None
        return radio
