"""Tell YouTube live streams apart from ordinary YouTube videos.

Kodi reports a YouTube live stream as a plain item with live false: 30 s
long over HLS (YouTube's window), 4 h over DASH (the timeshift buffer the
add-on asks inputstream.adaptive for). Started through a playlist its file is
the manifest URL, which names the live broadcast; opened directly it stays the
plugin URL, which says nothing. So yt-dlp is asked once per video, in the
background, and the answer is cached. Only items of those lengths are asked
about.

This module stays free of Telegram imports.
"""

import logging
import re
import threading

from yt_dlp import YoutubeDL

from kodibot.core import kodi_api

log = logging.getLogger(__name__)

TIMEOUT = 10.0
# Kodi reports YouTube's live window (HLS) as the item's length ...
MAX_WINDOW_SEC = 60
# ... or the add-on's timeshift buffer limit (DASH).
DASH_BUFFER_SEC = 4 * 60 * 60

_MANIFEST_RE = re.compile(r"/id/([A-Za-z0-9_-]{11})(?:\.\d+)?/(?:[^?]*/)?source/yt_live_broadcast/")

_LOCK = threading.Lock()
_LIVE = {}  # video id -> bool
_PENDING = set()


def live_video_id(file_url, total_sec):
    """Video id when Kodi plays a YouTube live stream, else None.

    An unknown video starts a background lookup and counts as not live until
    the answer is in; the panel refreshes every few seconds anyway.
    """
    m = _MANIFEST_RE.search(file_url or "")
    if m:
        with _LOCK:
            _LIVE[m.group(1)] = True
        return m.group(1)
    if not (file_url or "").startswith("plugin://plugin.video.youtube/"):
        return None
    if total_sec is None or (total_sec > MAX_WINDOW_SEC and total_sec != DASH_BUFFER_SEC):
        return None
    video_id = kodi_api.extract_youtube_id(file_url)
    if not video_id:
        return None
    with _LOCK:
        known = _LIVE.get(video_id)
        start = known is None and video_id not in _PENDING
        if start:
            _PENDING.add(video_id)
    if start:
        _start_lookup(video_id)
    return video_id if known else None


def is_dash_buffer(total_sec):
    """Whether Kodi's length is the add-on's DASH buffer, not YouTube's window."""
    return total_sec == DASH_BUFFER_SEC


def _start_lookup(video_id):
    threading.Thread(target=_lookup, args=(video_id,), daemon=True).start()


def _lookup(video_id):
    opts = {"quiet": True, "skip_download": True, "noplaylist": True, "socket_timeout": TIMEOUT}
    live = None
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False, process=False,
            )
        live = (info or {}).get("live_status") == "is_live" or bool((info or {}).get("is_live"))
    except Exception as e:
        log.warning("YouTube live lookup failed vid=%s err=%s", video_id, e)
    with _LOCK:
        _PENDING.discard(video_id)
        if live is not None:
            _LIVE[video_id] = live
