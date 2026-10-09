"""Subtitle selection that swaps YouTube's rolling captions for a clean copy."""

import logging
import os
import time

from kodibot.config import CFG
from kodibot.core import kodi_api, kodi_library, kodi_ws, opensubtitles, youtube_subs
from kodibot.core.kodi_metadata import extract_youtube_id
from kodibot.telegram import state as S

log = logging.getLogger(__name__)


def _stream_indexes(av_state):
    return {s.get("index") for s in (av_state.get("subtitles") or [])}


def _clean_youtube_track(stream):
    """Index of an overlap-free copy of ``stream``, attached if need be.

    None whenever the copy does not apply or could not be made; the caller
    then falls back to the track as Kodi has it.
    """
    info = kodi_library.now_playing_media_info()
    if not info or not kodi_ws.is_youtube_playback_file(info["file"]):
        return None
    cleaned = S.YOUTUBE_CLEAN_SUBTITLES.setdefault(info["file"], {})
    if stream.get("index") in cleaned.values():
        # Already one of our copies.
        return None
    language = opensubtitles.normalize_language(stream.get("language"))
    if language in cleaned:
        return cleaned[language]
    video_id = extract_youtube_id(info["file"]) or kodi_api.LAST_WS_YT_ID
    content = youtube_subs.fetch_clean_auto_captions(video_id, language)
    if not content:
        return None

    name = f"{video_id}.{language}.srt"
    local_path = os.path.join(CFG.upload_dir, "subs", name)
    try:
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as fh:
            fh.write(content)
    except OSError as e:
        log.warning("YouTube caption write failed: %s", e)
        return None

    before = _stream_indexes(kodi_api.get_av_settings())
    if not kodi_api.add_subtitle_file(os.path.join(CFG.kodi_upload_dir, "subs", name)):
        return None
    # Give Kodi a moment to register the new stream before re-reading indexes.
    time.sleep(0.5)
    added = sorted(i for i in _stream_indexes(kodi_api.get_av_settings()) - before if i is not None)
    if not added:
        return None
    cleaned[language] = added[-1]
    return added[-1]


def select_subtitle(stream):
    """Turn one subtitle track on. Blocking; run it in a thread."""
    try:
        index = _clean_youtube_track(stream)
    except Exception as e:
        log.warning("YouTube caption cleanup failed: %s", e)
        index = None
    if index is None:
        index = stream.get("index")
    return kodi_api.set_subtitle_stream(index)
