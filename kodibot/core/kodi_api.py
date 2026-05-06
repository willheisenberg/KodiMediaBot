import logging
import re
import shlex
import threading
import time
import subprocess
import json
import asyncio
import unicodedata
from urllib.parse import unquote, quote_plus, urlparse, parse_qs

import requests
import websockets
from kodibot.telegram import media
from kodibot.config import CFG

log = logging.getLogger(__name__)

CEC_CMD_VOL_UP = "0x41"
CEC_CMD_VOL_DOWN = "0x42"

YT = re.compile(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})")
PL = re.compile(r"(?:[?&]list=)([A-Za-z0-9_-]+)")
SC = re.compile(r"https?://(www\.)?soundcloud\.com/[^/]+/[^/?#]+")
SC_SET = re.compile(r"https?://(www\.)?soundcloud\.com/[^/]+/sets/[^/?#]+")
SC_SHORT = re.compile(r"https?://on\.soundcloud\.com/[A-Za-z0-9]+")
YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
IMDB_ID_RE = re.compile(r"^tt\d+$")
PLAY_MEDIA_RE = re.compile(r"^\s*PlayMedia\((.*)\)\s*$", flags=re.IGNORECASE)

WS_CONNECTED = False
WS_PLAYING = False
WS_LAST_EVENT_TS = 0.0
WS_STATE = "unknown"

LAST_WS_ITEM = {}
LAST_WS_PLAYERID = None
LAST_WS_YT_ID = ""
LAST_WS_PLAYING_FILE = ""
LAST_WS_SC_URL = ""
LAST_WS_SC_TRACK_ID = ""
LAST_WS_SC_LOOKUP_TS = 0.0
LAST_WS_SC_PROBE_TS = 0.0
LAST_WS_SC_PROBE_ACTIVE = False

SC_CLIENT_ID_CACHE = ""
SC_CLIENT_ID_TS = 0.0
SC_PERMALINK_CACHE = {}
SC_PERMALINK_TTL = 3600.0
RADIO_STREAM_MAP_CACHE = None
RADIO_M3U_MAP_CACHE = None
ICY_TITLE_CACHE = {}
YT_SEARCH_CACHE = {}
SC_SEARCH_CACHE = {}
HTTP = requests.Session()
LAST_KODI_ERROR_LOG_TS = 0.0
PLAYER_GETITEM_PROPERTIES = ["title", "artist", "file"]

# ── WebSocket callback registry ─────────────────────────────────────
# Replaces the circular importlib hack.  queue_state registers its
# handlers via set_ws_handlers() during startup.
_ws_on_play = None
_ws_on_pause = None
_ws_on_resume = None
_ws_on_stop = None
_ws_on_playback_refresh = None


def set_ws_handlers(*, on_play=None, on_pause=None, on_resume=None,
                    on_stop=None, on_playback_refresh=None):
    global _ws_on_play, _ws_on_pause, _ws_on_resume, _ws_on_stop
    global _ws_on_playback_refresh
    _ws_on_play = on_play
    _ws_on_pause = on_pause
    _ws_on_resume = on_resume
    _ws_on_stop = on_stop
    _ws_on_playback_refresh = on_playback_refresh


# Send a JSON-RPC request to Kodi and return the response JSON.
def kodi_call(method: str, params: dict | None = None):
    global LAST_KODI_ERROR_LOG_TS
    payload = {"jsonrpc": "2.0", "method": method, "id": 1}
    if params:
        payload["params"] = params
    try:
        resp = HTTP.post(CFG.kodi_url, auth=CFG.kodi_auth, json=payload, timeout=5)
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        now = time.time()
        if now - LAST_KODI_ERROR_LOG_TS >= CFG.kodi_error_log_interval:
            LAST_KODI_ERROR_LOG_TS = now
            log.error(f"Kodi call failed: method={method} host={CFG.kodi_host} port={CFG.kodi_port} err={e}")
        return {
            "error": {
                "message": str(e),
                "type": e.__class__.__name__,
                "method": method,
            }
        }


async def kodi_call_async(method: str, params: dict | None = None):
    return await asyncio.to_thread(kodi_call, method, params)


def kodi_call_with_props(method, id_key, id_value, properties):
    props = list(properties)
    while props:
        res = kodi_call(method, {id_key: id_value, "properties": props})
        if not res.get("error"):
            return res
        if log.isEnabledFor(logging.DEBUG):
            log.debug(f"Library fetch: retry method={method} props={props} err={res.get('error')}")
        props = props[:-1]
    return kodi_call(method, {id_key: id_value, "properties": []})


# Return Kodi favourites that can be opened as playable media.
def get_playable_favourites():
    attempts = [
        {"type": "media", "properties": ["path", "windowparameter"]},
        {"type": "media"},
        {"properties": ["path", "windowparameter"]},
        None,
    ]
    raw_favs = []
    for params in attempts:
        res = kodi_call("Favourites.GetFavourites", params)
        if res.get("error"):
            continue
        result = res.get("result", {}) or {}
        raw_favs = result.get("favourites") or []
        break
    out = []
    for fav in raw_favs:
        target = favourite_media_target(fav)
        if not target:
            continue
        title = fav.get("title") or fav.get("name") or target
        out.append({"title": title, "target": target})
    return out


def favourite_media_target(fav):
    if not isinstance(fav, dict):
        return None
    path = fav.get("path")
    if isinstance(path, str) and path:
        return path
    wp = fav.get("windowparameter")
    if isinstance(wp, str) and wp:
        wp = unquote(wp).strip()
        if wp.startswith((
            "plugin://",
            "http://",
            "https://",
            "smb://",
            "nfs://",
            "file://",
            "musicdb://",
            "videodb://",
            "special://",
        )):
            return wp
    favourite_cmd = fav.get("favourite")
    if isinstance(favourite_cmd, str):
        m = PLAY_MEDIA_RE.match(favourite_cmd)
        if m:
            raw = m.group(1).strip()
            raw = raw.strip("\"'")
            if raw:
                return unquote(raw)
    return None


def play_favourite_target(target):
    if not target:
        return False
    stop_player_and_clear_playlists()
    res = kodi_call("Player.Open", {"item": {"file": target}})
    return "error" not in res


def play_picture(file_path):
    if not file_path:
        return False
    stop_player_and_clear_playlists()
    res = kodi_call("Player.Open", {"item": {"file": file_path}})
    return "error" not in res


def play_picture_slideshow(directory_path):
    if not directory_path:
        return False
    stop_player_and_clear_playlists()
    res = kodi_call(
        "Player.Open",
        {"item": {"directory": directory_path, "media": "pictures", "recursive": False}},
    )
    return "error" not in res


# Return the first active Kodi player, if any.
def get_active_player():
    players = get_active_players()
    return players[0] if players else None


# Return the active player id, if any.
def get_active_playerid():
    p = get_active_player()
    return p["playerid"] if p else None


# Fetch the list of active Kodi players.
def get_active_players():
    return kodi_call("Player.GetActivePlayers").get("result", [])


def is_picture_player_active():
    return any(p.get("type") == "picture" for p in get_active_players())


def wait_for_picture_player_active(timeout_s=2.0, interval_s=0.1):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if is_picture_player_active():
            return True
        time.sleep(interval_s)
    return is_picture_player_active()


async def cleanup_image_session_after_stop_delay(stopped_file, delay_s=2.0):
    await asyncio.sleep(delay_s)
    if not media.is_active_image_session_media(stopped_file):
        return
    picture_active = await asyncio.to_thread(is_picture_player_active)
    if not picture_active:
        await asyncio.to_thread(media.cleanup_temp_media, stopped_file)


def get_av_settings():
    playerid = get_active_playerid()
    if playerid is None:
        return {"playerid": None, "error": "nothing_playing"}
    res = kodi_call(
        "Player.GetProperties",
        {
            "playerid": playerid,
            "properties": [
                "audiostreams",
                "currentaudiostream",
                "subtitles",
                "currentsubtitle",
                "subtitleenabled",
            ],
        },
    )
    if res.get("error"):
        return {"playerid": playerid, "error": res.get("error")}
    result = (res.get("result", {}) or {})
    return {
        "playerid": playerid,
        "audiostreams": result.get("audiostreams") or [],
        "currentaudiostream": result.get("currentaudiostream") or {},
        "subtitles": result.get("subtitles") or [],
        "currentsubtitle": result.get("currentsubtitle") or {},
        "subtitleenabled": bool(result.get("subtitleenabled")),
    }


def set_audio_stream(stream_index):
    playerid = get_active_playerid()
    if playerid is None or stream_index is None:
        return False
    res = kodi_call("Player.SetAudioStream", {"playerid": playerid, "stream": stream_index})
    return "error" not in res


def set_subtitle_stream(subtitle_index):
    playerid = get_active_playerid()
    if playerid is None or subtitle_index is None:
        return False
    res = kodi_call(
        "Player.SetSubtitle",
        {"playerid": playerid, "subtitle": subtitle_index, "enable": True},
    )
    return "error" not in res


def disable_subtitles():
    playerid = get_active_playerid()
    if playerid is None:
        return False
    res = kodi_call(
        "Player.SetSubtitle",
        {"playerid": playerid, "subtitle": "off", "enable": False},
    )
    return "error" not in res


def pick_playerid(players):
    if not players:
        return None
    for p in players:
        if p.get("type") == "video":
            return p.get("playerid")
    return players[0].get("playerid")


# Stop all active Kodi players.
def stop_all_players():
    for p in get_active_players():
        pid = p.get("playerid")
        if pid is not None:
            kodi_call("Player.Stop", {"playerid": pid})


# Stop playback and clear Kodi playlists.
def stop_player_and_clear_playlists():
    stop_all_players()
    kodi_clear_all_playlists()


# Clear both audio and video Kodi playlists.
def kodi_clear_all_playlists():
    kodi_call("Playlist.Clear", {"playlistid": 0})
    kodi_call("Playlist.Clear", {"playlistid": 1})


from kodibot.core.kodi_hifi import (
    run_cec_volume,
    run_denon_volume_delta,
    run_volume_delta,
    run_denon_power,
    run_cec_power,
    run_airplay_kill,
    get_hifi_power_status,
    get_airplay_status,
    get_denon_mainzone_volume,
)
from kodibot.core.kodi_metadata import (
    format_kodi_time,
    kodi_time_seconds,
    normalize_title,
    normalize_radio_track_title,
    normalize_match_text,
    youtube_result_matches_radio_track,
    soundcloud_result_matches_radio_track,
    kodi_item_name,
    extract_youtube_id,
    soundcloud_slug,
    soundcloud_track_slug_from_url,
    soundcloud_display_title_from_url,
    is_soundcloud_stream_url,
    guess_soundcloud_link,
    extract_soundcloud_url,
    read_soundcloud_client_id,
    extract_soundcloud_track_id,
    normalize_channel_name,
    read_radio_stream_map,
    read_radio_stream_map_from_m3u,
    get_radio_stream_m3u_map,
    get_radio_stream_url,
    get_cached_icy_title,
    cache_icy_title,
    fetch_icy_title,
    get_cached_youtube_link,
    cache_youtube_link,
    get_cached_soundcloud_link,
    cache_soundcloud_link,
    search_youtube_link,
    search_soundcloud_link,
    radio_title_to_youtube_link,
    radio_title_to_soundcloud_link,
    resolve_radio_title,
    get_cached_soundcloud_permalink,
    cache_soundcloud_permalink,
    fetch_soundcloud_permalink,
    maybe_cache_soundcloud_url,
    resolve_soundcloud_link_from_kodi,
    schedule_soundcloud_permalink_probe,
    external_item_display,
    kodi_item_matches_queue,
    fetch_library_item,
    build_imdb_link,
)
from kodibot.core.kodi_library import (
    scan_video_library,
    list_movies,
    list_tvshows,
    list_tvshow_episodes,
    play_movie,
    play_episode,
    play_all_episodes,
)
from kodibot.core.kodi_ws import (
    cleanup_image_session_after_stop_delay,
    kodi_ws_listener,
)
