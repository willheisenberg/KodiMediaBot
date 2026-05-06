from kodibot.core import kodi_api as KA


def scan_video_library():
    res = KA.kodi_call("VideoLibrary.Scan")
    if res.get("error"):
        KA.log.warning(f"Video library scan failed: error={res['error']}")
        return False
    KA.log.info(f"Video library scan ok: res={res}")
    return True


def list_movies():
    res = KA.kodi_call(
        "VideoLibrary.GetMovies",
        {"properties": ["title", "year", "originaltitle", "uniqueid", "imdbnumber"], "sort": {"method": "title"}},
    )
    movies = (res.get("result", {}) or {}).get("movies", []) or []
    return movies


def list_tvshows():
    res = KA.kodi_call(
        "VideoLibrary.GetTVShows",
        {"properties": ["title", "year", "uniqueid", "imdbnumber"], "sort": {"method": "title"}},
    )
    shows = (res.get("result", {}) or {}).get("tvshows", []) or []
    return shows


def list_tvshow_episodes(tvshowid, showtitle=""):
    attempts = []
    if tvshowid is not None:
        attempts.extend([
            {
                "tvshowid": tvshowid,
                "properties": ["title", "showtitle", "season", "episode", "uniqueid", "imdbnumber"],
                "sort": {"method": "episode"},
            },
            {
                "tvshowid": tvshowid,
                "properties": ["title", "showtitle", "season", "episode", "uniqueid", "imdbnumber"],
            },
            {
                "tvshowid": tvshowid,
                "properties": ["title", "showtitle", "season", "episode"],
            },
        ])
    if showtitle:
        attempts.extend([
            {
                "properties": ["title", "showtitle", "season", "episode", "uniqueid", "imdbnumber"],
                "sort": {"method": "episode"},
            },
            {
                "properties": ["title", "showtitle", "season", "episode"],
            },
        ])

    want = KA.normalize_title(showtitle)
    for params in attempts:
        res = KA.kodi_call("VideoLibrary.GetEpisodes", params)
        episodes = (res.get("result", {}) or {}).get("episodes", []) or []
        if want and "tvshowid" not in params:
            episodes = [ep for ep in episodes if KA.normalize_title(ep.get("showtitle") or "") == want]
        if episodes:
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
