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

# Must match the add-on's RTP port setting (default 23433). Add-on versions
# that stream 44.1 kHz play this URL directly.
STREAM_URL = "rtp://127.0.0.1:23433"
# Versions that stream 48 kHz play the RTP stream through this SDP file in the
# add-on's profile folder, e.g.
# /storage/.kodi/userdata/addon_data/service.soloist/soloist.sdp
STREAM_SDP_SUFFIX = "/addon_data/service.soloist/soloist.sdp"
ADDON_ID = "service.soloist"
# Sent by the add-on via NotifyAll right before it starts the stream, so the
# bot hears about the takeover before Kodi reports the old item stopped.
TAKEOVER_EVENT = "Other.soloist_takeover"
# Sent by the bot via JSONRPC.NotifyAll when it stops playback or starts its
# own: the add-on then releases the Spotify Connect device, so the app moves
# playback back to the phone instead of keeping the box selected (paused).
RELEASE_MESSAGE = "soloist_release"
# Info label the add-on sets to the Spotify account the box is logged in as.
USER_LABEL = "Window(Home).Property(soloist.user)"


def is_stream(file_url) -> bool:
    return bool(file_url) and (
        file_url.rstrip("/") == STREAM_URL or file_url.endswith(STREAM_SDP_SUFFIX)
    )


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


def parse_user_names(raw) -> dict:
    """SPOTIFY_USER_NAMES ("username=Name,other=Name") as a dict."""
    names = {}
    for pair in (raw or "").split(","):
        user, sep, name = pair.partition("=")
        if sep and user.strip() and name.strip():
            names[user.strip()] = name.strip()
    return names


def user_name(user, names) -> str:
    """Name to show for a Spotify account: its mapped name, else the username."""
    user = (user or "").strip()
    return names.get(user, user)


def display_name(item, user="") -> str:
    """Panel text for the Spotify stream; `user` is whose account plays."""
    source = f"Spotify ({user})" if user else "Spotify"
    artists, title = artist_title(item)
    if title and artists:
        return f"{source}: {title} – {artists}"
    if title:
        return f"{source}: {title}"
    return source
