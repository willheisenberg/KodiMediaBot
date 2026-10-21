"""Subtitle selection that swaps YouTube's rolling captions for a clean copy."""

import logging
import os
import re
import time

from kodibot.config import CFG
from kodibot.core import kodi_api, kodi_library, kodi_ws, opensubtitles, youtube_subs
from kodibot.core.kodi_metadata import extract_youtube_id

log = logging.getLogger(__name__)

# Kodi names an attached file after its stem ("ytclean2 (External)"), cutting
# the stem at " .-" and swallowing the language code.  A video id may hold
# "-", so the copies carry this marker and a running number instead.
_COPY_NAME_RE = re.compile(r"\bytclean(\d+)\b")
# A copy Kodi cannot open is replaced by a new one -- this often per playback.
MAX_COPIES = 3
KODI_REGISTER_SECONDS = 0.5
# Kodi keeps reporting a track it failed to open as current for about a second.
KODI_OPEN_SECONDS = 2.0


def _language(stream):
    return opensubtitles.normalize_language(stream.get("language"))


def _copy_number(stream):
    match = _COPY_NAME_RE.search(stream.get("name") or "")
    return int(match.group(1)) if match else None


def _copies():
    """The attached clean copies as Kodi lists them right now, newest first.

    Looked up on every pick instead of remembered: Kodi renumbers the tracks
    whenever the stream changes quality, moving attached files to the front.
    """
    streams = kodi_api.get_av_settings().get("subtitles") or []
    copies = [s for s in streams if _copy_number(s) is not None]
    return sorted(copies, key=_copy_number, reverse=True)


def _copy_path(video_id, language, number):
    # A folder of its own per copy: Kodi has never listed it, so no cached
    # listing can hide the file.
    return os.path.join("subs", f"{video_id}.{number}", f"ytclean{number}.{language}.srt")


def _attach_copy(video_id, language, number):
    """Fetch the clean captions and attach them; the new track, or None."""
    content = youtube_subs.fetch_clean_auto_captions(video_id, language)
    if not content:
        return None

    path = _copy_path(video_id, language, number)
    local_path = os.path.join(CFG.upload_dir, path)
    try:
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as fh:
            fh.write(content)
    except OSError as e:
        log.warning("YouTube caption write failed: %s", e)
        return None

    if not kodi_api.add_subtitle_file(os.path.join(CFG.kodi_upload_dir, path)):
        return None
    time.sleep(KODI_REGISTER_SECONDS)
    return next((c for c in _copies() if _copy_number(c) == number), None)


def _show_copy(copy):
    """Turn a clean copy on; False when Kodi could not open it."""
    if not kodi_api.set_subtitle_stream(copy.get("index")):
        return False
    # Kodi answers OK even when it fails to open the file, and is then left
    # without any subtitle at all.
    time.sleep(KODI_OPEN_SECONDS)
    current = kodi_api.get_av_settings().get("currentsubtitle") or {}
    return _copy_number(current) == _copy_number(copy)


def _select_clean_youtube_track(stream):
    """Show the overlap-free copy of ``stream``; None when there is none.

    False means a copy exists but Kodi would not show it.
    """
    info = kodi_library.now_playing_media_info()
    if not info or not kodi_ws.is_youtube_playback_file(info["file"]):
        return None
    video_id = extract_youtube_id(info["file"]) or kodi_api.LAST_WS_YT_ID
    language = _language(stream)
    if not video_id or not language:
        return None

    copies = _copies()
    copy = next((c for c in copies if _language(c) == language), None)
    if copy:
        # The bot empties its upload folder on start, a copy from before then
        # is gone from disk while Kodi still lists it.
        local_path = os.path.join(CFG.upload_dir, _copy_path(video_id, language, _copy_number(copy)))
        if os.path.isfile(local_path) and _show_copy(copy):
            return True
        log.warning("Kodi cannot open clean captions vid=%s track=%s", video_id, copy.get("name"))

    number = max((_copy_number(c) for c in copies), default=0) + 1
    if number > MAX_COPIES:
        return False
    attached = _attach_copy(video_id, language, number)
    if not attached:
        return False if copy else None
    if _show_copy(attached):
        return True
    log.warning("Kodi cannot open clean captions vid=%s track=%s", video_id, attached.get("name"))
    return False


def _youtube_track(stream):
    """``stream`` itself, or YouTube's own track when ``stream`` is a copy."""
    if _copy_number(stream) is None:
        return stream
    for other in kodi_api.get_av_settings().get("subtitles") or []:
        if _copy_number(other) is None and _language(other) == _language(stream):
            return other
    return stream


def select_subtitle(stream):
    """Turn one subtitle track on. Blocking; run it in a thread."""
    try:
        shown = _select_clean_youtube_track(stream)
    except Exception as e:
        log.warning("YouTube caption cleanup failed: %s", e)
        shown = None
    if shown:
        return True
    if shown is False:
        # Rolling captions beat none at all.
        stream = _youtube_track(stream)
    return kodi_api.set_subtitle_stream(stream.get("index"))
