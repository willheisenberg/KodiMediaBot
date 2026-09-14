import shlex
import subprocess
from kodibot.core import kodi_api as KA


def scan_video_library():
    res = KA.kodi_call("VideoLibrary.Scan")
    if res.get("error"):
        KA.log.warning(f"Video library scan failed: error={res['error']}")
        return False
    KA.log.info(f"Video library scan ok: res={res}")
    return True


def get_ctimes_via_ssh(files: list) -> dict:
    if not files or not KA.CFG.cec_host:
        return {}
    file_list_str = "\n".join(f for f in files if f) + "\n"

    script = (
        "import os, sys\n"
        "for f in sys.stdin.read().splitlines():\n"
        "    if not f or not os.path.exists(f): continue\n"
        "    try:\n"
        "        st = os.stat(f)\n"
        "        ct = st.st_ctime\n"
        "        if os.path.isdir(f):\n"
        "            for root, dirs, files in os.walk(f):\n"
        "                for name in dirs + files:\n"
        "                    try: ct = max(ct, os.stat(os.path.join(root, name)).st_ctime)\n"
        "                    except: pass\n"
        "        print(f'{f}|{int(ct)}')\n"
        "    except: pass\n"
    )
    remote_cmd = f"python3 -c {shlex.quote(script)}"
    cmd = f"{_ssh_prefix()} {shlex.quote(remote_cmd)}"

    try:
        res = subprocess.run(
            cmd, shell=True, input=file_list_str, text=True, capture_output=True, timeout=20
        )
        if res.returncode != 0 and not res.stdout:
            KA.log.warning(f"SSH ctime fetch failed: {res.stderr.strip()}")
            return {}
        ctime_map = {}
        for line in res.stdout.splitlines():
            parts = line.split("|")
            if len(parts) == 2:
                try:
                    ctime_map[parts[0]] = int(parts[1])
                except ValueError:
                    pass
        return ctime_map
    except Exception as e:
        KA.log.warning(f"SSH ctime error: {e}")
        return {}


def _ssh_prefix():
    host = shlex.quote(KA.CFG.cec_host)
    return (
        "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-o BatchMode=yes -o ConnectTimeout=5 root@{host}"
    )


def subtitle_path_for(video_path, language):
    """Kodi's naming scheme: ``Movie.mkv`` plus ``de`` becomes ``Movie.de.srt``."""
    if not video_path:
        return ""
    tail = video_path.rsplit("/", 1)[-1]
    base = video_path.rsplit(".", 1)[0] if "." in tail else video_path
    return f"{base}.{language}.srt"


def subtitle_exists_via_ssh(video_path, language):
    """True when the subtitle already sits next to the video on the Kodi host."""
    target = subtitle_path_for(video_path, language)
    if not target or not KA.CFG.cec_host or "://" in video_path:
        return False
    script = (
        "import os, sys\n"
        "print('yes' if os.path.exists(sys.argv[1]) else 'no')\n"
    )
    remote_cmd = f"python3 -c {shlex.quote(script)} {shlex.quote(target)}"
    cmd = f"{_ssh_prefix()} {shlex.quote(remote_cmd)}"
    try:
        res = subprocess.run(
            cmd, shell=True, capture_output=True, timeout=20, stdin=subprocess.DEVNULL
        )
    except Exception as e:
        KA.log.warning(f"SSH subtitle probe error: {e}")
        return False
    return (res.stdout or b"").decode("utf-8", "replace").strip() == "yes"


def write_subtitle_via_ssh(video_path, language, content):
    """Write a subtitle next to the video file on the Kodi host.

    Returns the written path, or None when the host is unreachable or the
    source is not on its local filesystem (smb://, nfs://).  An existing file
    is never overwritten and counts as success.
    """
    target = subtitle_path_for(video_path, language)
    if not target or not KA.CFG.cec_host:
        return None
    if "://" in video_path:
        # Network sources do not live on the Kodi host's filesystem.
        return None
    script = (
        "import os, sys\n"
        "target = sys.argv[1]\n"
        "data = sys.stdin.buffer.read()\n"
        "if os.path.exists(target):\n"
        "    print('exists')\n"
        "    sys.exit(0)\n"
        "folder = os.path.dirname(target)\n"
        "if not os.path.isdir(folder):\n"
        "    print('nodir')\n"
        "    sys.exit(1)\n"
        "with open(target, 'wb') as fh:\n"
        "    fh.write(data)\n"
        "print('ok')\n"
    )
    remote_cmd = f"python3 -c {shlex.quote(script)} {shlex.quote(target)}"
    cmd = f"{_ssh_prefix()} {shlex.quote(remote_cmd)}"
    try:
        res = subprocess.run(cmd, shell=True, input=content, capture_output=True, timeout=20)
    except Exception as e:
        KA.log.warning(f"SSH subtitle write error: {e}")
        return None
    out = (res.stdout or b"").decode("utf-8", "replace").strip()
    if out in ("ok", "exists"):
        return target
    KA.log.warning(f"SSH subtitle write failed: rc={res.returncode} out={out}")
    return None


def now_playing_media_info():
    """Path and IMDb ids of the running item, for subtitle lookups.

    Returns None when nothing plays or the item has no file path.
    """
    pid = KA.get_active_playerid()
    if pid is None:
        return None
    res = KA.kodi_call(
        "Player.GetItem",
        {
            "playerid": pid,
            "properties": ["file", "uniqueid", "season", "episode", "showtitle", "tvshowid"],
        },
    )
    item = ((res.get("result", {}) or {}).get("item") or {})
    file_path = item.get("file") or ""
    if not file_path:
        return None
    info = {
        "file": file_path,
        "imdb_id": (item.get("uniqueid") or {}).get("imdb") or "",
        "parent_imdb_id": "",
        "season": None,
        "episode": None,
    }
    if (item.get("type") or "") == "episode":
        info["season"] = item.get("season")
        info["episode"] = item.get("episode")
        tvshowid = item.get("tvshowid")
        if tvshowid:
            show = KA.kodi_call(
                "VideoLibrary.GetTVShowDetails",
                {"tvshowid": tvshowid, "properties": ["uniqueid", "imdbnumber"]},
            )
            details = ((show.get("result", {}) or {}).get("tvshowdetails") or {})
            info["parent_imdb_id"] = (
                (details.get("uniqueid") or {}).get("imdb")
                or details.get("imdbnumber")
                or ""
            )
    return info


def list_movies():
    res = KA.kodi_call(
        "VideoLibrary.GetMovies",
        {"properties": ["title", "year", "originaltitle", "uniqueid", "imdbnumber", "dateadded", "file"], "sort": {"method": "title"}},
    )
    movies = (res.get("result", {}) or {}).get("movies", []) or []
    
    files = [m.get("file") for m in movies if m.get("file")]
    ctime_map = get_ctimes_via_ssh(files)
    for m in movies:
        f = m.get("file")
        if f and f in ctime_map:
            m["ctime"] = ctime_map[f]
            
    return movies


def list_tvshows():
    res = KA.kodi_call(
        "VideoLibrary.GetTVShows",
        {"properties": ["title", "year", "uniqueid", "imdbnumber", "dateadded", "file"], "sort": {"method": "title"}},
    )
    shows = (res.get("result", {}) or {}).get("tvshows", []) or []
    
    # Often TV shows represent directories, but if Kodi has a file prop for shows, we fetch it.
    files = [s.get("file") for s in shows if s.get("file")]
    if files:
        ctime_map = get_ctimes_via_ssh(files)
        for s in shows:
            f = s.get("file")
            if f and f in ctime_map:
                s["ctime"] = ctime_map[f]
                
    return shows


def list_tvshow_episodes(tvshowid, showtitle=""):
    attempts = []
    if tvshowid is not None:
        attempts.extend([
            {
                "tvshowid": tvshowid,
                "properties": ["title", "showtitle", "season", "episode", "uniqueid", "imdbnumber", "dateadded", "file"],
                "sort": {"method": "episode"},
            },
            {
                "tvshowid": tvshowid,
                "properties": ["title", "showtitle", "season", "episode", "uniqueid", "imdbnumber", "dateadded", "file"],
            },
            {
                "tvshowid": tvshowid,
                "properties": ["title", "showtitle", "season", "episode", "dateadded", "file"],
            },
        ])
    if showtitle:
        attempts.extend([
            {
                "properties": ["title", "showtitle", "season", "episode", "uniqueid", "imdbnumber", "dateadded", "file"],
                "sort": {"method": "episode"},
            },
            {
                "properties": ["title", "showtitle", "season", "episode", "dateadded", "file"],
            },
        ])

    want = KA.normalize_title(showtitle)
    for params in attempts:
        res = KA.kodi_call("VideoLibrary.GetEpisodes", params)
        episodes = (res.get("result", {}) or {}).get("episodes", []) or []
        if want and "tvshowid" not in params:
            episodes = [ep for ep in episodes if KA.normalize_title(ep.get("showtitle") or "") == want]
        if episodes:
            files = [e.get("file") for e in episodes if e.get("file")]
            ctime_map = get_ctimes_via_ssh(files)
            for e in episodes:
                f = e.get("file")
                if f and f in ctime_map:
                    e["ctime"] = ctime_map[f]
            return episodes
    return []


def play_movie(movieid, resume=False):
    if movieid is None:
        return False
    KA.stop_player_and_clear_playlists()
    res = KA.kodi_call("Player.Open", {"item": {"movieid": movieid}, "options": {"resume": bool(resume)}})
    return "error" not in res


def play_episode(episodeid, resume=False):
    if episodeid is None:
        return False
    KA.stop_player_and_clear_playlists()
    res = KA.kodi_call("Player.Open", {"item": {"episodeid": episodeid}, "options": {"resume": bool(resume)}})
    return "error" not in res


def play_all_episodes(episode_ids):
    ids = [eid for eid in episode_ids if eid is not None]
    if not ids:
        return False
    KA.stop_player_and_clear_playlists()
    for eid in ids:
        res = KA.kodi_call("Playlist.Add", {"playlistid": 1, "item": {"episodeid": eid}})
        if "error" in res:
            return False
    res = KA.kodi_call("Player.Open", {"item": {"playlistid": 1, "position": 0}})
    return "error" not in res


def list_movies_for_visual():
    """Movies for the Party Video source menu, with the codec.

    ``slow`` marks HEVC: the addon decodes in software, so those are likely to
    stutter on the Pi. They stay selectable — marked, not hidden.
    """
    res = KA.kodi_call(
        "VideoLibrary.GetMovies",
        {"properties": ["title", "file", "streamdetails"], "sort": {"method": "title"}},
    )
    movies = (res.get("result", {}) or {}).get("movies", []) or []
    result = []
    for movie in movies:
        path = movie.get("file")
        if not path:
            continue
        streams = ((movie.get("streamdetails") or {}).get("video") or [{}])[0]
        codec = (streams.get("codec") or "").lower()
        result.append({
            "title": movie.get("title") or path.rsplit("/", 1)[-1],
            "file": path,
            "codec": codec,
            "slow": codec in ("hevc", "h265"),
        })
    return result
