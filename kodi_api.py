import os
import re
import threading
import time
import subprocess
import json
import asyncio
import unicodedata
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

YT = re.compile(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})")
PL = re.compile(r"(?:[?&]list=)([A-Za-z0-9_-]+)")
SC = re.compile(r"https?://(www\.)?soundcloud\.com/[^/]+/[^/?#]+")
SC_SET = re.compile(r"https?://(www\.)?soundcloud\.com/[^/]+/sets/[^/?#]+")
SC_SHORT = re.compile(r"https?://on\.soundcloud\.com/[A-Za-z0-9]+")

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


# Send a JSON-RPC request to Kodi and return the response JSON.
def kodi_call(method: str, params: dict | None = None):
    payload = {"jsonrpc": "2.0", "method": method, "id": 1}
    if params:
        payload["params"] = params
    return requests.post(KODI_URL, auth=AUTH, json=payload, timeout=5).json()


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


# Turn the audio system on or off via CEC over SSH.
def run_cec_power(on: bool) -> bool:
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


# Query the audio system power state via CEC.
def get_hifi_power_status():
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
    if vid_param and re.match(r"^[A-Za-z0-9_-]{11}$", vid_param):
        return vid_param
    file_param = (qs.get("file") or [""])[0]
    if file_param:
        base = file_param.split("/")[-1]
        if "." in base:
            base = base.split(".", 1)[0]
        if re.match(r"^[A-Za-z0-9_-]{11}$", base):
            return base
    for part in parsed.path.split("/"):
        if re.match(r"^[A-Za-z0-9_-]{11}$", part):
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
        resp = requests.get(api_url, timeout=6)
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
    if not link and file_url.startswith("plugin://plugin.video.youtube/"):
        yt_id = extract_youtube_id(file_url)
        if yt_id:
            link = f"https://youtu.be/{yt_id}"
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
        if "youtube" in (file_url or "") or "manifest" in (file_url or "") or "youtube" in (LAST_WS_PLAYING_FILE or ""):
            link = f"https://youtu.be/{LAST_WS_YT_ID}"
    if link and ("youtu" in link or "soundcloud" in link):
        pass
    elif imdbnumber and re.match(r"^tt\d+$", imdbnumber):
        link = f"https://www.imdb.com/title/{imdbnumber}/"
    elif imdb_id and re.match(r"^tt\d+$", imdb_id):
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
                        import queue_state as qs
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
                                item = kodi_call(
                                    "Player.GetItem",
                                    {"playerid": pid, "properties": ["title", "artist", "file"]},
                                ).get("result", {}).get("item", {})
                            with qs.LOCK:
                                if qs.DISPLAY_INDEX is not None and 0 <= qs.DISPLAY_INDEX < len(qs.QUEUE):
                                    qitem = qs.QUEUE[qs.DISPLAY_INDEX]
                                else:
                                    qitem = None
                            if not kodi_item_matches_queue(item, qitem):
                                qs.clear_bot_playback_state()
                                qs.schedule_now_playing_refresh()
                        qs.schedule_playback_refresh()
                    elif method == "Player.OnPause":
                        WS_PLAYING = False
                        WS_STATE = "paused"
                        WS_LAST_EVENT_TS = time.time()
                        import queue_state as qs
                        qs.schedule_now_playing_refresh()
                    elif method == "Player.OnResume":
                        WS_PLAYING = True
                        WS_STATE = "playing"
                        WS_LAST_EVENT_TS = time.time()
                        import queue_state as qs
                        qs.schedule_now_playing_refresh()
                    elif method == "Player.OnStop":
                        WS_PLAYING = False
                        WS_STATE = "stopped"
                        WS_LAST_EVENT_TS = time.time()
                        import queue_state as qs
                        qs.schedule_now_playing_refresh()
        except Exception:
            WS_CONNECTED = False
            WS_STATE = "unknown"
            await asyncio.sleep(3)
