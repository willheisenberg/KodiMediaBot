import json
import logging
import re
import subprocess
import threading
import time
import unicodedata
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from kodibot.core import kodi_api as KA


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


def kodi_time_seconds(t):
    if not t:
        return None
    return t.get("hours", 0) * 3600 + t.get("minutes", 0) * 60 + t.get("seconds", 0)


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


def kodi_item_name(item):
    if not item:
        return ""
    artists = item.get("artist") or []
    title = item.get("title") or ""
    label = item.get("label") or ""
    if artists and title:
        return f"{', '.join(artists)} - {title}"
    return label or title or ""


def extract_youtube_id(url):
    if not url:
        return ""
    m = KA.YT.search(url)
    if m:
        return m.group(1)
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    qs = parse_qs(parsed.query)
    vid_param = (qs.get("video_id") or [""])[0]
    if vid_param and KA.YT_ID_RE.match(vid_param):
        return vid_param
    file_param = (qs.get("file") or [""])[0]
    if file_param:
        base = file_param.split("/")[-1]
        if "." in base:
            base = base.split(".", 1)[0]
        if KA.YT_ID_RE.match(base):
            return base
    for part in parsed.path.split("/"):
        if KA.YT_ID_RE.match(part):
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


def soundcloud_display_title_from_url(url):
    if not url:
        return ""
    m = re.match(r"^https?://(www\.)?soundcloud\.com/([^/]+)/([^/?#]+)", url)
    if not m:
        return ""
    artist = unquote(m.group(2) or "").replace("-", " ").strip()
    track = unquote(m.group(3) or "").replace("-", " ").strip()
    if artist and track:
        return f"{artist} - {track}"
    return artist or track


def is_soundcloud_stream_url(url):
    if not isinstance(url, str):
        return False
    return "sndcdn" in url or "media-streaming.soundcloud.cloud" in url


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
                if KA.SC.match(clean):
                    return clean
                return unquote(raw)
        except Exception:
            return ""
    return ""


def read_soundcloud_client_id():
    now = time.time()
    if KA.SC_CLIENT_ID_CACHE and now - KA.SC_CLIENT_ID_TS < 300:
        return KA.SC_CLIENT_ID_CACHE
    if KA.CFG.sc_client_id:
        KA.SC_CLIENT_ID_CACHE = KA.CFG.sc_client_id
        KA.SC_CLIENT_ID_TS = now
        return KA.SC_CLIENT_ID_CACHE
    path = KA.CFG.sc_client_id_file
    try:
        with open(path, "r") as f:
            KA.SC_CLIENT_ID_CACHE = f.read().strip()
            KA.SC_CLIENT_ID_TS = now
            return KA.SC_CLIENT_ID_CACHE
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
    raw = KA.CFG.radio_stream_map_raw
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
        out.setdefault(norm, url)
    return out


def get_radio_stream_m3u_map():
    if KA.RADIO_M3U_MAP_CACHE is None:
        KA.RADIO_M3U_MAP_CACHE = read_radio_stream_map_from_m3u(KA.CFG.radio_m3u_path)
    return KA.RADIO_M3U_MAP_CACHE


def get_radio_stream_url(channel):
    if KA.RADIO_STREAM_MAP_CACHE is None:
        KA.RADIO_STREAM_MAP_CACHE = read_radio_stream_map()
    key = normalize_channel_name(channel)
    if not key:
        return ""
    hit = KA.RADIO_STREAM_MAP_CACHE.get(key, "")
    if hit:
        return hit
    return get_radio_stream_m3u_map().get(key, "")


def get_cached_icy_title(stream_url):
    if not stream_url:
        return ""
    hit = KA.ICY_TITLE_CACHE.get(stream_url)
    if not hit:
        return ""
    title, ts = hit
    if time.time() - ts > KA.CFG.icy_title_ttl:
        KA.ICY_TITLE_CACHE.pop(stream_url, None)
        return ""
    return title


def cache_icy_title(stream_url, title):
    if not stream_url or not title:
        return
    KA.ICY_TITLE_CACHE[stream_url] = (title, time.time())


def fetch_icy_title(stream_url):
    if not stream_url or not stream_url.startswith(("http://", "https://")):
        return ""
    cached = get_cached_icy_title(stream_url)
    if cached:
        return cached
    try:
        headers = {"Icy-MetaData": "1", "User-Agent": "KodiMediaBot/1.0"}
        with KA.HTTP.get(stream_url, headers=headers, stream=True, timeout=KA.CFG.icy_timeout, allow_redirects=True) as resp:
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
    hit = KA.YT_SEARCH_CACHE.get(query_key)
    if not hit:
        return None
    link, ts = hit
    ttl = KA.CFG.yt_search_ttl if link else KA.CFG.yt_search_fail_ttl
    if time.time() - ts > ttl:
        KA.YT_SEARCH_CACHE.pop(query_key, None)
        return None
    return link


def cache_youtube_link(query_key, link):
    if not query_key:
        return
    KA.YT_SEARCH_CACHE[query_key] = (link or "", time.time())


def get_cached_soundcloud_link(query_key):
    if not query_key:
        return None
    hit = KA.SC_SEARCH_CACHE.get(query_key)
    if not hit:
        return None
    link, ts = hit
    ttl = KA.CFG.sc_search_ttl if link else KA.CFG.sc_search_fail_ttl
    if time.time() - ts > ttl:
        KA.SC_SEARCH_CACHE.pop(query_key, None)
        return None
    return link


def cache_soundcloud_link(query_key, link):
    if not query_key:
        return
    KA.SC_SEARCH_CACHE[query_key] = (link or "", time.time())


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
            timeout=KA.CFG.yt_search_timeout,
        )
        out = (res.stdout or "").strip().splitlines()
        link = ""
        if out:
            first = out[0].strip()
            vid, sep, result_title = first.partition("\t")
            vid = vid.strip()
            result_title = result_title.strip()
            if KA.YT_ID_RE.match(vid):
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
            timeout=KA.CFG.sc_search_timeout,
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
            if KA.SC.match(page_url):
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
    hit = KA.SC_PERMALINK_CACHE.get(track_id)
    if not hit:
        return ""
    url, ts = hit
    if time.time() - ts > KA.SC_PERMALINK_TTL:
        KA.SC_PERMALINK_CACHE.pop(track_id, None)
        return ""
    return url


def cache_soundcloud_permalink(track_id, url):
    if not track_id or not url:
        return
    KA.SC_PERMALINK_CACHE[track_id] = (url, time.time())


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
        resp = KA.HTTP.get(api_url, timeout=6)
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
    sc_url = extract_soundcloud_url(file_url)
    if not sc_url and isinstance(file_url, str):
        clean = re.sub(r"\?.*$", "", file_url.strip())
        if KA.SC.match(clean):
            sc_url = clean
    if sc_url:
        KA.LAST_WS_SC_URL = sc_url


def resolve_soundcloud_link_from_kodi():
    pid = KA.get_active_playerid()
    if pid is None:
        return ""
    cur_title = ""
    try:
        item = KA.kodi_call(
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
                KA.LAST_WS_SC_TRACK_ID = track_id
                KA.LAST_WS_SC_URL = link
                return link
    except Exception:
        pass
    try:
        res = KA.kodi_call(
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
                        KA.LAST_WS_SC_TRACK_ID = track_id
                        KA.LAST_WS_SC_URL = link
                        return link
    except Exception:
        pass
    return ""


def schedule_soundcloud_permalink_probe(timeout_s=2.0, interval_s=0.2):
    now = time.time()
    if KA.LAST_WS_SC_PROBE_ACTIVE and now - KA.LAST_WS_SC_PROBE_TS < timeout_s:
        return
    KA.LAST_WS_SC_PROBE_TS = now
    KA.LAST_WS_SC_PROBE_ACTIVE = True

    def _run():
        end = time.time() + timeout_s
        while time.time() < end:
            try:
                pid = KA.get_active_playerid()
                if pid is None:
                    time.sleep(interval_s)
                    continue
                item = KA.kodi_call(
                    "Player.GetItem",
                    {"playerid": pid, "properties": ["file"]},
                ).get("result", {}).get("item", {})
                file_url = item.get("file") or ""
                sc = extract_soundcloud_url(file_url)
                if sc:
                    KA.LAST_WS_SC_URL = sc
                    break
                track_id = extract_soundcloud_track_id(file_url)
                if track_id:
                    link = fetch_soundcloud_permalink(track_id)
                    if link:
                        KA.LAST_WS_SC_URL = link
                        KA.LAST_WS_SC_TRACK_ID = track_id
                        break
            except Exception:
                pass
            time.sleep(interval_s)
        KA.LAST_WS_SC_PROBE_ACTIVE = False

    threading.Thread(target=_run, daemon=True).start()


def external_item_display(item):
    if not item:
        if KA.log.isEnabledFor(logging.DEBUG):
            KA.log.debug("External item display: empty item")
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
    temp_title = KA.media.get_temp_media_title(file_url)
    if temp_title:
        return temp_title, file_url
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
        elif is_soundcloud_stream_url(link):
            if KA.LAST_WS_SC_URL:
                link = KA.LAST_WS_SC_URL
                display_name = label or title
                if display_name and display_name.casefold() != "playlist.m3u8":
                    return display_name, link
                fallback_name = soundcloud_display_title_from_url(KA.LAST_WS_SC_URL)
                if fallback_name:
                    return fallback_name, link
            track_id = extract_soundcloud_track_id(file_url)
            if KA.LAST_WS_SC_URL and KA.LAST_WS_SC_TRACK_ID and track_id and track_id == KA.LAST_WS_SC_TRACK_ID:
                link = KA.LAST_WS_SC_URL
                return label or title or None, link
            schedule_soundcloud_permalink_probe()
            now = time.time()
            if now - KA.LAST_WS_SC_LOOKUP_TS > 2.0:
                KA.LAST_WS_SC_LOOKUP_TS = now
                sc = resolve_soundcloud_link_from_kodi()
                if sc:
                    KA.LAST_WS_SC_URL = sc
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
    if not link and itype in ("video", "movie") and KA.LAST_WS_YT_ID:
        if "youtube" in (file_url or "") or "manifest" in (file_url or ""):
            link = f"https://youtu.be/{KA.LAST_WS_YT_ID}"
    if link and ("youtu" in link or "soundcloud" in link):
        pass
    elif imdbnumber and KA.IMDB_ID_RE.match(imdbnumber):
        link = f"https://www.imdb.com/title/{imdbnumber}/"
    elif imdb_id and KA.IMDB_ID_RE.match(imdb_id):
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

    # FALLBACK: If we have no good title/label yet, check if this file URL is in our favourites
    if not (artist and title) and not channel and file_url:
        fav_name = KA.find_favourite_label_by_path(file_url)
        if fav_name:
            # Try to fetch current song title from the stream (ICY metadata)
            radio_title = KA.fetch_icy_title(file_url)
            if radio_title:
                # Search for YouTube/SoundCloud links if a song title is found
                yt_link = KA.radio_title_to_youtube_link(radio_title)
                if yt_link:
                    return f"{fav_name} || {radio_title}", yt_link
                sc_link = KA.radio_title_to_soundcloud_link(radio_title)
                return f"{fav_name} || {radio_title}", (sc_link or link)
            
            return fav_name, link

    return label or title or None, link




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
        item_sc_url = extract_soundcloud_url(item_file)
        if item_sc_url and item_sc_url == q_link:
            return True
        if KA.LAST_WS_SC_URL and KA.LAST_WS_SC_URL == q_link and is_soundcloud_stream_url(item_file):
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
        res = KA.kodi_call_with_props(
            "VideoLibrary.GetMovieDetails",
            "movieid",
            item_id,
            ["title", "year", "originaltitle", "uniqueid", "imdbnumber"],
        )
        if res.get("error"):
            KA.log.debug(f"Library fetch: movie error={res.get('error')} id={item_id}")
        return (res.get("result", {}) or {}).get("moviedetails", {}) or {}
    if itype == "episode":
        res = KA.kodi_call_with_props(
            "VideoLibrary.GetEpisodeDetails",
            "episodeid",
            item_id,
            ["title", "showtitle", "season", "episode", "uniqueid", "imdbnumber"],
        )
        if res.get("error"):
            KA.log.debug(f"Library fetch: episode error={res.get('error')} id={item_id}")
        return (res.get("result", {}) or {}).get("episodedetails", {}) or {}
    if itype == "tvshow":
        res = KA.kodi_call_with_props(
            "VideoLibrary.GetTVShowDetails",
            "tvshowid",
            item_id,
            ["title", "year", "uniqueid", "imdbnumber"],
        )
        if res.get("error"):
            KA.log.debug(f"Library fetch: tvshow error={res.get('error')} id={item_id}")
        return (res.get("result", {}) or {}).get("tvshowdetails", {}) or {}
    return {}


def build_imdb_link(item):
    if not isinstance(item, dict):
        return ""

    def _item_imdb_link(obj):
        imdbnumber = obj.get("imdbnumber") or ""
        if KA.IMDB_ID_RE.match(imdbnumber):
            return f"https://www.imdb.com/title/{imdbnumber}/"
        uniqueid = obj.get("uniqueid") or {}
        if isinstance(uniqueid, dict):
            imdb_id = uniqueid.get("imdb") or ""
            if KA.IMDB_ID_RE.match(imdb_id):
                return f"https://www.imdb.com/title/{imdb_id}/"
        return ""

    link = _item_imdb_link(item)
    if link:
        return link

    episodeid = item.get("episodeid")
    if episodeid is not None:
        details = fetch_library_item("episode", episodeid)
        link = _item_imdb_link(details)
        if link:
            return link

    title = item.get("title") or item.get("showtitle") or ""
    if title:
        return f"https://www.imdb.com/find?q={quote_plus(title)}"
    return ""
