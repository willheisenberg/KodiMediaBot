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
    host = shlex.quote(KA.CFG.cec_host)
    ssh = f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@{host}"
    
    script = (
        "import os, sys\n"
        "for f in sys.stdin.read().splitlines():\n"
        "    if os.path.exists(f):\n"
        "        print(f'{f}|{int(os.stat(f).st_ctime)}')\n"
    )
    remote_cmd = f"python3 -c {shlex.quote(script)}"
    cmd = f"{ssh} {shlex.quote(remote_cmd)}"
    
    try:
        res = subprocess.run(cmd, shell=True, input=file_list_str, text=True, capture_output=True)
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
