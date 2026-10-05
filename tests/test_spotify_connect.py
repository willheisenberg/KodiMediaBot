"""Tests for the Spotify Connect handover (service.soloist add-on)."""
import asyncio
import os
import sys

os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "test:token")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from kodibot.core import kodi_api, kodi_metadata, queue_state, spotify_connect
from kodibot.telegram import panel

SPOTIFY_ITEM = {
    "type": "song",
    "title": "Song A",
    "artist": ["Artist A", "Artist B"],
    "file": spotify_connect.STREAM_URL,
}
QUEUED = {"title": "Queued Video", "url": "plugin://queued", "kind": "video", "link": "https://queued.example"}


def _reset():
    spotify_connect.end()
    queue_state.QUEUE.clear()
    queue_state.CURRENT_INDEX = None
    queue_state.DISPLAY_INDEX = None
    queue_state.NEXT_INDEX = 0
    queue_state.AUTOPLAY_ENABLED = True
    queue_state.EXTERNAL_PLAYBACK = False
    queue_state.BOT_EXPECTING_WS = 0
    queue_state.LAST_PLAYED_RADIO = None
    queue_state.EXPECTED_STOP = False
    queue_state.ON_PLAY_STARTED = None
    queue_state.CANCEL_RECONNECT_CB = None
    queue_state.ON_UNEXPECTED_RADIO_STOP = None
    queue_state.LAST_PROGRESS_TS = 0.0
    queue_state.LAST_PROGRESS_TIME = None
    queue_state.LAST_PROGRESS_TOTAL = None
    queue_state.LAST_PROGRESS_INDEX = None


def _queue_playing_at(seconds):
    queue_state.QUEUE.append(dict(QUEUED))
    queue_state.CURRENT_INDEX = 0
    queue_state.DISPLAY_INDEX = 0
    queue_state.NEXT_INDEX = 1
    queue_state.LAST_PROGRESS_INDEX = 0
    queue_state.LAST_PROGRESS_TIME = {"hours": 0, "minutes": 0, "seconds": seconds}
    queue_state.LAST_PROGRESS_TOTAL = {"hours": 0, "minutes": 5, "seconds": 0}


class TestHandoverState:
    def setup_method(self):
        spotify_connect.end()

    def test_stream_detection(self):
        assert spotify_connect.is_stream_item(SPOTIFY_ITEM)
        assert spotify_connect.is_stream(spotify_connect.STREAM_URL + "/")
        assert not spotify_connect.is_stream_item({"file": "rtp://127.0.0.1:9999"})
        assert not spotify_connect.is_stream_item(None)

    def test_takeover_event_requires_the_addon_as_sender(self):
        params = {"sender": "service.soloist", "data": None}
        assert spotify_connect.is_takeover_event("Other.soloist_takeover", params)
        assert not spotify_connect.is_takeover_event("Other.soloist_takeover", {"sender": "x"})
        assert not spotify_connect.is_takeover_event("Player.OnPlay", params)

    def test_display_name(self):
        assert spotify_connect.display_name(SPOTIFY_ITEM) == "Spotify: Song A – Artist A, Artist B"
        assert spotify_connect.display_name({"title": "Song A", "artist": []}) == "Spotify: Song A"
        assert spotify_connect.display_name({}) == "Spotify"

    def test_second_begin_keeps_first_radio_snapshot(self):
        assert spotify_connect.begin({"url": "u", "title": "Radio"}, now=0)
        assert not spotify_connect.begin(None, now=1)
        assert spotify_connect.parked_radio() == {"url": "u", "title": "Radio"}
        assert spotify_connect.end() == {"url": "u", "title": "Radio"}
        assert not spotify_connect.is_active()
        assert spotify_connect.end() is None

    def test_settle_and_pause_timing(self):
        spotify_connect.begin(now=100)
        assert not spotify_connect.settled(now=100 + spotify_connect.SETTLE_SEC - 1)
        assert spotify_connect.settled(now=100 + spotify_connect.SETTLE_SEC)

        spotify_connect.set_paused(True, now=200)
        # A second pause event must not restart the clock.
        spotify_connect.set_paused(True, now=210)
        assert not spotify_connect.paused_too_long(now=200 + spotify_connect.PAUSE_TAKEBACK_SEC - 1)
        assert spotify_connect.paused_too_long(now=200 + spotify_connect.PAUSE_TAKEBACK_SEC)
        spotify_connect.set_paused(False)
        assert not spotify_connect.paused_too_long(now=1000)

    def test_pause_outside_handover_is_ignored(self):
        spotify_connect.set_paused(True, now=0)
        spotify_connect.begin(now=1)
        assert not spotify_connect.paused_too_long(now=1000)


class TestQueueStateHandover:
    def setup_method(self):
        _reset()

    def teardown_method(self):
        _reset()

    def test_spotify_play_parks_queue_instead_of_clearing_it(self, monkeypatch):
        _queue_playing_at(90)
        monkeypatch.setattr(queue_state, "schedule_playback_refresh", lambda: None)
        monkeypatch.setattr(
            queue_state, "clear_bot_playback_state",
            lambda: (_ for _ in ()).throw(AssertionError("queue must stay parked")),
        )

        queue_state._handle_ws_play(item=SPOTIFY_ITEM, item_params={})

        assert spotify_connect.is_active()
        assert queue_state.AUTOPLAY_ENABLED is True
        assert queue_state.DISPLAY_INDEX == 0
        assert queue_state.EXTERNAL_PLAYBACK is False

    def test_takeover_parks_radio_and_cancels_pending_reconnect(self, monkeypatch):
        monkeypatch.setattr(queue_state, "schedule_playback_refresh", lambda: None)
        cancelled = []
        queue_state.CANCEL_RECONNECT_CB = lambda: cancelled.append(True)
        queue_state.set_last_played_radio("http://radio", "Radio X")

        queue_state.begin_spotify_handover()

        assert cancelled == [True]
        assert queue_state.EXPECTED_STOP is True
        assert spotify_connect.parked_radio() == {"url": "http://radio", "title": "Radio X"}

    def test_radio_stop_after_takeover_does_not_reconnect(self, monkeypatch):
        monkeypatch.setattr(queue_state, "schedule_playback_refresh", lambda: None)
        monkeypatch.setattr(queue_state, "schedule_now_playing_refresh", lambda: None)
        reconnects = []
        queue_state.ON_UNEXPECTED_RADIO_STOP = lambda url, title: reconnects.append(url)
        queue_state.set_last_played_radio("http://radio", "Radio X")

        queue_state.begin_spotify_handover()
        queue_state._handle_ws_stop(item_params={"type": "song"}, player_params={"playerid": 0})

        assert reconnects == []

    def test_external_play_during_handover_drops_it(self, monkeypatch):
        monkeypatch.setattr(queue_state, "schedule_playback_refresh", lambda: None)
        monkeypatch.setattr(queue_state, "schedule_now_playing_refresh", lambda: None)
        monkeypatch.setattr(queue_state.kodi_api, "kodi_item_matches_queue", lambda item, qitem: False)
        spotify_connect.begin({"url": "http://radio", "title": "Radio X"})

        queue_state._handle_ws_play(item={"file": "smb://movie.mkv"}, item_params={})

        assert not spotify_connect.is_active()

    def test_pause_events_feed_the_takeback_timer(self, monkeypatch):
        monkeypatch.setattr(queue_state, "schedule_now_playing_refresh", lambda: None)
        spotify_connect.begin(now=0)
        queue_state._handle_ws_pause()
        assert spotify_connect._paused_since is not None
        queue_state._handle_ws_resume()
        assert spotify_connect._paused_since is None

    def test_spotify_stop_ends_handover_and_restarts_radio(self, monkeypatch):
        monkeypatch.setattr(queue_state, "schedule_playback_refresh", lambda: None)
        started = []
        monkeypatch.setattr(queue_state.kodi_api, "play_favourite_target", lambda url, title: started.append(url) or True)
        monkeypatch.setattr(queue_state.kodi_api, "get_active_players", lambda: [])
        monkeypatch.setattr(queue_state.kodi_api, "WS_STATE", "stopped")
        spotify_connect.begin({"url": "http://radio", "title": "Radio X"}, now=0)

        queue_state._tick_spotify_handover()

        assert started == ["http://radio"]
        assert not spotify_connect.is_active()
        assert queue_state.LAST_PLAYED_RADIO == {"url": "http://radio", "title": "Radio X"}

    def test_stop_during_switch_does_not_end_handover(self, monkeypatch):
        monkeypatch.setattr(queue_state.kodi_api, "WS_STATE", "stopped")
        monkeypatch.setattr(queue_state.kodi_api, "get_active_players", lambda: [])
        spotify_connect.begin()  # just now: Kodi is still switching to the stream

        queue_state._tick_spotify_handover()

        assert spotify_connect.is_active()

    def test_stop_with_a_player_still_active_does_not_end_handover(self, monkeypatch):
        monkeypatch.setattr(queue_state.kodi_api, "WS_STATE", "stopped")
        monkeypatch.setattr(queue_state.kodi_api, "get_active_players", lambda: [{"playerid": 0}])
        spotify_connect.begin(now=0)

        queue_state._tick_spotify_handover()

        assert spotify_connect.is_active()

    def test_long_pause_takes_playback_back_for_a_parked_queue(self, monkeypatch):
        _queue_playing_at(90)
        stops = []
        monkeypatch.setattr(queue_state.kodi_api, "stop_all_players", lambda: stops.append(True))
        monkeypatch.setattr(queue_state.kodi_api, "WS_STATE", "paused")
        spotify_connect.begin(now=0)
        spotify_connect.set_paused(True, now=0)

        queue_state._tick_spotify_handover()

        assert stops == [True]

    def test_long_pause_without_anything_to_resume_leaves_spotify(self, monkeypatch):
        stops = []
        monkeypatch.setattr(queue_state.kodi_api, "stop_all_players", lambda: stops.append(True))
        monkeypatch.setattr(queue_state.kodi_api, "WS_STATE", "paused")
        queue_state.AUTOPLAY_ENABLED = False
        spotify_connect.begin(now=0)
        spotify_connect.set_paused(True, now=0)

        queue_state._tick_spotify_handover()

        assert stops == []

    def test_short_pause_keeps_spotify(self, monkeypatch):
        _queue_playing_at(90)
        stops = []
        monkeypatch.setattr(queue_state.kodi_api, "stop_all_players", lambda: stops.append(True))
        monkeypatch.setattr(queue_state.kodi_api, "WS_STATE", "paused")
        spotify_connect.begin()
        spotify_connect.set_paused(True)

        queue_state._tick_spotify_handover()

        assert stops == []

    def test_bot_playback_ends_handover(self, monkeypatch):
        monkeypatch.setattr(queue_state.media, "cleanup_active_image_session", lambda: None)
        monkeypatch.setattr(queue_state.kodi_api, "stop_all_players", lambda: None)
        monkeypatch.setattr(queue_state.kodi_api, "kodi_clear_all_playlists", lambda: None)
        monkeypatch.setattr(queue_state.kodi_api, "kodi_call", lambda m, p=None: {})
        monkeypatch.setattr(queue_state, "set_expecting_ws", lambda n: None)
        monkeypatch.setattr(queue_state, "schedule_playback_refresh", lambda: None)
        spotify_connect.begin({"url": "http://radio", "title": "Radio X"})

        queue_state.play_item(dict(QUEUED))

        assert not spotify_connect.is_active()

    def test_hard_stop_ends_handover(self, monkeypatch):
        monkeypatch.setattr(queue_state.media, "cleanup_active_image_session", lambda: None)
        monkeypatch.setattr(queue_state.kodi_api, "get_active_players", lambda: [])
        monkeypatch.setattr(queue_state.kodi_api, "stop_all_players", lambda: None)
        monkeypatch.setattr(queue_state.kodi_api, "kodi_clear_all_playlists", lambda: None)
        monkeypatch.setattr(queue_state, "schedule_playback_refresh", lambda: None)
        spotify_connect.begin()

        queue_state.hard_stop_and_clear()

        assert not spotify_connect.is_active()


class TestPanelDuringHandover:
    def setup_method(self):
        _reset()
        kodi_api.LAST_WS_PLAYERID = None
        kodi_api.LAST_WS_ITEM.clear()
        kodi_api.WS_PLAYING = True

    def teardown_method(self):
        _reset()

    def test_panel_shows_spotify_and_keeps_queue_resume_point(self, monkeypatch):
        _queue_playing_at(90)
        spotify_connect.begin()

        async def fake_call(method, params=None):
            if method == "Player.GetActivePlayers":
                return {"result": [{"playerid": 0, "type": "audio"}]}
            if method == "Player.GetProperties":
                return {"result": {
                    "time": {"hours": 0, "minutes": 0, "seconds": 7},
                    "totaltime": {"hours": 0, "minutes": 0, "seconds": 0},
                }}
            if method == "Player.GetItem":
                return {"result": {"item": dict(SPOTIFY_ITEM)}}
            raise AssertionError(method)

        monkeypatch.setattr(panel.kodi_api, "kodi_call_async", fake_call)
        monkeypatch.setattr(panel.kodi_api, "pick_playerid", lambda players: 0)
        monkeypatch.setattr(panel.kodi_api, "maybe_cache_soundcloud_url", lambda file_url: None)
        monkeypatch.setattr(panel.kodi_api, "cached_spotify_connect_track_link", lambda artist, title: "")
        monkeypatch.setattr(
            panel.kodi_api, "external_item_display",
            lambda item: (_ for _ in ()).throw(AssertionError("should not be called")),
        )

        text, progress, playlist = asyncio.run(panel.get_now_playing_text())

        assert text == "▶ Spotify: Song A – Artist A, Artist B"
        # The queue stays parked at its interruption point.
        assert queue_state.DISPLAY_INDEX == 0
        assert queue_state.EXTERNAL_PLAYBACK is False
        assert queue_state.LAST_PROGRESS_TIME == {"hours": 0, "minutes": 0, "seconds": 90}
        assert queue_state.LAST_PROGRESS_INDEX == 0


class TestPlayEventFromPythonAddon:
    """Kodi reports playerid -1 and no file for the add-on's stream."""

    def setup_method(self):
        _reset()

    def teardown_method(self):
        _reset()

    def test_play_event_without_file_keeps_handover_and_queue(self, monkeypatch):
        _queue_playing_at(90)
        monkeypatch.setattr(queue_state, "schedule_playback_refresh", lambda: None)
        monkeypatch.setattr(
            queue_state, "clear_bot_playback_state",
            lambda: (_ for _ in ()).throw(AssertionError("queue must stay parked")),
        )
        queue_state.begin_spotify_handover()

        queue_state._handle_ws_play(item={}, item_params={"title": "Song A", "type": "song"})

        assert spotify_connect.is_active()
        assert queue_state.DISPLAY_INDEX == 0

    def test_playerid_minus_one_resolves_to_the_active_player(self, monkeypatch):
        from kodibot.core import kodi_ws

        async def fake_call(method, params=None):
            assert method == "Player.GetActivePlayers"
            return {"result": [{"playerid": 0, "type": "audio"}]}

        monkeypatch.setattr(kodi_api, "kodi_call_async", fake_call)

        resolved = asyncio.run(kodi_ws.resolve_player_params({"playerid": -1, "speed": 1}))

        assert resolved == {"playerid": 0, "speed": 1}

    def test_valid_playerid_is_left_alone(self, monkeypatch):
        from kodibot.core import kodi_ws

        async def fake_call(method, params=None):
            raise AssertionError("no lookup needed")

        monkeypatch.setattr(kodi_api, "kodi_call_async", fake_call)

        assert asyncio.run(kodi_ws.resolve_player_params({"playerid": 1})) == {"playerid": 1}
        assert asyncio.run(kodi_ws.resolve_player_params({})) == {}

    def test_playerid_minus_one_without_active_player_stays(self, monkeypatch):
        from kodibot.core import kodi_ws

        async def fake_call(method, params=None):
            return {"result": []}

        monkeypatch.setattr(kodi_api, "kodi_call_async", fake_call)

        assert asyncio.run(kodi_ws.resolve_player_params({"playerid": -1})) == {"playerid": -1}


class TestStopReleasesSpotify:
    def setup_method(self):
        _reset()

    def teardown_method(self):
        _reset()

    def _patch_stop(self, monkeypatch, active):
        calls = []
        monkeypatch.setattr(queue_state.media, "cleanup_active_image_session", lambda: None)
        monkeypatch.setattr(queue_state.kodi_api, "get_active_players", lambda: active)
        monkeypatch.setattr(queue_state.kodi_api, "stop_all_players", lambda: calls.append("stop"))
        monkeypatch.setattr(queue_state.kodi_api, "kodi_clear_all_playlists", lambda: None)
        monkeypatch.setattr(queue_state.kodi_api, "kodi_call", lambda m, p=None: calls.append((m, p)) or {})
        monkeypatch.setattr(queue_state, "schedule_playback_refresh", lambda: None)
        return calls

    def test_stop_releases_the_spotify_device_before_stopping(self, monkeypatch):
        calls = self._patch_stop(monkeypatch, [{"playerid": 0, "type": "audio"}])

        queue_state.hard_stop_and_clear()

        assert calls[0] == (
            "JSONRPC.NotifyAll",
            {"sender": "kodibot", "message": spotify_connect.RELEASE_MESSAGE},
        )
        assert calls[1] == "stop"

    def test_queue_playback_releases_the_spotify_device_before_stopping(self, monkeypatch):
        calls = self._patch_stop(monkeypatch, [])
        monkeypatch.setattr(queue_state, "set_expecting_ws", lambda n: None)
        spotify_connect.begin()

        queue_state.play_item(dict(QUEUED))

        assert calls[0] == (
            "JSONRPC.NotifyAll",
            {"sender": "kodibot", "message": spotify_connect.RELEASE_MESSAGE},
        )
        assert calls[1] == "stop"

    def test_radio_start_releases_the_spotify_device_and_ends_the_handover(self, monkeypatch):
        calls = self._patch_stop(monkeypatch, [])
        monkeypatch.setattr(
            queue_state.kodi_api, "play_favourite_target",
            lambda url, title=None: calls.append(("open", url)) or True,
        )
        spotify_connect.begin()

        assert queue_state.play_radio("pvr://channels/radio/1", "Radio X")

        assert calls == [
            ("JSONRPC.NotifyAll", {"sender": "kodibot", "message": spotify_connect.RELEASE_MESSAGE}),
            ("open", "pvr://channels/radio/1"),
        ]
        assert not spotify_connect.is_active()

    def test_closing_a_slideshow_over_music_keeps_spotify(self, monkeypatch):
        players = [{"playerid": 2, "type": "picture"}, {"playerid": 0, "type": "audio"}]
        calls = self._patch_stop(monkeypatch, players)

        queue_state.hard_stop_and_clear()

        assert not any(isinstance(c, tuple) and c[0] == "JSONRPC.NotifyAll" for c in calls)


def test_handover_end_resets_resume_attempts(monkeypatch):
    _reset()
    try:
        _queue_playing_at(90)
        queue_state.RESUME_ATTEMPTS[0] = queue_state.RESUME_MAX_ATTEMPTS - 1
        monkeypatch.setattr(queue_state, "schedule_playback_refresh", lambda: None)
        spotify_connect.begin()

        queue_state.end_spotify_handover()

        assert queue_state.RESUME_ATTEMPTS == {}
    finally:
        queue_state.RESUME_ATTEMPTS.clear()
        _reset()


class TestSpotifyTrackLink:
    """The panel links the Spotify track to YouTube/SoundCloud like radio titles."""

    def setup_method(self):
        panel.S.SPOTIFY_LINK_PENDING.clear()
        kodi_api.YT_SEARCH_CACHE.clear()
        kodi_api.SC_SEARCH_CACHE.clear()

    def teardown_method(self):
        self.setup_method()

    def test_artist_title_from_item(self):
        assert spotify_connect.artist_title(SPOTIFY_ITEM) == ("Artist A, Artist B", "Song A")
        assert spotify_connect.artist_title({"artist": "Solo", "title": "T"}) == ("Solo", "T")
        assert spotify_connect.artist_title(None) == ("", "")

    def test_lookup_prefers_youtube_and_matches_on_primary_artist(self, monkeypatch):
        calls = []

        def fake_yt(query, expected_title="", timeout=None):
            calls.append((query, expected_title))
            return "https://youtu.be/abcdefghijk"

        monkeypatch.setattr(kodi_metadata, "search_youtube_link", fake_yt)
        monkeypatch.setattr(
            kodi_metadata, "search_soundcloud_link",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("YouTube already matched")),
        )

        link = kodi_api.spotify_connect_track_link("Artist A, Artist B", "Song A")

        assert link == "https://youtu.be/abcdefghijk"
        assert calls == [("Artist A, Artist B Song A", "Artist A - Song A")]

    def test_lookup_falls_back_to_soundcloud(self, monkeypatch):
        monkeypatch.setattr(kodi_metadata, "search_youtube_link", lambda *a, **k: "")
        monkeypatch.setattr(
            kodi_metadata, "search_soundcloud_link",
            lambda query, expected_title="": f"https://soundcloud.com/x/{query}",
        )

        assert kodi_api.spotify_connect_track_link("Artist A", "Song A") == (
            "https://soundcloud.com/x/Artist A - Song A"
        )

    def test_lookup_needs_artist_and_title(self):
        assert kodi_api.spotify_connect_track_link("", "Song A") == ""
        assert kodi_api.cached_spotify_connect_track_link("Artist A", "") == ""

    def test_cache_peek_distinguishes_unknown_from_not_found(self):
        norm = kodi_api.normalize_title
        assert kodi_api.cached_spotify_connect_track_link("Artist A", "Song A") is None

        kodi_api.cache_youtube_link(norm("Artist A Song A"), "")
        # YouTube found nothing; SoundCloud has not been asked yet.
        assert kodi_api.cached_spotify_connect_track_link("Artist A", "Song A") is None

        kodi_api.cache_soundcloud_link(norm("Artist A - Song A"), "")
        assert kodi_api.cached_spotify_connect_track_link("Artist A", "Song A") == ""

        kodi_api.cache_youtube_link(norm("Artist A Song A"), "https://youtu.be/abcdefghijk")
        assert kodi_api.cached_spotify_connect_track_link("Artist A", "Song A") == "https://youtu.be/abcdefghijk"

    def test_known_link_is_returned_without_a_lookup(self, monkeypatch):
        monkeypatch.setattr(
            panel.kodi_api, "cached_spotify_connect_track_link",
            lambda artist, title: "https://youtu.be/abcdefghijk",
        )
        monkeypatch.setattr(
            panel.kodi_api, "spotify_connect_track_link",
            lambda *a: (_ for _ in ()).throw(AssertionError("no lookup needed")),
        )

        async def run():
            return panel.spotify_track_link(SPOTIFY_ITEM)

        assert asyncio.run(run()) == "https://youtu.be/abcdefghijk"
        assert panel.S.SPOTIFY_LINK_PENDING == set()

    def test_unknown_track_is_looked_up_once_in_the_background(self, monkeypatch):
        lookups, refreshes = [], []
        monkeypatch.setattr(panel.kodi_api, "cached_spotify_connect_track_link", lambda artist, title: None)
        monkeypatch.setattr(
            panel.kodi_api, "spotify_connect_track_link",
            lambda artist, title: lookups.append((artist, title)) or "https://youtu.be/abcdefghijk",
        )
        monkeypatch.setattr(panel.queue_state, "schedule_now_playing_refresh", lambda: refreshes.append(True))

        async def run():
            first = panel.spotify_track_link(SPOTIFY_ITEM)
            second = panel.spotify_track_link(SPOTIFY_ITEM)  # panel refreshed meanwhile
            pending = set(panel.S.SPOTIFY_LINK_PENDING)
            await asyncio.sleep(0.2)
            return first, second, pending

        first, second, pending = asyncio.run(run())

        # The panel is not held up: no link yet, it arrives with the refresh.
        assert first is None and second is None
        assert pending == {("Artist A, Artist B", "Song A")}
        assert lookups == [("Artist A, Artist B", "Song A")]
        assert refreshes == [True]
        assert panel.S.SPOTIFY_LINK_PENDING == set()

    def test_failed_lookup_does_not_refresh_the_panel(self, monkeypatch):
        refreshes = []
        monkeypatch.setattr(panel.kodi_api, "cached_spotify_connect_track_link", lambda artist, title: None)
        monkeypatch.setattr(panel.kodi_api, "spotify_connect_track_link", lambda artist, title: "")
        monkeypatch.setattr(panel.queue_state, "schedule_now_playing_refresh", lambda: refreshes.append(True))

        async def run():
            panel.spotify_track_link(SPOTIFY_ITEM)
            await asyncio.sleep(0.2)

        asyncio.run(run())

        assert refreshes == []
        assert panel.S.SPOTIFY_LINK_PENDING == set()
