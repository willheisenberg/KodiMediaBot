"""Spotify Connect through the service.soloist add-on.

The add-on plays Spotify into Kodi as an RTP stream.  The two sides simply
replace each other:

* Spotify taking over ends whatever the bot was playing.  Nothing is parked
  and nothing comes back when Spotify stops.
* Anything the bot starts itself, and its stop button, releases the Spotify
  Connect device, so Spotify cannot start again underneath it.

This module only knows the add-on's stream and messages; queue_state acts on
them.  It is free of Kodi and Telegram imports.
"""

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
