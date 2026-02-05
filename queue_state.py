import asyncio
import re
import threading
import time
from urllib.parse import unquote

import requests
from pytube import Playlist, YouTube
from yt_dlp import YoutubeDL

import kodi_api

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

LAST_PROGRESS_TS = 0.0
LAST_PROGRESS_TIME = None
LAST_PROGRESS_TOTAL = None
LAST_PROGRESS_INDEX = None

RESUME_ATTEMPTS = {}
RESUME_MAX_ATTEMPTS = 8
RESUME_MIN_REMAINING_SEC = 10
RESUME_SEEK_WAIT_SEC = 20

LIST_DIRTY = False

_SCHEDULE_NOW_PLAYING_REFRESH = None


def set_ui_callbacks(schedule_now_playing_refresh):
    global _SCHEDULE_NOW_PLAYING_REFRESH
    _SCHEDULE_NOW_PLAYING_REFRESH = schedule_now_playing_refresh


def schedule_now_playing_refresh():
    if _SCHEDULE_NOW_PLAYING_REFRESH:
        _SCHEDULE_NOW_PLAYING_REFRESH()


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
                        print(f"RESUME SEEK skip canseek=false playerid={pid} ctx={context}", flush=True)
                        return
                    target_sec = kodi_api.kodi_time_seconds(t)
                    if target_sec is None:
                        print(
                            f"RESUME SEEK skip invalid times playerid={pid} ctx={context} "
                            f"target={t}",
                            flush=True
                        )
                        return
                    print(
                        f"RESUME SEEK playerid={pid} ctx={context} target_sec={target_sec}",
                        flush=True
                    )
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
                print(
                    f"RESUME SEEK waiting for playerid ctx={context} elapsed={elapsed:.1f}s players={players}",
                    flush=True
                )
                last_log_ts = now
            time.sleep(0.3)
        print(f"RESUME SEEK gave up: no playerid ctx={context}", flush=True)
    threading.Thread(target=_seek, daemon=True).start()


# Start playback of a queue item via Kodi.
def play_item(item: dict, resume_time=None):
    global BOT_EXPECTING_WS
    kodi_api.stop_all_players()
    kodi_api.kodi_clear_all_playlists()
    kind = item.get("kind", "video")
    BOT_EXPECTING_WS = 2
    print(
        f"PLAY_ITEM start kind={item.get('kind')} title={item.get('title')} url={item.get('url')}",
        flush=True,
    )

    if kind == "audio":
        playlistid = 0
        kodi_api.maybe_cache_soundcloud_url(item.get("url"))
        kodi_add_to_playlist(item["url"], playlistid)
        res = kodi_api.kodi_call("Player.Open", {"item": {"playlistid": playlistid, "position": 0}})
        print(f"PLAY_ITEM open audio res={res}", flush=True)
        schedule_audio_resolve_and_open(playlistid, resume_time=resume_time)
    else:
        playlistid = 1
        kodi_add_to_playlist(item["url"], playlistid)
        res = kodi_api.kodi_call("Player.Open", {"item": {"playlistid": playlistid}})
        print(f"PLAY_ITEM open video res={res}", flush=True)
        schedule_playback_refresh()
        if resume_time is not None:
            seek_when_player_ready(resume_time, context="video")
    players = kodi_api.get_active_players()
    print(f"PLAY_ITEM active_players={players}", flush=True)


# Start playback and then seek to a saved timestamp.
def resume_item_at_time(item: dict, t):
    if not t:
        play_item(item)
        return
    play_item(item, resume_time=t)


# Stop playback and reset bot playback state.
def hard_stop_and_clear():
    global AUTOPLAY_ENABLED, CURRENT_INDEX, DISPLAY_INDEX, NEXT_INDEX, LAST_PROGRESS_TS, LAST_PROGRESS_TIME, LAST_PROGRESS_TOTAL, LAST_PROGRESS_INDEX, EXTERNAL_PLAYBACK, BOT_EXPECTING_WS
    AUTOPLAY_ENABLED = False
    kodi_api.stop_all_players()
    kodi_api.kodi_clear_all_playlists()
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
def make_item(title, url, kind, link=None):
    return {"title": title, "url": url, "kind": kind, "link": link}


# Fetch a YouTube title and author for display.
def fetch_youtube_title(vid):
    url = f"https://youtu.be/{vid}"
    try:
        yt = YouTube(url)
        author = yt.author or ""
        title = yt.title or ""
        if author and title:
            return f"{author} - {title}"
        if title:
            return title
    except Exception:
        pass
    try:
        oembed = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=6,
        )
        if oembed.ok:
            data = oembed.json()
            author = data.get("author_name", "")
            title = data.get("title", "")
            if author and title:
                return f"{author} - {title}"
            if title:
                return title
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
    m = re.match(r"^https?://(www\.)?soundcloud\.com/([^/]+)/([^/?#]+)", clean_url)
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
        link=clean
    )


# Validate that a SoundCloud URL is a track link.
def is_sc_track_url(url):
    return bool(re.match(r"^https?://(www\.)?soundcloud\.com/[^/]+/[^/?#]+", url)) and "discover/sets" not in url


def is_sc_set_url(url):
    return bool(re.match(r"^https?://(www\.)?soundcloud\.com/[^/]+/sets/[^/?#]+", url)) and "discover/sets" not in url


# Resolve a SoundCloud short link to a full track URL.
def resolve_sc_short(url):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        }
        r = requests.get(url, allow_redirects=True, timeout=8, headers=headers)
        print(f"SC_SHORT RESOLVE start={url} final={r.url} history={[h.url for h in r.history]}", flush=True)
        candidates = [h.url for h in r.history] + [r.url]
        for u in candidates:
            if re.match(r"^https?://(www\.)?soundcloud\.com/", u) and "discover/sets" not in u:
                print(f"SC_SHORT RESOLVE pick={u}", flush=True)
                return u
        m = re.search(r'https?://soundcloud\.com/[^\s"\'<>]+', r.text)
        if m:
            print(f"SC_SHORT RESOLVE html={m.group(0)}", flush=True)
            return m.group(0)
        print("SC_SHORT RESOLVE failed", flush=True)
        return None
    except Exception as e:
        print(f"SC_SHORT RESOLVE error={e}", flush=True)
        return None


# Add a file URL to a Kodi playlist.
def kodi_add_to_playlist(url, playlistid):
    kodi_api.kodi_call(
        "Playlist.Add",
        {"playlistid": playlistid, "item": {"file": url}}
    )


# Start playback of a Kodi playlist.
def kodi_play_playlist(playlistid):
    kodi_api.kodi_call(
        "Player.Open",
        {"item": {"playlistid": playlistid}}
    )


# Poll the Kodi playlist for the resolved SoundCloud stream URL.
def resolve_soundcloud_media_url(playlistid, timeout_s=6.0, interval_s=0.5):
    end = time.time() + timeout_s
    while time.time() < end:
        res = kodi_api.kodi_call(
            "Playlist.GetItems",
            {"playlistid": playlistid, "properties": ["file", "title"]}
        )
        items = res.get("result", {}).get("items", [])
        for it in items:
            f = it.get("file", "")
            if "media_url=" in f:
                return f
        time.sleep(interval_s)
    return None


# Resolve SoundCloud stream URL asynchronously and open it.
def schedule_audio_resolve_and_open(playlistid, resume_time=None):
    def _run():
        url = resolve_soundcloud_media_url(playlistid)
        if not url:
            return
        kodi_api.kodi_call("Player.Open", {"item": {"file": url}})
        kodi_api.kodi_call("Playlist.Clear", {"playlistid": playlistid})
        schedule_playback_refresh()
        if resume_time is not None:
            print("PLAY_ITEM audio stream opened; seeking...", flush=True)
            seek_when_player_ready(resume_time, context="audio")
    threading.Thread(target=_run, daemon=True).start()


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
    pl = Playlist(f"https://www.youtube.com/playlist?list={pid}")
    return [kodi_api.YT.search(v).group(1) for v in pl.video_urls if kodi_api.YT.search(v)]


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
    for vid in vids:
        await queue_video_async(vid)
    mark_list_dirty()
    return len(vids)


# Clear the queue and reset indices.
def clear_queue():
    global CURRENT_INDEX, NEXT_INDEX, LAST_PROGRESS_TS, LAST_PROGRESS_TIME, LAST_PROGRESS_TOTAL, LAST_PROGRESS_INDEX, EXTERNAL_PLAYBACK, BOT_EXPECTING_WS
    with LOCK:
        QUEUE.clear()
        CURRENT_INDEX = None
        NEXT_INDEX = 0
        LAST_PROGRESS_TS = 0.0
        LAST_PROGRESS_TIME = None
        LAST_PROGRESS_TOTAL = None
        LAST_PROGRESS_INDEX = None
        EXTERNAL_PLAYBACK = False
        BOT_EXPECTING_WS = 0
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
    global CURRENT_INDEX, DISPLAY_INDEX, NEXT_INDEX, AUTOPLAY_ENABLED, EXTERNAL_PLAYBACK
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
    mark_list_dirty()
    play_item(item)


# Check if the requested index is already playing or starting.
def is_requested_track_already_playing(i):
    with LOCK:
        if DISPLAY_INDEX is None or i != DISPLAY_INDEX:
            return False
    if BOT_EXPECTING_WS > 0:
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
    global BOT_EXPECTING_WS

    while True:
        try:
            now = time.time()

            if not kodi_api.WS_CONNECTED:
                time.sleep(0.5)
                continue

            if not AUTOPLAY_ENABLED:
                time.sleep(0.5)
                continue

            if BOT_EXPECTING_WS > 0:
                time.sleep(0.2)
                continue

            if kodi_api.WS_STATE == "playing":
                time.sleep(0.5)
                continue

            if kodi_api.WS_STATE == "paused":
                time.sleep(0.5)
                continue

            resume_pending = False
            if kodi_api.WS_STATE == "stopped" and DISPLAY_INDEX is not None and LAST_PROGRESS_INDEX == DISPLAY_INDEX and LAST_PROGRESS_TIME:
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
                        print(
                            f"RESUME PENDING idx={DISPLAY_INDEX} attempts={attempts} remaining={remaining}",
                            flush=True,
                        )

            if kodi_api.WS_STATE == "stopped" and DISPLAY_INDEX is not None:
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
                        print(
                            f"RESUME ATTEMPT idx={DISPLAY_INDEX} attempt={RESUME_ATTEMPTS[DISPLAY_INDEX]} "
                            f"remaining={remaining}",
                            flush=True,
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

            if kodi_api.WS_STATE == "stopped":
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
            print("AUTOPLAY ERROR:", e)

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
