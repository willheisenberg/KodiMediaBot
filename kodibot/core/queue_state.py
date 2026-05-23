import logging
import re
import subprocess
import threading
import time
from urllib.parse import unquote

import asyncio
import requests
from yt_dlp import YoutubeDL

from kodibot.core import kodi_api
from kodibot.config import CFG
from kodibot.telegram import media

log = logging.getLogger(__name__)

# Queue and playback state
QUEUE = []
CURRENT_INDEX = None
DISPLAY_INDEX = None
NEXT_INDEX = 0
LOCK = threading.Lock()
AUTOPLAY_ENABLED = True
REPEAT_MODE = "off"
EXTERNAL_PLAYBACK = False
BOT_EXPECTING_WS = 0

# Radio unexpected stop and reconnect tracking
LAST_PLAYED_RADIO = None
EXPECTED_STOP = False
ON_UNEXPECTED_RADIO_STOP = None
CANCEL_RECONNECT_CB = None

# Timestamp of the last bot-initiated play_index() call.  Used by the panel to
# show queue track info optimistically for a few seconds after a track change,
# even when BOT_EXPECTING_WS has already dropped to 0 (happens quickly for
# SoundCloud because the plugin fires WS events faster than YouTube's plugin).
PLAY_INDEX_TS = 0.0
PLAY_INDEX_OPTIMISTIC_WINDOW = 8.0  # seconds

LAST_PROGRESS_TS = 0.0
LAST_PROGRESS_TIME = None
LAST_PROGRESS_TOTAL = None
LAST_PROGRESS_INDEX = None

RESUME_ATTEMPTS = {}
RESUME_MAX_ATTEMPTS = 8
RESUME_MIN_REMAINING_SEC = 10
RESUME_SEEK_WAIT_SEC = 20
RESUME_STALE_PROGRESS_SEC = 12

LIST_DIRTY = False

_SCHEDULE_NOW_PLAYING_REFRESH = None
HTTP = requests.Session()
SC_DISPLAY_RE = re.compile(r"^https?://(www\.)?soundcloud\.com/([^/]+)/([^/?#]+)")
SC_TRACK_RE = re.compile(r"^https?://(www\.)?soundcloud\.com/[^/]+/[^/?#]+")
SC_SET_RE = re.compile(r"^https?://(www\.)?soundcloud\.com/[^/]+/sets/[^/?#]+")
SC_BASE_RE = re.compile(r"^https?://(www\.)?soundcloud\.com/")
SC_HTML_RE = re.compile(r'https?://soundcloud\.com/[^\s"\'<>]+')
YT_TITLE_CACHE = {}
YT_TITLE_CACHE_TTL = 3600.0
YT_TITLE_CACHE_LOCK = threading.Lock()
SC_PLUGIN_START_TIMEOUT_S = 4.0
SC_PLUGIN_START_POLL_S = 0.25


def get_cached_youtube_title(vid: str):
    now = time.time()
    with YT_TITLE_CACHE_LOCK:
        hit = YT_TITLE_CACHE.get(vid)
        if not hit:
            return None
        title, ts = hit
        if now - ts > YT_TITLE_CACHE_TTL:
            YT_TITLE_CACHE.pop(vid, None)
            return None
        return title


def cache_youtube_title(vid: str, title: str):
    if not vid or not title:
        return
    with YT_TITLE_CACHE_LOCK:
        YT_TITLE_CACHE[vid] = (title, time.time())


def set_ui_callbacks(schedule_now_playing_refresh, on_unexpected_radio_stop=None, cancel_reconnect_cb=None):
    global _SCHEDULE_NOW_PLAYING_REFRESH, ON_UNEXPECTED_RADIO_STOP, CANCEL_RECONNECT_CB
    _SCHEDULE_NOW_PLAYING_REFRESH = schedule_now_playing_refresh
    if on_unexpected_radio_stop is not None:
        ON_UNEXPECTED_RADIO_STOP = on_unexpected_radio_stop
    if cancel_reconnect_cb is not None:
        CANCEL_RECONNECT_CB = cancel_reconnect_cb


def set_last_played_radio(url, title):
    global LAST_PLAYED_RADIO, EXPECTED_STOP
    with LOCK:
        LAST_PLAYED_RADIO = {"url": url, "title": title}
        EXPECTED_STOP = False
    log.info("Radio state registered: '%s' (%s)", title, url)


def clear_radio_reconnect_state():
    global LAST_PLAYED_RADIO, EXPECTED_STOP
    with LOCK:
        LAST_PLAYED_RADIO = None
        EXPECTED_STOP = True
    log.info("Radio reconnect state cleared")


def schedule_now_playing_refresh():
    if _SCHEDULE_NOW_PLAYING_REFRESH:
        _SCHEDULE_NOW_PLAYING_REFRESH()


# ── Thread-safe helpers for BOT_EXPECTING_WS ─────────────────────────
def set_expecting_ws(n: int):
    """Set BOT_EXPECTING_WS under LOCK."""
    global BOT_EXPECTING_WS
    with LOCK:
        BOT_EXPECTING_WS = n


def decrement_expecting_ws() -> int:
    """Decrement BOT_EXPECTING_WS under LOCK, return new value."""
    global BOT_EXPECTING_WS
    with LOCK:
        if BOT_EXPECTING_WS > 0:
            BOT_EXPECTING_WS -= 1
        return BOT_EXPECTING_WS


def get_expecting_ws() -> int:
    """Read BOT_EXPECTING_WS under LOCK."""
    with LOCK:
        return BOT_EXPECTING_WS


# ── WS callback handlers (registered with kodi_api) ────────────────
def _handle_ws_play(*, item, item_params):
    """Called from kodi_api WS listener on Player.OnPlay/OnAVStart."""
    global BOT_EXPECTING_WS
    # decrement_expecting_ws returns the value AFTER decrement.
    # If before decrement it was > 0, this play was bot-initiated.
    with LOCK:
        was_expecting = BOT_EXPECTING_WS > 0
        if was_expecting:
            BOT_EXPECTING_WS -= 1
    if was_expecting:
        log.debug("WS play expected, remaining=%d", BOT_EXPECTING_WS)
        return
    # External play – check mismatch
    with LOCK:
        if DISPLAY_INDEX is not None and 0 <= DISPLAY_INDEX < len(QUEUE):
            qitem = QUEUE[DISPLAY_INDEX]
        else:
            qitem = None
    if not kodi_api.kodi_item_matches_queue(item, qitem):
        log.info(
            "WS mismatch clear_bot_playback_state "
            "item_file=%s item_title=%s q_url=%s q_title=%s q_link=%s",
            (item or {}).get('file'), (item or {}).get('title'),
            (qitem or {}).get('url'), (qitem or {}).get('title'),
            (qitem or {}).get('link'),
        )
        clear_bot_playback_state()
        schedule_now_playing_refresh()


def _handle_ws_pause():
    schedule_now_playing_refresh()


def _handle_ws_resume():
    schedule_now_playing_refresh()


def _handle_ws_stop(item_params=None, player_params=None):
    global EXPECTED_STOP, LAST_PLAYED_RADIO
    schedule_now_playing_refresh()
    
    player_id = (player_params or {}).get("playerid")
    stopped_type = (item_params or {}).get("type")
    
    is_audio_stop = True
    if player_id is not None and player_id != 0:
        is_audio_stop = False
    elif stopped_type == "picture":
        is_audio_stop = False
        
    with LOCK:
        unexpected = not EXPECTED_STOP and is_audio_stop
        radio_info = LAST_PLAYED_RADIO
        if is_audio_stop:
            EXPECTED_STOP = True
        
    if unexpected and radio_info and ON_UNEXPECTED_RADIO_STOP:
        log.info("Unexpected stop detected for radio: %s. Triggering reconnect...", radio_info)
        try:
            loop = asyncio.get_running_loop()
            import inspect
            if inspect.iscoroutinefunction(ON_UNEXPECTED_RADIO_STOP):
                loop.create_task(ON_UNEXPECTED_RADIO_STOP(radio_info["url"], radio_info["title"]))
            else:
                ON_UNEXPECTED_RADIO_STOP(radio_info["url"], radio_info["title"])
        except RuntimeError:
            log.warning("No running asyncio event loop, cannot trigger unexpected stop callback.")


def _handle_ws_playback_refresh():
    schedule_playback_refresh()


def register_ws_callbacks():
    """Register our event handlers with kodi_api's WS callback system."""
    kodi_api.set_ws_handlers(
        on_play=_handle_ws_play,
        on_pause=_handle_ws_pause,
        on_resume=_handle_ws_resume,
        on_stop=_handle_ws_stop,
        on_playback_refresh=_handle_ws_playback_refresh,
    )


# Mark the playlist display as needing refresh.
def mark_list_dirty():
    global LIST_DIRTY
    LIST_DIRTY = True


# Clear bot playback state without stopping Kodi playback.
def clear_bot_playback_state():
    global AUTOPLAY_ENABLED, CURRENT_INDEX, DISPLAY_INDEX, EXTERNAL_PLAYBACK
    with LOCK:
        AUTOPLAY_ENABLED = False
        CURRENT_INDEX = None
        DISPLAY_INDEX = None
        EXTERNAL_PLAYBACK = True
        RESUME_ATTEMPTS.clear()
    mark_list_dirty()


# Refresh list + now-playing after playback state changes.
def schedule_playback_refresh():
    mark_list_dirty()
    schedule_now_playing_refresh()


# Seek relative to the current position by a delta in seconds.
def seek_relative_seconds(delta_sec: int):
    pid = kodi_api.get_active_playerid()
    if pid is None:
        return False
    props = kodi_api.kodi_call(
        "Player.GetProperties",
        {"playerid": pid, "properties": ["time", "totaltime", "canseek"]}
    ).get("result", {})
    if not props.get("canseek"):
        return False
    cur = props.get("time")
    total = props.get("totaltime")
    cur_sec = kodi_api.kodi_time_seconds(cur)
    total_sec = kodi_api.kodi_time_seconds(total)
    if cur_sec is None:
        return False
    if total_sec is not None and delta_sec > 0 and cur_sec + delta_sec >= total_sec:
        return skip_queue()
    if total_sec is None:
        total_sec = max(cur_sec + 1, 1)
    new_sec = max(0, min(cur_sec + delta_sec, total_sec))
    h = int(new_sec // 3600)
    m = int((new_sec % 3600) // 60)
    s = int(new_sec % 60)
    kodi_api.kodi_call("Player.Seek", {"playerid": pid, "value": {"time": {"hours": h, "minutes": m, "seconds": s}}})
    return True


# Seek to a percentage position (0-100).
def seek_percent(percent: int):
    pid = kodi_api.get_active_playerid()
    if pid is None:
        return False
    props = kodi_api.kodi_call(
        "Player.GetProperties",
        {"playerid": pid, "properties": ["canseek"]}
    ).get("result", {})
    if not props.get("canseek"):
        return False
    kodi_api.kodi_call("Player.Seek", {"playerid": pid, "value": {"percentage": percent}})
    return True


# Try to seek to a time once a player is available.
def seek_when_player_ready(t, context=""):
    def _seek():
        end = time.time() + RESUME_SEEK_WAIT_SEC
        start_ts = time.time()
        last_log_ts = 0.0
        while time.time() < end:
            players = kodi_api.get_active_players()
            pid = players[0]["playerid"] if players else None
            if pid is not None:
                try:
                    props = kodi_api.kodi_call(
                        "Player.GetProperties",
                        {"playerid": pid, "properties": ["totaltime", "canseek"]}
                    ).get("result", {})
                    if not props.get("canseek"):
                        log.debug("Resume seek skip canseek=false playerid=%s ctx=%s", pid, context)
                        return
                    target_sec = kodi_api.kodi_time_seconds(t)
                    if target_sec is None:
                        log.debug( f"RESUME SEEK skip invalid times playerid={pid} ctx={context} " f"target={t}" )
                        return
                    log.debug( f"RESUME SEEK playerid={pid} ctx={context} target_sec={target_sec}" )
                    kodi_api.kodi_call(
                        "Player.Seek",
                        {"playerid": pid, "value": {"time": t}}
                    )
                except Exception:
                    pass
                return
            now = time.time()
            if now - last_log_ts >= 1.0:
                elapsed = now - start_ts
                log.debug( f"RESUME SEEK waiting for playerid ctx={context} elapsed={elapsed:.1f}s players={players}" )
                last_log_ts = now
            time.sleep(0.3)
        log.warning("Resume seek gave up: no playerid ctx=%s", context)
    threading.Thread(target=_seek, daemon=True).start()


def is_soundcloud_item(item: dict):
    if not isinstance(item, dict):
        return False
    if item.get("resolver") == "soundcloud":
        return True
    link = item.get("link") or ""
    url = item.get("url") or ""
    if isinstance(link, str) and is_sc_track_url(link):
        return True
    if isinstance(url, str) and url.startswith("plugin://plugin.audio.soundcloud/"):
        return True
    return False


def soundcloud_playback_started(source_link: str):
    pid = kodi_api.get_active_playerid()
    if pid is None:
        return False
    item = (
        kodi_api.kodi_call(
            "Player.GetItem",
            {"playerid": pid, "properties": ["file", "title", "artist"]},
        ).get("result", {}) or {}
    ).get("item", {}) or {}
    file_url = item.get("file") or ""
    if not file_url:
        return False
    sc_url = kodi_api.extract_soundcloud_url(file_url)
    if sc_url and source_link and sc_url == source_link:
        return True
    if kodi_api.is_soundcloud_stream_url(file_url):
        return True
    return False


def resolve_soundcloud_playlist_media_url(playlistid=0, timeout_s=1.5, interval_s=0.25):
    end = time.time() + timeout_s
    while time.time() < end:
        res = kodi_api.kodi_call(
            "Playlist.GetItems",
            {"playlistid": playlistid, "properties": ["file", "title"]},
        )
        items = ((res or {}).get("result", {}) or {}).get("items", []) or []
        for it in items:
            file_url = it.get("file") or ""
            if "media_url=" in file_url:
                return file_url
        time.sleep(interval_s)
    return ""


def open_soundcloud_resolved_playlist_item(title: str, source_link: str, resume_time=None, playlistid=0, position=0):
    res = kodi_api.kodi_call("Player.Open", {"item": {"playlistid": playlistid, "position": position}})
    log.info("play_item soundcloud open_mode=resolved_playlist_start title=%s source=%s", title, source_link)
    log.debug("play_item open soundcloud resolved_playlist_start res=%s", res)
    schedule_playback_refresh()
    if resume_time is not None:
        seek_when_player_ready(resume_time, context="soundcloud")
    return True


def schedule_soundcloud_plugin_fallback(item: dict, source_link: str, resume_time=None):
    title = item.get("title")

    def _run():
        end = time.time() + SC_PLUGIN_START_TIMEOUT_S
        while time.time() < end:
            try:
                if soundcloud_playback_started(source_link):
                    return
            except Exception:
                pass
            time.sleep(SC_PLUGIN_START_POLL_S)

        resolved_url = resolve_soundcloud_playlist_media_url()
        if resolved_url:
            log.warning(
                "play_item soundcloud plugin_direct stalled, starting resolved playlist item "
                "title=%s source=%s",
                title,
                source_link,
            )
            open_soundcloud_resolved_playlist_item(
                title,
                source_link,
                resume_time=resume_time,
            )
            retry_end = time.time() + 2.0
            while time.time() < retry_end:
                try:
                    if soundcloud_playback_started(source_link):
                        return
                except Exception:
                    pass
                time.sleep(SC_PLUGIN_START_POLL_S)

        log.warning(
            "play_item soundcloud plugin_direct stalled and resolved playlist did not start "
            "title=%s source=%s",
            title,
            source_link,
        )

    threading.Thread(target=_run, daemon=True).start()


def play_item(item: dict, resume_time=None):
    global EXPECTED_STOP, LAST_PLAYED_RADIO
    if CANCEL_RECONNECT_CB:
        try:
            CANCEL_RECONNECT_CB()
        except Exception as e:
            log.warning("Failed to cancel reconnect callback: %s", e)
    with LOCK:
        EXPECTED_STOP = True
        LAST_PLAYED_RADIO = None
    media.cleanup_active_image_session()
    kodi_api.stop_all_players()
    kodi_api.kodi_clear_all_playlists()
    kind = item.get("kind", "video")
    resolver = item.get("resolver")
    set_expecting_ws(2)
    log.info(
        "play_item start kind=%s resolver=%s title=%s url=%s",
        item.get("kind"),
        resolver,
        item.get("title"),
        item.get("url"),
    )

    if kind == "audio" and is_soundcloud_item(item):
        # Open the addon URL directly. The old playlist-based SoundCloud path
        # (`Playlist.Add` + `Player.Open(playlistid=0)`) was what created the
        # duplicate placeholder/stream entries in Kodi's audio playlist.
        plugin_url = item["url"]
        source_link = item.get("link") or kodi_api.extract_soundcloud_url(plugin_url) or plugin_url
        kodi_api.maybe_cache_soundcloud_url(source_link)
        log.info("play_item soundcloud open_mode=plugin_direct title=%s source=%s", item.get("title"), source_link)
        res = kodi_api.kodi_call("Player.Open", {"item": {"file": plugin_url}})
        log.debug("play_item open soundcloud res=%s", res)
        schedule_soundcloud_plugin_fallback(item, source_link, resume_time=resume_time)
        schedule_playback_refresh()
        if resume_time is not None:
            seek_when_player_ready(resume_time, context="soundcloud")
    elif kind == "audio":
        playlistid = 0
        kodi_add_to_playlist(item["url"], playlistid)
        res = kodi_api.kodi_call("Player.Open", {"item": {"playlistid": playlistid, "position": 0}})
        log.debug("play_item open direct audio res=%s", res)
        schedule_playback_refresh()
        if resume_time is not None:
            seek_when_player_ready(resume_time, context="audio")
    else:
        playlistid = 1
        kodi_add_to_playlist(item["url"], playlistid)
        res = kodi_api.kodi_call("Player.Open", {"item": {"playlistid": playlistid}})
        log.debug("play_item open video res=%s", res)
        schedule_playback_refresh()
        if resume_time is not None:
            seek_when_player_ready(resume_time, context="video")


# Start playback and then seek to a saved timestamp.
def resume_item_at_time(item: dict, t):
    if not t:
        play_item(item)
        return
    play_item(item, resume_time=t)


def hard_stop_and_clear():
    global AUTOPLAY_ENABLED, CURRENT_INDEX, DISPLAY_INDEX, NEXT_INDEX, LAST_PROGRESS_TS, LAST_PROGRESS_TIME, LAST_PROGRESS_TOTAL, LAST_PROGRESS_INDEX, EXTERNAL_PLAYBACK
    global EXPECTED_STOP, LAST_PLAYED_RADIO
    
    # Check if both picture and audio players are active concurrently
    active = kodi_api.get_active_players()
    has_picture = any(p.get("type") == "picture" for p in active)
    has_audio = any(p.get("type") == "audio" for p in active)
    
    if has_picture and has_audio:
        # Stop only the picture player and preserve background music
        for p in active:
            if p.get("type") == "picture":
                pid = p.get("playerid")
                if pid is not None:
                    kodi_api.kodi_call("Player.Stop", {"playerid": pid})
        media.cleanup_active_image_session()
        schedule_playback_refresh()
        return

    if CANCEL_RECONNECT_CB:
        try:
            CANCEL_RECONNECT_CB()
        except Exception as e:
            log.warning("Failed to cancel reconnect callback: %s", e)
    media.cleanup_active_image_session()
    kodi_api.stop_all_players()
    kodi_api.kodi_clear_all_playlists()
    with LOCK:
        AUTOPLAY_ENABLED = False
        CURRENT_INDEX = None
        DISPLAY_INDEX = None
        NEXT_INDEX = 0
        LAST_PROGRESS_TS = 0.0
        LAST_PROGRESS_TIME = None
        LAST_PROGRESS_TOTAL = None
        LAST_PROGRESS_INDEX = None
        EXTERNAL_PLAYBACK = False
        BOT_EXPECTING_WS = 0
        RESUME_ATTEMPTS.clear()
        EXPECTED_STOP = True
        LAST_PLAYED_RADIO = None
    global PLAY_INDEX_TS
    PLAY_INDEX_TS = 0.0
    schedule_playback_refresh()


# Advance to the next queue item and start playback.
def skip_queue():
    global CURRENT_INDEX, DISPLAY_INDEX, NEXT_INDEX, AUTOPLAY_ENABLED

    with LOCK:
        if not QUEUE:
            AUTOPLAY_ENABLED = False
            CURRENT_INDEX = None
            DISPLAY_INDEX = None
            NEXT_INDEX = 0
            kodi_api.stop_player_and_clear_playlists()
            return False

        if REPEAT_MODE == "one" and DISPLAY_INDEX is not None:
            i = DISPLAY_INDEX
        else:
            i = 0 if DISPLAY_INDEX is None else DISPLAY_INDEX + 1

        if i >= len(QUEUE):
            if REPEAT_MODE == "all":
                i = 0
            else:
                AUTOPLAY_ENABLED = False
                CURRENT_INDEX = None
                DISPLAY_INDEX = None
                NEXT_INDEX = 0
                kodi_api.stop_player_and_clear_playlists()
                return False

    play_index(i)
    return True


# Create a queue item dict.
def make_item(title, url, kind, link=None, resolver=None):
    return {"title": title, "url": url, "kind": kind, "link": link, "resolver": resolver}


# Fetch a YouTube title and author for display.
def fetch_youtube_title(vid):
    cached = get_cached_youtube_title(vid)
    if cached:
        return cached
    url = f"https://youtu.be/{vid}"
    # Try oembed first (fast, no subprocess)
    try:
        oembed = HTTP.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=6,
        )
        if oembed.ok:
            data = oembed.json()
            author = data.get("author_name", "")
            title = data.get("title", "")
            if author and title:
                out = f"{author} - {title}"
                cache_youtube_title(vid, out)
                return out
            if title:
                cache_youtube_title(vid, title)
                return title
    except Exception:
        pass
    # Fallback: yt-dlp metadata extraction
    try:
        res = subprocess.run(
            ["yt-dlp", "--skip-download", "--print", "%(uploader)s\t%(title)s", "--no-warnings", url],
            capture_output=True, text=True, timeout=15,
        )
        if res.returncode == 0 and res.stdout.strip():
            parts = res.stdout.strip().split("\t", 1)
            if len(parts) == 2 and parts[0] and parts[1]:
                out = f"{parts[0]} - {parts[1]}"
                cache_youtube_title(vid, out)
                return out
            if parts[-1]:
                cache_youtube_title(vid, parts[-1])
                return parts[-1]
    except Exception:
        pass
    return url


# Create a YouTube queue item with Kodi plugin URL.
def make_youtube(vid, title=None):
    link = f"https://youtu.be/{vid}"
    return make_item(
        title or link,
        f"plugin://plugin.video.youtube/play/?video_id={vid}",
        "video",
        link=link
    )


# Derive a display title from a SoundCloud URL.
def soundcloud_display_title(clean_url):
    m = SC_DISPLAY_RE.match(clean_url)
    if not m:
        return clean_url
    artist = unquote(m.group(2)).replace("-", " ")
    track = unquote(m.group(3)).replace("-", " ")
    return f"{artist} - {track}".strip()


# Create a SoundCloud queue item with Kodi plugin URL.
def make_soundcloud(url):
    clean = re.sub(r"\?.*$", "", url)
    return make_item(
        soundcloud_display_title(clean),
        f"plugin://plugin.audio.soundcloud/play/?url={clean}",
        "audio",
        link=clean,
        resolver="soundcloud",
    )


def _pick_soundcloud_stream_url(info):
    if not isinstance(info, dict):
        return ""
    direct = info.get("url") or ""
    if isinstance(direct, str) and direct.startswith(("http://", "https://")):
        return direct

    best_url = ""
    best_score = None
    for fmt in info.get("formats") or []:
        if not isinstance(fmt, dict):
            continue
        url = fmt.get("url") or ""
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        score = 0
        protocol = (fmt.get("protocol") or "").lower()
        if protocol in ("http", "https"):
            score += 100
        if (fmt.get("vcodec") or "").lower() in ("", "none"):
            score += 10
        if (fmt.get("acodec") or "").lower() not in ("", "none"):
            score += 5
        abr = fmt.get("abr")
        if isinstance(abr, (int, float)):
            score += min(int(abr), 999)
        if best_score is None or score > best_score:
            best_score = score
            best_url = url
    return best_url


def _pick_soundcloud_transcoding(track_info):
    if not isinstance(track_info, dict):
        return None
    media_info = track_info.get("media") or {}
    transcodings = media_info.get("transcodings") or []
    best = None
    best_score = None
    for transcoding in transcodings:
        if not isinstance(transcoding, dict):
            continue
        api_url = transcoding.get("url") or ""
        if not isinstance(api_url, str) or not api_url.startswith(("http://", "https://")):
            continue
        format_info = transcoding.get("format") or {}
        protocol = (format_info.get("protocol") or "").lower()
        mime = (format_info.get("mime_type") or "").lower()
        preset = (transcoding.get("preset") or "").lower()
        score = 0
        if protocol == "progressive":
            score += 100
        elif protocol == "hls":
            score += 80
        if "audio/mpeg" in mime or "mp3" in preset:
            score += 10
        if "opus" in preset:
            score += 5
        if best_score is None or score > best_score:
            best_score = score
            best = transcoding
    return best


def _resolve_soundcloud_stream_url_via_api(url):
    client_id = kodi_api.read_soundcloud_client_id()
    if not client_id:
        log.warning("SoundCloud direct resolve skipped url=%s err=no_client_id", url)
        return ""
    try:
        resp = HTTP.get(
            "https://api-v2.soundcloud.com/resolve",
            params={"url": url, "client_id": client_id},
            timeout=CFG.sc_search_timeout,
        )
    except Exception as e:
        log.warning("SoundCloud resolve api failed url=%s err=%s", url, e)
        return ""
    if not resp.ok:
        log.warning("SoundCloud resolve api bad status url=%s status=%s", url, resp.status_code)
        return ""
    try:
        track_info = resp.json() or {}
    except Exception as e:
        log.warning("SoundCloud resolve api bad json url=%s err=%s", url, e)
        return ""
    transcoding = _pick_soundcloud_transcoding(track_info)
    if not transcoding:
        log.warning("SoundCloud resolve api no transcoding url=%s", url)
        return ""
    api_url = transcoding.get("url") or ""
    try:
        stream_resp = HTTP.get(
            api_url,
            params={"client_id": client_id},
            timeout=CFG.sc_search_timeout,
        )
    except Exception as e:
        log.warning("SoundCloud transcoding api failed url=%s api_url=%s err=%s", url, api_url, e)
        return ""
    if not stream_resp.ok:
        log.warning(
            "SoundCloud transcoding api bad status url=%s api_url=%s status=%s",
            url,
            api_url,
            stream_resp.status_code,
        )
        return ""
    try:
        data = stream_resp.json() or {}
    except Exception as e:
        log.warning("SoundCloud transcoding api bad json url=%s api_url=%s err=%s", url, api_url, e)
        return ""
    stream_url = data.get("url") or ""
    if not isinstance(stream_url, str) or not stream_url.startswith(("http://", "https://")):
        log.warning("SoundCloud transcoding api no stream url=%s api_url=%s", url, api_url)
        return ""
    return stream_url


def _resolve_soundcloud_stream_url_via_ytdlp(url):
    clean = re.sub(r"\?.*$", "", (url or "").strip())
    if not clean or not is_sc_track_url(clean):
        return ""
    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "socket_timeout": CFG.sc_search_timeout,
    }
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(clean, download=False)
    except Exception as e:
        log.warning("SoundCloud yt-dlp resolve failed url=%s err=%s", clean, e)
        return ""
    stream_url = _pick_soundcloud_stream_url(info)
    if not stream_url:
        log.warning("SoundCloud yt-dlp resolve returned no stream url=%s", clean)
    return stream_url


def resolve_soundcloud_stream_url(url):
    clean = re.sub(r"\?.*$", "", (url or "").strip())
    if not clean or not is_sc_track_url(clean):
        return ""
    stream_url = _resolve_soundcloud_stream_url_via_api(clean)
    if stream_url:
        return stream_url
    return _resolve_soundcloud_stream_url_via_ytdlp(clean)


# Validate that a SoundCloud URL is a track link.
def is_sc_track_url(url):
    return bool(SC_TRACK_RE.match(url)) and "discover/sets" not in url


def is_sc_set_url(url):
    return bool(SC_SET_RE.match(url)) and "discover/sets" not in url


# Resolve a SoundCloud short link to a full track URL.
def resolve_sc_short(url):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        }
        r = HTTP.get(url, allow_redirects=True, timeout=8, headers=headers)
        log.debug("SC short resolve start=%s final=%s", url, r.url)
        candidates = [h.url for h in r.history] + [r.url]
        for u in candidates:
            if SC_BASE_RE.match(u) and "discover/sets" not in u:
                log.debug("SC short resolve pick=%s", u)
                return u
        m = SC_HTML_RE.search(r.text)
        if m:
            log.debug("SC short resolve html=%s", m.group(0))
            return m.group(0)
        log.debug("SC short resolve failed")
        return None
    except Exception as e:
        log.warning("SC short resolve error=%s", e)
        return None


# Add a file URL to a Kodi playlist.
def kodi_add_to_playlist(url, playlistid):
    kodi_api.kodi_call(
        "Playlist.Add",
        {"playlistid": playlistid, "item": {"file": url}}
    )


# Expand a SoundCloud set into track URLs using yt-dlp.
def expand_soundcloud_set(url):
    clean = re.sub(r"\?.*$", "", url)
    ydl_opts = {"quiet": True, "skip_download": True, "extract_flat": True}
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(clean, download=False)
    except Exception:
        return []
    entries = info.get("entries") or []
    urls = []
    for e in entries:
        u = e.get("webpage_url") or e.get("url")
        if u and u.startswith("http"):
            urls.append(u)
    return [u for u in urls if is_sc_track_url(u)]


def queue_soundcloud_set(url):
    tracks = expand_soundcloud_set(url)
    for t in tracks:
        queue_item(make_soundcloud(t))
    mark_list_dirty()
    return len(tracks)


async def queue_soundcloud_set_async(url):
    try:
        tracks = await asyncio.to_thread(expand_soundcloud_set, url)
    except Exception:
        tracks = []
    for t in tracks:
        queue_item(make_soundcloud(t))
    mark_list_dirty()
    return len(tracks)


# Append an item to the queue and mark list dirty.
def queue_item(item):
    with LOCK:
        QUEUE.append(item)
    mark_list_dirty()


# Expand a YouTube playlist into video ids.
def expand_playlist(pid):
    url = f"https://www.youtube.com/playlist?list={pid}"
    try:
        res = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--print", "id", "--no-warnings", url],
            capture_output=True, text=True, timeout=30,
        )
        if res.returncode == 0:
            return [line.strip() for line in res.stdout.strip().splitlines() if line.strip()]
    except Exception as e:
        log.warning("yt-dlp playlist expansion failed: %s", e)
    return []


# Append a YouTube video to the queue.
def queue_video(vid, title=None):
    with LOCK:
        QUEUE.append(make_youtube(vid, title=title))
    mark_list_dirty()


# Fetch YouTube title asynchronously and queue the video.
async def queue_video_async(vid):
    try:
        title = await asyncio.to_thread(fetch_youtube_title, vid)
    except Exception:
        title = None
    queue_video(vid, title=title)


# Queue all items from a YouTube playlist.
def queue_playlist(pid):
    for vid in expand_playlist(pid):
        queue_video(vid)
    mark_list_dirty()


# Asynchronously queue all items from a YouTube playlist.
async def queue_playlist_async(pid):
    try:
        vids = await asyncio.to_thread(expand_playlist, pid)
    except Exception:
        vids = []
    sem = asyncio.Semaphore(5)

    async def _fetch_title(vid):
        async with sem:
            try:
                return await asyncio.to_thread(fetch_youtube_title, vid)
            except Exception:
                return None

    titles = await asyncio.gather(*(_fetch_title(vid) for vid in vids))
    for vid, title in zip(vids, titles):
        queue_video(vid, title=title)
    mark_list_dirty()
    return len(vids)


# Clear the queue and reset indices.
def clear_queue():
    global CURRENT_INDEX, DISPLAY_INDEX, NEXT_INDEX, LAST_PROGRESS_TS, LAST_PROGRESS_TIME, LAST_PROGRESS_TOTAL, LAST_PROGRESS_INDEX, EXTERNAL_PLAYBACK, BOT_EXPECTING_WS
    with LOCK:
        QUEUE.clear()
        CURRENT_INDEX = None
        DISPLAY_INDEX = None
        NEXT_INDEX = 0
        LAST_PROGRESS_TS = 0.0
        LAST_PROGRESS_TIME = None
        LAST_PROGRESS_TOTAL = None
        LAST_PROGRESS_INDEX = None
        EXTERNAL_PLAYBACK = False
        BOT_EXPECTING_WS = 0  # safe: under LOCK
        RESUME_ATTEMPTS.clear()
    mark_list_dirty()


# Remove a queue item by index with safety checks.
def delete_index(i):
    global CURRENT_INDEX, NEXT_INDEX, DISPLAY_INDEX

    with LOCK:
        if i < 0 or i >= len(QUEUE):
            return False, "Invalid index."

        if DISPLAY_INDEX is not None and i == DISPLAY_INDEX:
            return False, "You cannot delete the currently playing title. Use /skip or /stop first."

        QUEUE.pop(i)

        if DISPLAY_INDEX is not None and i < DISPLAY_INDEX:
            DISPLAY_INDEX -= 1

        if CURRENT_INDEX is not None and i < CURRENT_INDEX:
            CURRENT_INDEX -= 1

        if i < NEXT_INDEX:
            NEXT_INDEX -= 1

        mark_list_dirty()
        return True, None


# Play a specific queue index and update state.
def play_index(i):
    global CURRENT_INDEX, DISPLAY_INDEX, NEXT_INDEX, AUTOPLAY_ENABLED, EXTERNAL_PLAYBACK, PLAY_INDEX_TS
    with LOCK:
        if i < 0 or i >= len(QUEUE):
            return
        CURRENT_INDEX = i
        DISPLAY_INDEX = i
        NEXT_INDEX = i + 1
        AUTOPLAY_ENABLED = True
        EXTERNAL_PLAYBACK = False
        item = QUEUE[i]
        RESUME_ATTEMPTS.clear()
    # Record when a bot-initiated track starts so the panel can show queue
    # info optimistically even after BOT_EXPECTING_WS drops to 0.
    PLAY_INDEX_TS = time.time()
    mark_list_dirty()
    play_item(item)


# Check if the requested index is already playing or starting.
def is_requested_track_already_playing(i):
    with LOCK:
        if DISPLAY_INDEX is None or i != DISPLAY_INDEX:
            return False
    if get_expecting_ws() > 0:
        return True
    return kodi_api.WS_PLAYING


# Go back to the previous queue item.
def back_queue():
    global CURRENT_INDEX, DISPLAY_INDEX, NEXT_INDEX, AUTOPLAY_ENABLED

    with LOCK:
        if not QUEUE:
            return False
        if REPEAT_MODE == "one" and DISPLAY_INDEX is not None:
            i = DISPLAY_INDEX
        else:
            if DISPLAY_INDEX is None:
                i = len(QUEUE) - 1 if REPEAT_MODE == "all" else 0
            else:
                i = DISPLAY_INDEX - 1
                if i < 0:
                    if REPEAT_MODE == "all":
                        i = len(QUEUE) - 1
                    else:
                        i = 0

    play_index(i)
    return True


# Background loop that advances playback automatically.
def autoplay_loop():
    global CURRENT_INDEX, NEXT_INDEX, AUTOPLAY_ENABLED, DISPLAY_INDEX
    global LAST_PROGRESS_INDEX, LAST_PROGRESS_TIME, LAST_PROGRESS_TOTAL

    while True:
        try:
            now = time.time()
            playback_state = kodi_api.WS_STATE

            if not kodi_api.WS_CONNECTED:
                time.sleep(0.5)
                continue

            if not AUTOPLAY_ENABLED:
                time.sleep(0.5)
                continue

            if get_expecting_ws() > 0:
                time.sleep(0.2)
                continue

            # Fallback: some streams stop without emitting Player.OnStop.
            # If progress is stale and Kodi reports no active player, treat it as stopped.
            if playback_state != "stopped" and DISPLAY_INDEX is not None:
                freshness_ts = max(LAST_PROGRESS_TS or 0.0, kodi_api.WS_LAST_EVENT_TS or 0.0)
                stale = freshness_ts > 0 and (now - freshness_ts) >= RESUME_STALE_PROGRESS_SEC
                if stale:
                    players = kodi_api.get_active_players()
                    if not players:
                        log.info(
                            "Resume inferred stop without ws-event "
                            "state=%s idx=%s stale_for=%.1fs",
                            playback_state, DISPLAY_INDEX, now - freshness_ts,
                        )
                        playback_state = "stopped"

            if playback_state == "playing":
                time.sleep(0.5)
                continue

            if playback_state == "paused":
                time.sleep(0.5)
                continue

            resume_pending = False
            if playback_state == "stopped" and DISPLAY_INDEX is not None and LAST_PROGRESS_INDEX == DISPLAY_INDEX and LAST_PROGRESS_TIME:
                remaining = None
                if LAST_PROGRESS_TOTAL:
                    cur_sec = kodi_api.kodi_time_seconds(LAST_PROGRESS_TIME)
                    total_sec = kodi_api.kodi_time_seconds(LAST_PROGRESS_TOTAL)
                    if cur_sec is not None and total_sec is not None:
                        remaining = max(total_sec - cur_sec, 0)
                if remaining is None or remaining > RESUME_MIN_REMAINING_SEC:
                    attempts = RESUME_ATTEMPTS.get(DISPLAY_INDEX, 0)
                    resume_pending = attempts < RESUME_MAX_ATTEMPTS
                    if resume_pending:
                        log.info(
                            "Resume pending idx=%s attempts=%s remaining=%s",
                            DISPLAY_INDEX, attempts, remaining,
                        )

            if playback_state == "stopped" and DISPLAY_INDEX is not None:
                if LAST_PROGRESS_INDEX == DISPLAY_INDEX and LAST_PROGRESS_TIME:
                    remaining = None
                    if LAST_PROGRESS_TOTAL:
                        cur_sec = kodi_api.kodi_time_seconds(LAST_PROGRESS_TIME)
                        total_sec = kodi_api.kodi_time_seconds(LAST_PROGRESS_TOTAL)
                        if cur_sec is not None and total_sec is not None:
                            remaining = max(total_sec - cur_sec, 0)
                    if remaining is not None and remaining <= RESUME_MIN_REMAINING_SEC:
                        if REPEAT_MODE == "one":
                            NEXT_INDEX = CURRENT_INDEX
                        CURRENT_INDEX = None
                        DISPLAY_INDEX = None
                        LAST_PROGRESS_TIME = None
                        LAST_PROGRESS_INDEX = None
                        LAST_PROGRESS_TOTAL = None
                        mark_list_dirty()
                        continue
                    attempts = RESUME_ATTEMPTS.get(DISPLAY_INDEX, 0)
                    if attempts < RESUME_MAX_ATTEMPTS:
                        RESUME_ATTEMPTS[DISPLAY_INDEX] = attempts + 1
                        log.info(
                            "Resume attempt idx=%s attempt=%s remaining=%s",
                            DISPLAY_INDEX, RESUME_ATTEMPTS[DISPLAY_INDEX], remaining,
                        )
                        with LOCK:
                            if DISPLAY_INDEX is not None and DISPLAY_INDEX < len(QUEUE):
                                item = QUEUE[DISPLAY_INDEX]
                            else:
                                item = None
                        if item:
                            resume_item_at_time(item, LAST_PROGRESS_TIME)
                            time.sleep(0.3)
                            continue
                    else:
                        CURRENT_INDEX = None
                        DISPLAY_INDEX = None
                        mark_list_dirty()

            if resume_pending:
                time.sleep(0.3)
                continue

            if playback_state == "stopped":
                if CURRENT_INDEX is not None:
                    if REPEAT_MODE == "one":
                        NEXT_INDEX = CURRENT_INDEX
                    CURRENT_INDEX = None
                    time.sleep(0.3)
                    continue

                with LOCK:
                    if NEXT_INDEX < len(QUEUE):
                        CURRENT_INDEX = NEXT_INDEX
                        DISPLAY_INDEX = CURRENT_INDEX
                        item = QUEUE[CURRENT_INDEX]
                        NEXT_INDEX += 1
                        mark_list_dirty()
                    else:
                        if REPEAT_MODE == "all":
                            NEXT_INDEX = 0
                            CURRENT_INDEX = None
                            DISPLAY_INDEX = None
                        else:
                            AUTOPLAY_ENABLED = False
                            CURRENT_INDEX = None
                            DISPLAY_INDEX = None
                        item = None

                if item:
                    play_item(item)

        except Exception as e:
            log.error("Autoplay error: %s", e, exc_info=True)

        time.sleep(1)


AUTOPLAY_THREAD_STARTED = False
AUTOPLAY_THREAD = None


def start_autoplay_thread():
    global AUTOPLAY_THREAD_STARTED, AUTOPLAY_THREAD
    if AUTOPLAY_THREAD_STARTED and AUTOPLAY_THREAD and AUTOPLAY_THREAD.is_alive():
        return
    AUTOPLAY_THREAD_STARTED = True
    AUTOPLAY_THREAD = threading.Thread(target=autoplay_loop, daemon=True)
    AUTOPLAY_THREAD.start()
