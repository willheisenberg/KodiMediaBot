import os
import re
import threading
import time
import subprocess
import json
import asyncio
import unicodedata
import importlib
from urllib.parse import unquote, quote_plus, urlparse, parse_qs

import requests
import websockets

KODI_HOST = os.environ["KODI_HOST"]
KODI_PORT = os.environ["KODI_PORT"]
KODI_WS_PORT = os.environ["KODI_WS_PORT"]
KODI_URL = f"http://{KODI_HOST}:{KODI_PORT}/jsonrpc"
AUTH = (os.environ["KODI_USER"], os.environ["KODI_PASS"])
CEC_HOST = os.environ.get("CEC_HOST") or os.environ.get("HOST_IP")
CEC_CMD_VOL_UP = "0x41"
CEC_CMD_VOL_DOWN = "0x42"
DEBUG_WS = os.environ.get("DEBUG_WS") in ("1", "true", "True", "yes", "YES")
DENON_HOST = os.environ.get("DENON_HOST")
DENON_VOLUME_STEP_COMMANDS = 2

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
KODI_WS_URL = None

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
ICY_TITLE_TTL = float(os.environ.get("ICY_TITLE_TTL", "15"))
ICY_TIMEOUT = float(os.environ.get("ICY_TIMEOUT", "6"))
RADIO_M3U_PATH = os.environ.get("RADIO_M3U_PATH", "/data/kodi.m3u")
YT_SEARCH_CACHE = {}
YT_SEARCH_TTL = float(os.environ.get("RADIO_YT_TTL", "21600"))
YT_SEARCH_FAIL_TTL = float(os.environ.get("RADIO_YT_FAIL_TTL", "300"))
YT_SEARCH_TIMEOUT = float(os.environ.get("RADIO_YT_TIMEOUT", "8"))
SC_SEARCH_CACHE = {}
SC_SEARCH_TTL = float(os.environ.get("RADIO_SC_TTL", "21600"))
SC_SEARCH_FAIL_TTL = float(os.environ.get("RADIO_SC_FAIL_TTL", "300"))
SC_SEARCH_TIMEOUT = float(os.environ.get("RADIO_SC_TIMEOUT", "8"))
HTTP = requests.Session()
QUEUE_STATE_MODULE = None


def get_queue_state_module():
    global QUEUE_STATE_MODULE
    if QUEUE_STATE_MODULE is None:
        QUEUE_STATE_MODULE = importlib.import_module("queue_state")
    return QUEUE_STATE_MODULE


# Send a JSON-RPC request to Kodi and return the response JSON.
def kodi_call(method: str, params: dict | None = None):
    payload = {"jsonrpc": "2.0", "method": method, "id": 1}
    if params:
        payload["params"] = params
    return HTTP.post(KODI_URL, auth=AUTH, json=payload, timeout=5).json()


async def kodi_call_async(method: str, params: dict | None = None):
    return await asyncio.to_thread(kodi_call, method, params)


def kodi_call_with_props(method, id_key, id_value, properties):
    props = list(properties)
    while props:
        res = kodi_call(method, {id_key: id_value, "properties": props})
        if not res.get("error"):
            return res
        if DEBUG_WS:
            print(f"LIB FETCH retry method={method} props={props} err={res.get('error')}", flush=True)
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


def pick_playerid(players):
    if not players:
        return None
    for p in players:
        if p.get("type") == "video":
            return p.get("playerid")
    return players[0].get("playerid")


# Send repeated CEC volume commands over SSH.
def run_cec_volume(times: int, cmd_hex: str) -> bool:
    cmd = (
        f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@{CEC_HOST} seq {times} | "
        f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@{CEC_HOST} "
        f"xargs -Iz cec-ctl --user-control-pressed ui-cmd={cmd_hex} -t5"
    )
    try:
        res = subprocess.run(cmd, shell=True, check=False, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"CEC FAIL rc={res.returncode} stderr={res.stderr.strip()}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"CEC ERROR err={e}", flush=True)
        return False


def run_denon_volume_delta(points: int) -> bool:
    if not DENON_HOST:
        return False
    if points == 0:
        return True
    cmd = "MVUP" if points > 0 else "MVDOWN"
    steps = abs(points) * DENON_VOLUME_STEP_COMMANDS
    url = f"http://{DENON_HOST}/goform/formiPhoneAppDirect.xml?{cmd}"
    try:
        for _ in range(steps):
            res = HTTP.get(url, timeout=4)
            if res.status_code != 200:
                print(
                    f"DENON VOLUME FAIL status={res.status_code} host={DENON_HOST} points={points} cmd={cmd}",
                    flush=True,
                )
                return False
            time.sleep(0.05)
        return True
    except Exception as e:
        print(f"DENON VOLUME ERROR host={DENON_HOST} points={points} err={e}", flush=True)
        return False


def run_volume_delta(points: int) -> bool:
    if DENON_HOST:
        return run_denon_volume_delta(points)
    if points == 0:
        return True
    cmd_hex = CEC_CMD_VOL_UP if points > 0 else CEC_CMD_VOL_DOWN
    times = abs(points) * 2
    return run_cec_volume(times, cmd_hex)


def run_denon_power(on: bool) -> bool:
    if not DENON_HOST:
        return False
    action = "PowerOn" if on else "PowerStandby"
    url = f"http://{DENON_HOST}/goform/formiPhoneAppPower.xml?1+{action}"
    try:
        res = HTTP.get(url, timeout=4)
        if res.status_code != 200:
            print(f"DENON POWER FAIL status={res.status_code} host={DENON_HOST} action={action}", flush=True)
            return False
        text = res.text or ""
        expected = "ON" if on else "OFF"
        m = re.search(r"<Power>\s*<value>\s*(ON|OFF)\s*</value>\s*</Power>", text, flags=re.IGNORECASE)
        if not m:
            print(f"DENON POWER FAIL host={DENON_HOST} action={action} body={text[:120]!r}", flush=True)
            return False
        state = m.group(1).upper()
        if state != expected:
            print(f"DENON POWER FAIL host={DENON_HOST} expected={expected} got={state}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"DENON POWER ERROR host={DENON_HOST} action={action} err={e}", flush=True)
        return False


# Turn the audio system on or off via CEC over SSH.
def run_cec_power(on: bool) -> bool:
    if DENON_HOST:
        return run_denon_power(on)
    if on:
        cmd = (
            f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@{CEC_HOST} "
            f"cec-ctl --user-control-pressed ui-cmd=power-on-function -t0 && "
            f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@{CEC_HOST} "
            f"cec-ctl --user-control-pressed ui-cmd=power-on-function -t5"
        )
    else:
        cmd = (
            f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@{CEC_HOST} "
            f"cec-ctl --standby -t0 && "
            f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@{CEC_HOST} "
            f"cec-ctl --standby -t5"
        )
    try:
        res = subprocess.run(cmd, shell=True, check=False, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"CEC FAIL rc={res.returncode} stderr={res.stderr.strip()}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"CEC ERROR err={e}", flush=True)
        return False


def run_airplay_kill() -> bool:
    cmd = (
        f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@{CEC_HOST} "
        f"cec-ctl --active-source phys-addr=1.5.0.0 -t0"
    )
    try:
        res = subprocess.run(cmd, shell=True, check=False, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"CEC FAIL rc={res.returncode} stderr={res.stderr.strip()}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"CEC ERROR err={e}", flush=True)
        return False


# Query the audio system power state (DENON over IP when available, else CEC).
def get_hifi_power_status():
    if DENON_HOST:
        url = f"http://{DENON_HOST}/goform/formMainZone_MainZoneXml.xml"
        try:
            res = HTTP.get(url, timeout=4)
            if res.status_code != 200:
                print(f"DENON POWER STATUS FAIL status={res.status_code} host={DENON_HOST}", flush=True)
                return None
            text = res.text or ""
            m = re.search(r"<Power>\s*<value>\s*(ON|OFF|STANDBY)\s*</value>\s*</Power>", text, flags=re.IGNORECASE)
            if not m:
                return None
            state = m.group(1).upper()
            if state == "ON":
                return "On"
            if state in ("OFF", "STANDBY"):
                return "Standby"
            return None
        except Exception as e:
            print(f"DENON POWER STATUS ERROR host={DENON_HOST} err={e}", flush=True)
            return None

    cmd = (
        f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@{CEC_HOST} "
        f"cec-ctl --show-topology | awk '/Audio System/ {{f=1}} f && /Power Status/ {{print $NF; exit}}'"
    )
    try:
        res = subprocess.run(cmd, shell=True, check=False, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"CEC FAIL rc={res.returncode} stderr={res.stderr.strip()}", flush=True)
            return None
        val = (res.stdout or "").strip()
        if val in ("On", "Standby"):
            return val
        return None
    except Exception as e:
        print(f"CEC ERROR err={e}", flush=True)
        return None


def get_airplay_status():
    if not DENON_HOST:
        return None
    url = f"http://{DENON_HOST}/goform/formNetAudio_StatusXml.xml"
    try:
        res = HTTP.get(url, timeout=4)
        if res.status_code != 200:
            print(f"AIRPLAY FAIL status={res.status_code} host={DENON_HOST}", flush=True)
            return None
        text = res.text or ""
        m = re.search(r"<szLine>(.*?)</szLine>", text, flags=re.DOTALL | re.IGNORECASE)
        if not m:
            return None
        values = re.findall(r"<value>(.*?)</value>", m.group(1), flags=re.DOTALL | re.IGNORECASE)
        line1 = values[0].strip() if len(values) >= 1 else ""
        line2 = values[1].strip() if len(values) >= 2 else ""
        if line1 == "Now Playing" and line2 == "AirPlay":
            return "On"
        return "Off"
    except Exception as e:
        print(f"AIRPLAY ERROR host={DENON_HOST} err={e}", flush=True)
        return None


def get_denon_mainzone_volume():
    if not DENON_HOST:
        return None
    url = f"http://{DENON_HOST}/goform/formMainZone_MainZoneXml.xml"
    try:
        res = HTTP.get(url, timeout=4)
        if res.status_code != 200:
            print(f"DENON VOLUME FAIL status={res.status_code} host={DENON_HOST}", flush=True)
            return None
        text = res.text or ""
        m = re.search(
            r"<MasterVolume>\s*<value>\s*([+-]?\d+(?:\.\d+)?)\s*</value>",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if m:
            return m.group(1)
        values = re.findall(r"<value>\s*([+-]?\d+(?:\.\d+)?)\s*</value>", text, flags=re.IGNORECASE)
        if not values:
            return None
        for val in values:
            if val.startswith("-"):
                return val
        return values[0]
    except Exception as e:
        print(f"DENON VOLUME ERROR host={DENON_HOST} err={e}", flush=True)
        return None


# Format Kodi time dict into a mm:ss or h:mm:ss string.
def format_kodi_time(t):
    if not t:
        return "00:00"
    h = t.get("hours", 0)
    m = t.get("minutes", 0)
    s = t.get("seconds", 0)
    total = h * 3600 + m * 60 + s
    if total >= 3600:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


# Convert Kodi time dict into total seconds.
def kodi_time_seconds(t):
    if not t:
        return None
    return t.get("hours", 0) * 3600 + t.get("minutes", 0) * 60 + t.get("seconds", 0)


# Normalize a title for loose comparison.
def normalize_title(s):
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip().casefold()


def normalize_radio_track_title(track_title):
    if not track_title:
        return ""
    raw = re.sub(r"\s+", " ", track_title).strip()
    if not raw:
        return ""

    parts = [p.strip() for p in raw.split("|") if p.strip()]
    candidates = parts if parts else [raw]
    dashed = [p for p in candidates if " - " in p]
    pick = dashed[0] if dashed else candidates[0]

    if " - " in pick:
        left, right = pick.split(" - ", 1)
        left = left.strip()
        right = right.strip()
        if left and right:
            return f"{left} - {right}"
    return pick


def normalize_match_text(text):
    if not text:
        return ""
    norm = unicodedata.normalize("NFKD", text)
    norm = norm.encode("ascii", "ignore").decode().casefold()
    norm = re.sub(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}", " ", norm)
    norm = re.sub(r"\b(ft|feat)\.?\b", " feat ", norm)
    norm = re.sub(
        r"\b(official|audio|video|lyrics?|lyric|visualizer|remaster(?:ed)?|hd|4k|hq|topic|vevo)\b",
        " ",
        norm,
    )
    norm = re.sub(r"[^a-z0-9]+", " ", norm)
    return re.sub(r"\s+", " ", norm).strip()


def youtube_result_matches_radio_track(track_title, result_title):
    clean_track = normalize_radio_track_title(track_title)
    track_norm = normalize_match_text(clean_track)
    result_norm = normalize_match_text(result_title)
    if not track_norm or not result_norm:
        return False
    if track_norm in result_norm or result_norm in track_norm:
        return True
    if " - " not in clean_track:
        return False
    artist, title = clean_track.split(" - ", 1)
    artist_norm = normalize_match_text(artist)
    title_norm = normalize_match_text(title)
    if not artist_norm or not title_norm:
        return False
    return artist_norm in result_norm and title_norm in result_norm


def soundcloud_result_matches_radio_track(track_title, result_title, result_artist=""):
    clean_track = normalize_radio_track_title(track_title)
    track_norm = normalize_match_text(clean_track)
    title_norm = normalize_match_text(result_title)
    artist_norm = normalize_match_text(result_artist)
    combined_norm = " ".join(part for part in (artist_norm, title_norm) if part).strip()
    if not track_norm or not combined_norm:
        return False
    if track_norm in combined_norm:
        return True
    if " - " not in clean_track:
        return track_norm in title_norm
    expected_artist, expected_title = clean_track.split(" - ", 1)
    expected_artist_norm = normalize_match_text(expected_artist)
    expected_title_norm = normalize_match_text(expected_title)
    if not expected_artist_norm or not expected_title_norm:
        return False
    return expected_artist_norm in combined_norm and expected_title_norm in combined_norm


# Build a display name from a Kodi player item.
def kodi_item_name(item):
    if not item:
        return ""
    artists = item.get("artist") or []
    title = item.get("title") or ""
    label = item.get("label") or ""
    if artists and title:
        return f"{', '.join(artists)} - {title}"
    return label or title or ""


# Derive a YouTube video id from a URL or Kodi plugin URL.
def extract_youtube_id(url):
    if not url:
        return ""
    m = YT.search(url)
    if m:
        return m.group(1)
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    qs = parse_qs(parsed.query)
    vid_param = (qs.get("video_id") or [""])[0]
    if vid_param and YT_ID_RE.match(vid_param):
        return vid_param
    file_param = (qs.get("file") or [""])[0]
    if file_param:
        base = file_param.split("/")[-1]
        if "." in base:
            base = base.split(".", 1)[0]
        if YT_ID_RE.match(base):
            return base
    for part in parsed.path.split("/"):
        if YT_ID_RE.match(part):
            return part
    return ""


def soundcloud_slug(text):
    if not text:
        return ""
    norm = unicodedata.normalize("NFKD", text)
    norm = norm.encode("ascii", "ignore").decode()
    norm = norm.lower()
    norm = re.sub(r"[^a-z0-9]+", "-", norm).strip("-")
    return norm


def soundcloud_track_slug_from_url(url):
    if not url:
        return ""
    m = re.match(r"^https?://(www\.)?soundcloud\.com/[^/]+/([^/?#]+)", url)
    if not m:
        return ""
    return m.group(2) or ""


def guess_soundcloud_link(artist, title):
    if isinstance(artist, list):
        artist = artist[0] if artist else ""
    if not artist or not title:
        return ""
    a = soundcloud_slug(artist)
    t = soundcloud_slug(title)
    if not a or not t:
        return ""
    return f"https://soundcloud.com/{a}/{t}"


def extract_soundcloud_url(file_url):
    if not file_url:
        return ""
    if file_url.startswith("plugin://plugin.audio.soundcloud/play/"):
        try:
            parsed = urlparse(file_url)
            qs = parse_qs(parsed.query)
            raw = (qs.get("url") or [""])[0]
            if raw:
                clean = re.sub(r"\?.*$", "", unquote(raw))
                if SC.match(clean):
                    return clean
                return unquote(raw)
        except Exception:
            return ""
    return ""


def read_soundcloud_client_id():
    global SC_CLIENT_ID_CACHE, SC_CLIENT_ID_TS
    now = time.time()
    if SC_CLIENT_ID_CACHE and now - SC_CLIENT_ID_TS < 300:
        return SC_CLIENT_ID_CACHE
    env_id = os.environ.get("SC_CLIENT_ID")
    if env_id:
        SC_CLIENT_ID_CACHE = env_id.strip()
        SC_CLIENT_ID_TS = now
        return SC_CLIENT_ID_CACHE
    path = os.environ.get(
        "SC_CLIENT_ID_FILE",
        "/storage/.kodi/userdata/addon_data/plugin.audio.soundcloud/cache/api-client-id",
    )
    try:
        with open(path, "r") as f:
            SC_CLIENT_ID_CACHE = f.read().strip()
            SC_CLIENT_ID_TS = now
            return SC_CLIENT_ID_CACHE
    except Exception:
        return ""


def extract_soundcloud_track_id(text):
    if not text:
        return ""
    m = re.search(r"soundcloud:tracks:(\d+)", text)
    if m:
        return m.group(1)
    m = re.search(r"/tracks/(\d+)", text)
    if m:
        return m.group(1)
    return ""


def normalize_channel_name(name):
    if not name:
        return ""
    return re.sub(r"\s+", " ", name).strip().casefold()


def read_radio_stream_map():
    raw = os.environ.get("RADIO_STREAM_MAP", "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for k, v in data.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        nk = normalize_channel_name(k)
        vv = v.strip()
        if not nk or not vv.startswith(("http://", "https://")):
            continue
        out[nk] = vv
    return out


def read_radio_stream_map_from_m3u(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [line.strip() for line in f]
    except Exception:
        return {}
    out = {}
    last_inf = ""
    for line in lines:
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            last_inf = line
            continue
        if line.startswith("#"):
            continue
        url = line
        if not url.startswith(("http://", "https://")):
            continue
        name = ""
        if "," in last_inf:
            name = last_inf.rsplit(",", 1)[-1].strip()
        if not name:
            continue
        norm = normalize_channel_name(name)
        if not norm:
            continue
        # Keep first occurrence to stay close to playlist order.
        out.setdefault(norm, url)
    return out


def get_radio_stream_m3u_map():
    global RADIO_M3U_MAP_CACHE
    if RADIO_M3U_MAP_CACHE is None:
        RADIO_M3U_MAP_CACHE = read_radio_stream_map_from_m3u(RADIO_M3U_PATH)
    return RADIO_M3U_MAP_CACHE


def get_radio_stream_url(channel):
    global RADIO_STREAM_MAP_CACHE
    if RADIO_STREAM_MAP_CACHE is None:
        RADIO_STREAM_MAP_CACHE = read_radio_stream_map()
    key = normalize_channel_name(channel)
    if not key:
        return ""
    # Env mapping wins, M3U mapping is fallback.
    hit = RADIO_STREAM_MAP_CACHE.get(key, "")
    if hit:
        return hit
    return get_radio_stream_m3u_map().get(key, "")


def get_cached_icy_title(stream_url):
    if not stream_url:
        return ""
    hit = ICY_TITLE_CACHE.get(stream_url)
    if not hit:
        return ""
    title, ts = hit
    if time.time() - ts > ICY_TITLE_TTL:
        ICY_TITLE_CACHE.pop(stream_url, None)
        return ""
    return title


def cache_icy_title(stream_url, title):
    if not stream_url or not title:
        return
    ICY_TITLE_CACHE[stream_url] = (title, time.time())


def fetch_icy_title(stream_url):
    if not stream_url or not stream_url.startswith(("http://", "https://")):
        return ""
    cached = get_cached_icy_title(stream_url)
    if cached:
        return cached
    try:
        headers = {"Icy-MetaData": "1", "User-Agent": "KodiMediaBot/1.0"}
        with HTTP.get(stream_url, headers=headers, stream=True, timeout=ICY_TIMEOUT, allow_redirects=True) as resp:
            if not resp.ok:
                return ""
            metaint = resp.headers.get("icy-metaint")
            if not metaint:
                return ""
            raw = resp.raw
            raw.read(int(metaint))
            block_len = raw.read(1)
            if not block_len:
                return ""
            n = block_len[0] * 16
            if n <= 0:
                return ""
            meta = raw.read(n).decode("utf-8", errors="ignore")
            m = re.search(r"StreamTitle='([^']*)';", meta)
            title = (m.group(1).strip() if m else "")
            if title:
                cache_icy_title(stream_url, title)
            return title
    except Exception:
        return ""


def get_cached_youtube_link(query_key):
    if not query_key:
        return None
    hit = YT_SEARCH_CACHE.get(query_key)
    if not hit:
        return None
    link, ts = hit
    ttl = YT_SEARCH_TTL if link else YT_SEARCH_FAIL_TTL
    if time.time() - ts > ttl:
        YT_SEARCH_CACHE.pop(query_key, None)
        return None
    return link


def cache_youtube_link(query_key, link):
    if not query_key:
        return
    YT_SEARCH_CACHE[query_key] = (link or "", time.time())


def get_cached_soundcloud_link(query_key):
    if not query_key:
        return None
    hit = SC_SEARCH_CACHE.get(query_key)
    if not hit:
        return None
    link, ts = hit
    ttl = SC_SEARCH_TTL if link else SC_SEARCH_FAIL_TTL
    if time.time() - ts > ttl:
        SC_SEARCH_CACHE.pop(query_key, None)
        return None
    return link


def cache_soundcloud_link(query_key, link):
    if not query_key:
        return
    SC_SEARCH_CACHE[query_key] = (link or "", time.time())


def search_youtube_link(query, expected_title=""):
    if not query:
        return ""
    query_key = normalize_title(query)
    cached = get_cached_youtube_link(query_key)
    if cached is not None:
        return cached
    cmd = [
        "yt-dlp",
        "--skip-download",
        "--print",
        "%(id)s\t%(title)s",
        f"ytsearch1:{query}",
    ]
    try:
        res = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=YT_SEARCH_TIMEOUT,
        )
        out = (res.stdout or "").strip().splitlines()
        link = ""
        if out:
            first = out[0].strip()
            vid, sep, result_title = first.partition("\t")
            vid = vid.strip()
            result_title = result_title.strip()
            if YT_ID_RE.match(vid):
                candidate = f"https://youtu.be/{vid}"
                if not expected_title or youtube_result_matches_radio_track(expected_title, result_title):
                    link = candidate
        cache_youtube_link(query_key, link)
        return link
    except Exception:
        cache_youtube_link(query_key, "")
        return ""


def search_soundcloud_link(query, expected_title=""):
    if not query:
        return ""
    query_key = normalize_title(query)
    cached = get_cached_soundcloud_link(query_key)
    if cached is not None:
        return cached
    cmd = [
        "yt-dlp",
        "--skip-download",
        "--print",
        "%(webpage_url)s\t%(uploader)s\t%(title)s",
        f"scsearch1:{query}",
    ]
    try:
        res = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=SC_SEARCH_TIMEOUT,
        )
        out = (res.stdout or "").strip().splitlines()
        link = ""
        if out:
            first = out[0].strip()
            page_url, sep, rest = first.partition("\t")
            page_url = re.sub(r"\?.*$", "", page_url.strip())
            uploader = ""
            result_title = ""
            if sep:
                uploader, sep2, result_title = rest.partition("\t")
                uploader = uploader.strip()
                result_title = result_title.strip()
            if SC.match(page_url):
                if not expected_title or soundcloud_result_matches_radio_track(expected_title, result_title, uploader):
                    link = page_url
        cache_soundcloud_link(query_key, link)
        return link
    except Exception:
        cache_soundcloud_link(query_key, "")
        return ""


def radio_title_to_youtube_link(track_title):
    if not track_title:
        return ""
    clean = normalize_radio_track_title(track_title)
    if " - " in clean:
        query = f"{clean} official audio"
    else:
        query = clean or track_title
    return search_youtube_link(query, expected_title=clean or track_title)


def radio_title_to_soundcloud_link(track_title):
    if not track_title:
        return ""
    clean = normalize_radio_track_title(track_title)
    query = clean or track_title
    return search_soundcloud_link(query, expected_title=clean or track_title)


def resolve_radio_title(channel, fallback_title=""):
    stream_url = get_radio_stream_url(channel)
    if not stream_url:
        return "", ""
    title = fetch_icy_title(stream_url)
    if not title:
        return "", stream_url
    if fallback_title and normalize_title(title) == normalize_title(fallback_title):
        return "", stream_url
    yt_link = radio_title_to_youtube_link(title)
    if yt_link:
        return title, yt_link
    sc_link = radio_title_to_soundcloud_link(title)
    return title, sc_link or stream_url


def get_cached_soundcloud_permalink(track_id):
    if not track_id:
        return ""
    hit = SC_PERMALINK_CACHE.get(track_id)
    if not hit:
        return ""
    url, ts = hit
    if time.time() - ts > SC_PERMALINK_TTL:
        SC_PERMALINK_CACHE.pop(track_id, None)
        return ""
    return url


def cache_soundcloud_permalink(track_id, url):
    if not track_id or not url:
        return
    SC_PERMALINK_CACHE[track_id] = (url, time.time())


def fetch_soundcloud_permalink(track_id):
    if not track_id:
        return ""
    cached = get_cached_soundcloud_permalink(track_id)
    if cached:
        return cached
    client_id = read_soundcloud_client_id()
    if not client_id:
        return ""
    api_url = f"https://api-v2.soundcloud.com/tracks/{track_id}?client_id={client_id}"
    try:
        resp = HTTP.get(api_url, timeout=6)
        if not resp.ok:
            return ""
        data = resp.json() or {}
        url = data.get("permalink_url") or ""
        if url:
            cache_soundcloud_permalink(track_id, url)
        return url
    except Exception:
        return ""


def maybe_cache_soundcloud_url(file_url):
    global LAST_WS_SC_URL
    sc_url = extract_soundcloud_url(file_url)
    if sc_url:
        LAST_WS_SC_URL = sc_url


def resolve_soundcloud_link_from_kodi():
    global LAST_WS_SC_TRACK_ID, LAST_WS_SC_URL
    pid = get_active_playerid()
    if pid is None:
        return ""
    cur_title = ""
    try:
        item = kodi_call(
            "Player.GetItem",
            {"playerid": pid, "properties": ["file"]},
        ).get("result", {}).get("item", {})
        cur_title = item.get("title") or item.get("label") or ""
        file_url = item.get("file") or ""
        sc = extract_soundcloud_url(file_url)
        if sc:
            return sc
        track_id = extract_soundcloud_track_id(file_url)
        if track_id:
            link = fetch_soundcloud_permalink(track_id)
            if link:
                LAST_WS_SC_TRACK_ID = track_id
                LAST_WS_SC_URL = link
                return link
    except Exception:
        pass
    try:
        res = kodi_call(
            "Playlist.GetItems",
            {"playlistid": 0, "properties": ["file"]},
        )
        items = res.get("result", {}).get("items", []) or []
        want = normalize_title(cur_title)
        matched = None
        for it in items:
            if want:
                label = it.get("label") or it.get("title") or ""
                if normalize_title(label) == want:
                    matched = it
                    break
        if matched is not None:
            items = [matched]
        for it in items:
            file_url = it.get("file") or ""
            sc = extract_soundcloud_url(file_url)
            if sc:
                return sc
            if "media_url=" in file_url:
                try:
                    qs = parse_qs(urlparse(file_url).query)
                    media_url = (qs.get("media_url") or [""])[0]
                except Exception:
                    media_url = ""
                track_id = extract_soundcloud_track_id(media_url)
                if track_id:
                    link = fetch_soundcloud_permalink(track_id)
                    if link:
                        LAST_WS_SC_TRACK_ID = track_id
                        LAST_WS_SC_URL = link
                        return link
    except Exception:
        pass
    return ""


def schedule_soundcloud_permalink_probe(timeout_s=2.0, interval_s=0.2):
    global LAST_WS_SC_PROBE_TS, LAST_WS_SC_PROBE_ACTIVE, LAST_WS_SC_URL, LAST_WS_SC_TRACK_ID
    now = time.time()
    if LAST_WS_SC_PROBE_ACTIVE and now - LAST_WS_SC_PROBE_TS < timeout_s:
        return
    LAST_WS_SC_PROBE_TS = now
    LAST_WS_SC_PROBE_ACTIVE = True

    def _run():
        global LAST_WS_SC_PROBE_ACTIVE, LAST_WS_SC_URL, LAST_WS_SC_TRACK_ID
        end = time.time() + timeout_s
        while time.time() < end:
            try:
                pid = get_active_playerid()
                if pid is None:
                    time.sleep(interval_s)
                    continue
                item = kodi_call(
                    "Player.GetItem",
                    {"playerid": pid, "properties": ["file"]},
                ).get("result", {}).get("item", {})
                file_url = item.get("file") or ""
                sc = extract_soundcloud_url(file_url)
                if sc:
                    LAST_WS_SC_URL = sc
                    break
                track_id = extract_soundcloud_track_id(file_url)
                if track_id:
                    link = fetch_soundcloud_permalink(track_id)
                    if link:
                        LAST_WS_SC_URL = link
                        LAST_WS_SC_TRACK_ID = track_id
                        break
            except Exception:
                pass
            time.sleep(interval_s)
        LAST_WS_SC_PROBE_ACTIVE = False

    threading.Thread(target=_run, daemon=True).start()


def external_item_display(item):
    global LAST_WS_SC_LOOKUP_TS, LAST_WS_SC_URL, LAST_WS_SC_TRACK_ID
    if not item:
        if DEBUG_WS:
            print("EXT ITEM display: empty item", flush=True)
        return None, None
    itype = (item.get("type") or "").lower()
    title = item.get("title") or ""
    label = item.get("label") or ""
    imdbnumber = item.get("imdbnumber") or ""
    uniqueid = item.get("uniqueid") or {}
    imdb_id = ""
    if isinstance(uniqueid, dict):
        imdb_id = uniqueid.get("imdb") or ""
    file_url = item.get("file") or ""
    showtitle = item.get("showtitle") or ""
    season = item.get("season")
    episode = item.get("episode")
    artist = item.get("artist") or []
    album = item.get("album") or ""
    channel = item.get("channel") or ""

    link = None
    if not link and file_url.startswith("plugin://plugin.video.youtube/"):
        yt_id = extract_youtube_id(file_url)
        if yt_id:
            link = f"https://youtu.be/{yt_id}"
    yt_id_from_file = extract_youtube_id(file_url) if file_url else ""
    if yt_id_from_file and "/youtube/manifest/" in file_url:
        link = f"https://youtu.be/{yt_id_from_file}"
    sc_from_plugin = extract_soundcloud_url(file_url)
    if sc_from_plugin:
        link = sc_from_plugin
    if file_url.startswith("http"):
        link = file_url
        yt_id = extract_youtube_id(link)
        if yt_id:
            link = f"https://youtu.be/{yt_id}"
        elif "sndcdn" in link:
            track_id = extract_soundcloud_track_id(file_url)
            if LAST_WS_SC_URL and LAST_WS_SC_TRACK_ID and track_id and track_id == LAST_WS_SC_TRACK_ID:
                link = LAST_WS_SC_URL
                return label or title or None, link
            schedule_soundcloud_permalink_probe()
            now = time.time()
            if now - LAST_WS_SC_LOOKUP_TS > 2.0:
                LAST_WS_SC_LOOKUP_TS = now
                sc = resolve_soundcloud_link_from_kodi()
                if sc:
                    LAST_WS_SC_URL = sc
                    link = sc
                    return label or title or None, link
            sc_link = guess_soundcloud_link(artist, title)
            link = sc_link or None
        elif "/youtube/manifest/" in link and ("127.0.0.1" in link or "localhost" in link):
            yt_id = extract_youtube_id(link)
            if yt_id:
                link = f"https://youtu.be/{yt_id}"
            else:
                link = None
    if not link and itype in ("video", "movie") and LAST_WS_YT_ID:
        if "youtube" in (file_url or "") or "manifest" in (file_url or ""):
            link = f"https://youtu.be/{LAST_WS_YT_ID}"
    if link and ("youtu" in link or "soundcloud" in link):
        pass
    elif imdbnumber and IMDB_ID_RE.match(imdbnumber):
        link = f"https://www.imdb.com/title/{imdbnumber}/"
    elif imdb_id and IMDB_ID_RE.match(imdb_id):
        link = f"https://www.imdb.com/title/{imdb_id}/"
    elif itype in ("movie", "episode", "tvshow"):
        q = showtitle or title or label
        if q:
            link = f"https://www.imdb.com/find?q={quote_plus(q)}"

    if itype == "episode":
        base = showtitle or label or title
        ep_title = title or ""
        if base:
            if isinstance(season, int) and isinstance(episode, int):
                return f"{base} S{season:02d}E{episode:02d} – {ep_title}".strip(" –"), link
            return f"{base} – {ep_title}".strip(" –"), link
    if itype == "movie":
        return title or label or "Unknown", link
    if artist and title:
        return f"{', '.join(artist)} - {title}", link
    if album and title:
        return f"{album} - {title}", link
    if itype == "channel" and channel:
        radio_title, radio_link = resolve_radio_title(channel, fallback_title=channel)
        if not link and radio_link:
            link = radio_link
        if radio_title:
            return f"{channel} || {radio_title}", link
    if channel:
        return channel, link
    return label or title or None, link


# Check whether a Kodi item matches a queue item.
def kodi_item_matches_queue(item, qitem):
    if not item or not qitem:
        return False
    item_file = item.get("file") or ""
    q_url = qitem.get("url") or ""
    if item_file and q_url and item_file == q_url:
        return True
    q_link = qitem.get("link") or ""
    if q_link and "soundcloud.com" in q_link:
        if item_file and "sndcdn" in item_file:
            return True
        if LAST_WS_SC_URL and LAST_WS_SC_URL == q_link:
            return True
        item_title = item.get("title") or item.get("label") or ""
        q_slug = soundcloud_track_slug_from_url(q_link)
        t_slug = soundcloud_slug(item_title)
        if q_slug and t_slug and (q_slug == t_slug or q_slug in t_slug or t_slug in q_slug):
            return True
    item_name = normalize_title(kodi_item_name(item))
    q_title = normalize_title(qitem.get("title") or "")
    if not item_name or not q_title:
        return False
    return item_name in q_title or q_title in item_name


def fetch_library_item(item_type, item_id):
    if not item_type or item_id is None:
        return {}
    itype = item_type.lower()
    if itype == "movie":
        res = kodi_call_with_props(
            "VideoLibrary.GetMovieDetails",
            "movieid",
            item_id,
            ["title", "year", "originaltitle", "uniqueid", "imdbnumber"],
        )
        if DEBUG_WS and res.get("error"):
            print(f"LIB FETCH movie error={res.get('error')} id={item_id}", flush=True)
        return (res.get("result", {}) or {}).get("moviedetails", {}) or {}
    if itype == "episode":
        res = kodi_call_with_props(
            "VideoLibrary.GetEpisodeDetails",
            "episodeid",
            item_id,
            ["title", "showtitle", "season", "episode", "uniqueid", "imdbnumber"],
        )
        if DEBUG_WS and res.get("error"):
            print(f"LIB FETCH episode error={res.get('error')} id={item_id}", flush=True)
        return (res.get("result", {}) or {}).get("episodedetails", {}) or {}
    if itype == "tvshow":
        res = kodi_call_with_props(
            "VideoLibrary.GetTVShowDetails",
            "tvshowid",
            item_id,
            ["title", "year", "uniqueid", "imdbnumber"],
        )
        if DEBUG_WS and res.get("error"):
            print(f"LIB FETCH tvshow error={res.get('error')} id={item_id}", flush=True)
        return (res.get("result", {}) or {}).get("tvshowdetails", {}) or {}
    return {}


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


# Listen for Kodi playback events via WebSocket.
async def kodi_ws_listener():
    global KODI_WS_URL, WS_PLAYING, WS_LAST_EVENT_TS, WS_CONNECTED, WS_STATE
    global LAST_WS_YT_ID, LAST_WS_PLAYING_FILE, LAST_WS_PLAYERID
    if KODI_WS_URL is None:
        KODI_WS_URL = f"ws://{KODI_HOST}:{KODI_WS_PORT}/jsonrpc"
    while True:
        try:
            async with websockets.connect(KODI_WS_URL, ping_interval=20, ping_timeout=20) as ws:
                WS_CONNECTED = True
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    method = msg.get("method")
                    if DEBUG_WS and method:
                        print(f"WS EVENT method={method} msg={msg}", flush=True)
                    if method == "Other.playback_init":
                        data = msg.get("params", {}).get("data", {}) or {}
                        vid = data.get("video_id") or ""
                        playing_file = data.get("playing_file") or ""
                        if vid:
                            LAST_WS_YT_ID = vid
                        if playing_file:
                            LAST_WS_PLAYING_FILE = playing_file
                    if method in ("Player.OnPlay", "Player.OnAVStart"):
                        qs = get_queue_state_module()
                        now = time.time()
                        WS_PLAYING = True
                        WS_STATE = "playing"
                        WS_LAST_EVENT_TS = now
                        data = msg.get("params", {}).get("data", {}) or {}
                        player_params = data.get("player", {}) or {}
                        item_params = data.get("item", {}) or {}
                        if "playerid" in player_params:
                            LAST_WS_PLAYERID = player_params.get("playerid")
                        if any(k in item_params for k in ("id", "type", "title")):
                            LAST_WS_ITEM.clear()
                            for k in ("id", "type", "title"):
                                if k in item_params:
                                    LAST_WS_ITEM[k] = item_params.get(k)
                        if qs.BOT_EXPECTING_WS > 0:
                            qs.BOT_EXPECTING_WS -= 1
                            if DEBUG_WS:
                                print(
                                    f"WS EXPECT dec method={method} remaining={qs.BOT_EXPECTING_WS}",
                                    flush=True,
                                )
                        else:
                            player = data.get("player", {}) or {}
                            pid = player.get("playerid")
                            item = None
                            if pid is not None:
                                item = (await kodi_call_async(
                                    "Player.GetItem",
                                    {"playerid": pid, "properties": ["title", "artist", "file", "type", "label"]},
                                )).get("result", {}).get("item", {})
                            with qs.LOCK:
                                if qs.DISPLAY_INDEX is not None and 0 <= qs.DISPLAY_INDEX < len(qs.QUEUE):
                                    qitem = qs.QUEUE[qs.DISPLAY_INDEX]
                                else:
                                    qitem = None
                            if not kodi_item_matches_queue(item, qitem):
                                print(
                                    "WS MISMATCH clear_bot_playback_state "
                                    f"item_file={(item or {}).get('file')} "
                                    f"item_title={(item or {}).get('title')} "
                                    f"q_url={(qitem or {}).get('url')} "
                                    f"q_title={(qitem or {}).get('title')} "
                                    f"q_link={(qitem or {}).get('link')}",
                                    flush=True,
                                )
                                qs.clear_bot_playback_state()
                                qs.schedule_now_playing_refresh()
                        qs.schedule_playback_refresh()
                    elif method == "Player.OnPause":
                        qs = get_queue_state_module()
                        WS_PLAYING = False
                        WS_STATE = "paused"
                        WS_LAST_EVENT_TS = time.time()
                        qs.schedule_now_playing_refresh()
                    elif method == "Player.OnResume":
                        qs = get_queue_state_module()
                        WS_PLAYING = True
                        WS_STATE = "playing"
                        WS_LAST_EVENT_TS = time.time()
                        qs.schedule_now_playing_refresh()
                    elif method == "Player.OnStop":
                        qs = get_queue_state_module()
                        WS_PLAYING = False
                        WS_STATE = "stopped"
                        WS_LAST_EVENT_TS = time.time()
                        qs.schedule_now_playing_refresh()
        except Exception:
            WS_CONNECTED = False
            WS_STATE = "unknown"
            await asyncio.sleep(3)
