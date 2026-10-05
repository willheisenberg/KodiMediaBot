"""Tests for Spotify Connect through the service.soloist add-on."""
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


def _in_loop(call):
    """The reconnect callback is only triggered from a running event loop."""
    async def run():
        call()
    asyncio.run(run())


class TestStream:
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
        assert spotify_connect.display_name({"title": "Song A"}) == "Spotify: Song A"
        assert spotify_connect.display_name({}) == "Spotify"


class TestTakeover:
    """Spotify replaces what the bot was playing; nothing comes back after it."""

    def setup_method(self):
        _reset()

    def teardown_method(self):
        _reset()

    def test_takeover_ends_the_queue_playback(self, monkeypatch):
        _queue_playing_at(90)
        monkeypatch.setattr(queue_state, "schedule_now_playing_refresh", lambda: None)

        queue_state.spotify_takeover()

        assert queue_state.AUTOPLAY_ENABLED is False
        assert queue_state.DISPLAY_INDEX is None
        assert queue_state.CURRENT_INDEX is None
        # The queue itself stays; only its playback is over.
        assert queue_state.QUEUE == [QUEUED]

    def test_spotify_stream_play_event_counts_as_takeover(self, monkeypatch):
        _queue_playing_at(90)
        monkeypatch.setattr(queue_state, "schedule_now_playing_refresh", lambda: None)

        queue_state._handle_ws_play(item=SPOTIFY_ITEM, item_params={})

        assert queue_state.AUTOPLAY_ENABLED is False
        assert queue_state.DISPLAY_INDEX is None

    def test_takeover_forgets_the_radio_and_cancels_a_pending_reconnect(self, monkeypatch):
        cancelled = []
        queue_state.CANCEL_RECONNECT_CB = lambda: cancelled.append(True)
        queue_state.set_last_played_radio("http://radio", "Radio X")
        monkeypatch.setattr(queue_state, "schedule_now_playing_refresh", lambda: None)

        queue_state.spotify_takeover()

        assert queue_state.LAST_PLAYED_RADIO is None
        assert cancelled

    def test_radio_stop_after_takeover_does_not_reconnect(self, monkeypatch):
        reconnects = []
        queue_state.ON_UNEXPECTED_RADIO_STOP = lambda url, title: reconnects.append(url)
        queue_state.set_last_played_radio("http://radio", "Radio X")
        monkeypatch.setattr(queue_state, "schedule_now_playing_refresh", lambda: None)

        queue_state.spotify_takeover()
        _in_loop(lambda: queue_state._handle_ws_stop(item_params={"type": "channel", "title": "Radio X"}))

        assert reconnects == []

    def test_nothing_restarts_when_spotify_stops(self, monkeypatch):
        reconnects = []
        queue_state.ON_UNEXPECTED_RADIO_STOP = lambda url, title: reconnects.append(url)
        _queue_playing_at(90)
        queue_state.set_last_played_radio("http://radio", "Radio X")
        monkeypatch.setattr(queue_state, "schedule_now_playing_refresh", lambda: None)
        queue_state.spotify_takeover()

        _in_loop(lambda: queue_state._handle_ws_stop(item_params={"type": "song", "title": "Song A"}))

        assert reconnects == []
        assert queue_state.AUTOPLAY_ENABLED is False
        assert queue_state.DISPLAY_INDEX is None


class TestPanelShowsSpotify:
    def setup_method(self):
        _reset()
        kodi_api.LAST_WS_PLAYERID = None
        kodi_api.LAST_WS_ITEM.clear()
        kodi_api.WS_PLAYING = True

    def teardown_method(self):
        _reset()

    def test_panel_shows_the_spotify_track(self, monkeypatch):
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


class TestPlayEventFromPythonAddon:
    """Kodi reports playerid -1 and no file for the add-on's stream."""

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


class TestBotReleasesSpotify:
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

        queue_state.play_item(dict(QUEUED))

        assert calls[0] == (
            "JSONRPC.NotifyAll",
            {"sender": "kodibot", "message": spotify_connect.RELEASE_MESSAGE},
        )
        assert calls[1] == "stop"

    def test_radio_start_releases_the_spotify_device_first(self, monkeypatch):
        calls = self._patch_stop(monkeypatch, [])
        monkeypatch.setattr(
            queue_state.kodi_api, "play_favourite_target",
            lambda url, title=None: calls.append(("open", url)) or True,
        )

        assert queue_state.play_radio("pvr://channels/radio/1", "Radio X")

        assert calls == [
            ("JSONRPC.NotifyAll", {"sender": "kodibot", "message": spotify_connect.RELEASE_MESSAGE}),
            ("open", "pvr://channels/radio/1"),
        ]

    def test_closing_a_slideshow_over_music_keeps_spotify(self, monkeypatch):
        players = [{"playerid": 2, "type": "picture"}, {"playerid": 0, "type": "audio"}]
        calls = self._patch_stop(monkeypatch, players)

        queue_state.hard_stop_and_clear()

        assert not any(isinstance(c, tuple) and c[0] == "JSONRPC.NotifyAll" for c in calls)


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
