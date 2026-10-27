"""Tests for pure functions in queue_state.py"""
import os
import sys
import threading
from unittest.mock import patch

import pytest

os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "test:token")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from kodibot.core import queue_state


class TestMakeItem:
    def test_basic(self):
        item = queue_state.make_item("Test", "http://example.com", "video")
        assert item["title"] == "Test"
        assert item["url"] == "http://example.com"
        assert item["kind"] == "video"
        assert item["link"] is None

    def test_with_link(self):
        item = queue_state.make_item("Test", "http://example.com", "audio", link="http://link")
        assert item["link"] == "http://link"


class TestMakeYoutube:
    def test_creates_plugin_url(self):
        item = queue_state.make_youtube("dQw4w9WgXcQ")
        assert "plugin://plugin.video.youtube" in item["url"]
        assert "dQw4w9WgXcQ" in item["url"]
        assert item["kind"] == "video"
        assert item["link"] == "https://youtu.be/dQw4w9WgXcQ"


class TestMakeSoundcloud:
    def test_creates_plugin_url(self):
        item = queue_state.make_soundcloud("https://soundcloud.com/artist/track")
        assert "plugin://plugin.audio.soundcloud" in item["url"]
        assert item["kind"] == "audio"
        assert item["resolver"] == "soundcloud"


class TestSoundcloudDisplayTitle:
    def test_basic(self):
        result = queue_state.soundcloud_display_title("https://soundcloud.com/some-artist/some-track")
        assert "some artist" in result
        assert "some track" in result

    def test_no_match(self):
        result = queue_state.soundcloud_display_title("not-a-url")
        assert result == "not-a-url"


class TestIsSCUrls:
    def test_track_url(self):
        assert queue_state.is_sc_track_url("https://soundcloud.com/artist/track")

    def test_set_url(self):
        assert queue_state.is_sc_set_url("https://soundcloud.com/artist/sets/album")

    def test_not_sc(self):
        assert not queue_state.is_sc_track_url("https://example.com")


class TestQueueOperations:
    def setup_method(self):
        queue_state.QUEUE.clear()
        queue_state.CURRENT_INDEX = None
        queue_state.DISPLAY_INDEX = None
        queue_state.NEXT_INDEX = 0
        queue_state._SCHEDULE_NOW_PLAYING_REFRESH = None

    def test_queue_item(self):
        item = queue_state.make_item("Test", "url", "video")
        queue_state.queue_item(item)
        assert len(queue_state.QUEUE) == 1

    def test_clear_queue(self):
        queue_state.queue_item(queue_state.make_item("A", "url1", "video"))
        queue_state.queue_item(queue_state.make_item("B", "url2", "video"))
        queue_state.clear_queue()
        assert len(queue_state.QUEUE) == 0
        assert queue_state.CURRENT_INDEX is None
        assert queue_state.DISPLAY_INDEX is None
        assert queue_state.NEXT_INDEX == 0

    def test_clear_queue_resets_display_and_ws_expectation(self):
        queue_state.queue_item(queue_state.make_item("A", "url1", "video"))
        queue_state.DISPLAY_INDEX = 0
        queue_state.CURRENT_INDEX = 0
        queue_state.NEXT_INDEX = 1
        queue_state.set_expecting_ws(2)
        queue_state.clear_queue()
        assert queue_state.DISPLAY_INDEX is None
        assert queue_state.CURRENT_INDEX is None
        assert queue_state.NEXT_INDEX == 0
        assert queue_state.get_expecting_ws() == 0

    def test_delete_index(self):
        queue_state.queue_item(queue_state.make_item("A", "url1", "video"))
        queue_state.queue_item(queue_state.make_item("B", "url2", "video"))
        ok, msg = queue_state.delete_index(0)
        assert ok
        assert len(queue_state.QUEUE) == 1
        assert queue_state.QUEUE[0]["title"] == "B"

    def test_delete_invalid_index(self):
        ok, msg = queue_state.delete_index(5)
        assert not ok


class TestThreadSafety:
    def test_set_expecting_ws(self):
        queue_state.set_expecting_ws(5)
        assert queue_state.get_expecting_ws() == 5

    def test_decrement_expecting_ws(self):
        queue_state.set_expecting_ws(2)
        val = queue_state.decrement_expecting_ws()
        assert val == 1
        val = queue_state.decrement_expecting_ws()
        assert val == 0
        # Should not go below 0
        val = queue_state.decrement_expecting_ws()
        assert val == 0


class TestSoundcloudPlayback:
    def test_switch_waits_for_outgoing_player_before_opening(self, monkeypatch):
        events = []
        monkeypatch.setattr(queue_state, "set_expecting_ws", lambda n: None)
        active = iter([[{"playerid": 0}], [{"playerid": 0}], []])

        def players():
            result = next(active)
            events.append("active" if result else "stopped")
            return result

        monkeypatch.setattr(queue_state.media, "cleanup_active_image_session", lambda: None)
        monkeypatch.setattr(queue_state.kodi_api, "stop_all_players", lambda: events.append("stop_requested"))
        monkeypatch.setattr(queue_state.kodi_api, "get_active_players", players)
        monkeypatch.setattr(queue_state.kodi_api, "kodi_clear_all_playlists", lambda: events.append("clear"))
        monkeypatch.setattr(queue_state.kodi_api, "maybe_cache_soundcloud_url", lambda url: None)
        monkeypatch.setattr(queue_state.kodi_api, "kodi_call", lambda method, params: events.append(method) or {})
        monkeypatch.setattr(queue_state, "schedule_soundcloud_plugin_fallback", lambda *args, **kwargs: events.append("probe"))
        monkeypatch.setattr(queue_state, "schedule_playback_refresh", lambda: None)
        queue_state.play_item(queue_state.make_soundcloud("https://soundcloud.com/artist/new"))
        assert events == [
            "JSONRPC.NotifyAll", "stop_requested", "active", "active", "stopped", "clear", "Player.Open", "probe",
        ]

    def test_stop_wait_times_out_or_cancels(self, monkeypatch):
        monkeypatch.setattr(queue_state.kodi_api, "get_active_players", lambda: [{"playerid": 0}])
        event = threading.Event()
        assert not queue_state.wait_for_players_stopped(event, timeout_s=0)
        event.set()
        assert not queue_state.wait_for_players_stopped(event)

    def test_fallback_opens_resolved_entry_instead_of_placeholder(self, monkeypatch):
        workers = []
        calls = []
        resolved = "plugin://plugin.audio.soundcloud/play/?media_url=resolved"
        monkeypatch.setattr(queue_state.threading, "Thread", lambda target, daemon: type(
            "Worker", (), {"start": lambda self: workers.append(target)},
        )())
        monkeypatch.setattr(queue_state, "SC_PLUGIN_START_TIMEOUT_S", 0)
        monkeypatch.setattr(queue_state, "schedule_playback_refresh", lambda: None)
        monkeypatch.setattr(queue_state, "soundcloud_playback_started", lambda *args: True)

        def rpc(method, params):
            if method == "Playlist.GetItems":
                return {"result": {"items": [
                    {"file": "plugin://plugin.audio.soundcloud/play/?url=placeholder"},
                    {"file": resolved},
                ]}}
            calls.append((method, params))
            return {"result": "OK"}

        monkeypatch.setattr(queue_state.kodi_api, "kodi_call", rpc)
        queue_state.schedule_soundcloud_plugin_fallback(
            {}, "https://soundcloud.com/artist/new", cancel_event=threading.Event(),
        )
        workers[0]()
        assert calls == [("Player.Open", {"item": {"playlistid": 0, "position": 1}})]

    @pytest.mark.parametrize("cancelled", [False, True])
    def test_resolved_start_does_not_open_changed_playlist(self, monkeypatch, cancelled):
        event = threading.Event()
        resolved = "plugin://plugin.audio.soundcloud/play/?media_url=resolved"

        def rpc(method, params):
            assert method == "Playlist.GetItems"
            if cancelled:
                event.set()
                return {"result": {"items": [{"file": resolved}]}}
            return {"result": {"items": [{"file": "another-track"}]}}

        monkeypatch.setattr(queue_state.kodi_api, "kodi_call", rpc)
        assert not queue_state.open_soundcloud_resolved_playlist_item(
            "Track", "https://soundcloud.com/artist/track", resolved_url=resolved, cancel_event=event,
        )

    def test_is_soundcloud_item_detects_plugin_url_without_resolver(self):
        item = {
            "title": "artist - track",
            "url": "plugin://plugin.audio.soundcloud/play/?url=https://soundcloud.com/artist/track",
            "kind": "audio",
            "link": "",
            "resolver": None,
        }

        assert queue_state.is_soundcloud_item(item) is True

    def test_play_item_opens_soundcloud_plugin_directly(self, monkeypatch):
        calls = []

        monkeypatch.setattr(queue_state.media, "cleanup_active_image_session", lambda: None)
        monkeypatch.setattr(queue_state.kodi_api, "stop_all_players", lambda: calls.append(("stop_all_players", None)))
        monkeypatch.setattr(queue_state.kodi_api, "kodi_clear_all_playlists", lambda: calls.append(("kodi_clear_all_playlists", None)))
        monkeypatch.setattr(queue_state.kodi_api, "maybe_cache_soundcloud_url", lambda url: calls.append(("maybe_cache_soundcloud_url", url)))
        monkeypatch.setattr(queue_state.kodi_api, "kodi_call", lambda method, params=None: calls.append((method, params)) or {})
        monkeypatch.setattr(queue_state.kodi_api, "get_active_players", lambda: [])
        monkeypatch.setattr(queue_state, "set_expecting_ws", lambda n: calls.append(("set_expecting_ws", n)))
        monkeypatch.setattr(queue_state, "schedule_playback_refresh", lambda: calls.append(("schedule_playback_refresh", None)))
        monkeypatch.setattr(
            queue_state,
            "schedule_soundcloud_plugin_fallback",
            lambda item, source_link, resume_time=None, cancel_event=None: calls.append(
                ("schedule_soundcloud_plugin_fallback", source_link, resume_time)
            ),
        )

        item = queue_state.make_soundcloud("https://soundcloud.com/artist/track")
        queue_state.play_item(item)

        rpc_calls = [call for call in calls if call[0] in ("Playlist.Add", "Player.Open", "Playlist.Clear")]
        assert rpc_calls == [
            ("Player.Open", {"item": {"file": item["url"]}}),
        ]
        assert ("schedule_soundcloud_plugin_fallback", "https://soundcloud.com/artist/track", None) in calls

    def test_play_item_uses_soundcloud_plugin_direct_path_without_resolver(self, monkeypatch):
        calls = []
        plugin_url = "plugin://plugin.audio.soundcloud/play/?url=https://soundcloud.com/artist/track"

        monkeypatch.setattr(queue_state.media, "cleanup_active_image_session", lambda: None)
        monkeypatch.setattr(queue_state.kodi_api, "stop_all_players", lambda: calls.append(("stop_all_players", None)))
        monkeypatch.setattr(queue_state.kodi_api, "kodi_clear_all_playlists", lambda: calls.append(("kodi_clear_all_playlists", None)))
        monkeypatch.setattr(queue_state.kodi_api, "maybe_cache_soundcloud_url", lambda url: calls.append(("maybe_cache_soundcloud_url", url)))
        monkeypatch.setattr(queue_state.kodi_api, "kodi_call", lambda method, params=None: calls.append((method, params)) or {})
        monkeypatch.setattr(queue_state.kodi_api, "get_active_players", lambda: [])
        monkeypatch.setattr(queue_state, "set_expecting_ws", lambda n: calls.append(("set_expecting_ws", n)))
        monkeypatch.setattr(queue_state, "schedule_playback_refresh", lambda: calls.append(("schedule_playback_refresh", None)))
        monkeypatch.setattr(
            queue_state,
            "schedule_soundcloud_plugin_fallback",
            lambda item, source_link, resume_time=None, cancel_event=None: calls.append(
                ("schedule_soundcloud_plugin_fallback", source_link, resume_time)
            ),
        )

        item = {
            "title": "artist - track",
            "url": plugin_url,
            "kind": "audio",
            "link": "",
            "resolver": None,
        }
        queue_state.play_item(item)

        rpc_calls = [call for call in calls if call[0] in ("Playlist.Add", "Player.Open", "Playlist.Clear")]
        assert rpc_calls == [
            ("Player.Open", {"item": {"file": plugin_url}}),
        ]
        assert ("maybe_cache_soundcloud_url", "https://soundcloud.com/artist/track") in calls
        assert ("schedule_soundcloud_plugin_fallback", "https://soundcloud.com/artist/track", None) in calls

    def test_soundcloud_playback_started_matches_plugin_or_stream(self, monkeypatch):
        item_calls = []

        monkeypatch.setattr(queue_state.kodi_api, "get_active_playerid", lambda: 1)

        def fake_kodi_call(method, params=None):
            item_calls.append((method, params))
            if method == "Player.GetProperties":
                return {"result": {"speed": 1}}
            assert method == "Player.GetItem"
            return {
                "result": {
                    "item": {
                        "file": "https://playback.media-streaming.soundcloud.cloud/x/y/playlist.m3u8",
                        "title": "",
                        "artist": [],
                    }
                }
            }

        monkeypatch.setattr(queue_state.kodi_api, "kodi_call", fake_kodi_call)

        assert queue_state.soundcloud_playback_started(
            "https://soundcloud.com/artist/track", threading.Event(),
        ) is True
        assert item_calls[0][0] == "Player.GetItem"

    @pytest.mark.parametrize("speed", [0, None])
    def test_paused_or_unknown_stream_is_not_started(self, monkeypatch, speed):
        monkeypatch.setattr(queue_state.kodi_api, "get_active_playerid", lambda: 0)

        def rpc(method, params=None):
            if method == "Player.GetItem":
                return {"result": {"item": {"file": "https://cf-media.sndcdn.com/track.mp3"}}}
            assert method == "Player.GetProperties"
            return {"result": {"speed": speed}}

        monkeypatch.setattr(queue_state.kodi_api, "kodi_call", rpc)
        assert not queue_state.soundcloud_playback_started("https://soundcloud.com/artist/track")

    @pytest.mark.parametrize("cancelled", [False, True])
    def test_start_recovers_paused_stream_unless_cancelled(self, monkeypatch, cancelled):
        cancel_event = threading.Event()
        calls = []
        monkeypatch.setattr(queue_state.kodi_api, "get_active_playerid", lambda: 0)

        def rpc(method, params=None):
            if method == "Player.GetItem":
                return {"result": {"item": {"file": "https://cf-media.sndcdn.com/track.mp3"}}}
            if method == "Player.GetProperties":
                if cancelled:
                    cancel_event.set()  # A new request arrives during the status RPC.
                return {"result": {"speed": 0}}
            calls.append((method, params))
            return {"result": {"speed": 1}}

        monkeypatch.setattr(queue_state.kodi_api, "kodi_call", rpc)
        assert queue_state.soundcloud_playback_started(
            "https://soundcloud.com/artist/track", cancel_event,
        ) is (not cancelled)
        assert calls == ([] if cancelled else [
            ("Player.PlayPause", {"playerid": 0, "play": True}),
        ])

    def test_old_fallback_does_not_reopen_playlist_after_track_change(self, monkeypatch):
        workers = []
        monkeypatch.setattr(queue_state, "_SC_START_CANCEL", threading.Event())
        monkeypatch.setattr(queue_state.threading, "Thread", lambda target, daemon: type(
            "Worker", (), {"start": lambda self: workers.append(target)},
        )())
        monkeypatch.setattr(queue_state, "SC_PLUGIN_START_TIMEOUT_S", 0)

        def resolve():
            queue_state.cancel_soundcloud_start()
            return "plugin://plugin.audio.soundcloud/play/?media_url=old"

        monkeypatch.setattr(queue_state, "resolve_soundcloud_playlist_media_url", resolve)
        monkeypatch.setattr(queue_state, "open_soundcloud_resolved_playlist_item",
                            lambda *args, **kwargs: pytest.fail("Stale fallback reopened playback"))
        queue_state.schedule_soundcloud_plugin_fallback({}, "https://soundcloud.com/artist/old")
        workers[0]()

    def test_resolve_soundcloud_playlist_media_url_finds_resolved_entry(self, monkeypatch):
        def fake_kodi_call(method, params=None):
            assert method == "Playlist.GetItems"
            return {
                "result": {
                    "items": [
                        {"file": "plugin://plugin.audio.soundcloud/play/?url=https://soundcloud.com/artist/track"},
                        {"file": "plugin://plugin.audio.soundcloud/play/?media_url=https%3A%2F%2Fapi-v2.soundcloud.com%2Fmedia%2Fsoundcloud%3Atracks%3A1%2Fstream"},
                    ]
                }
            }

        monkeypatch.setattr(queue_state.kodi_api, "kodi_call", fake_kodi_call)

        result = queue_state.resolve_soundcloud_playlist_media_url(timeout_s=0.1, interval_s=0.01)

        assert result == "plugin://plugin.audio.soundcloud/play/?media_url=https%3A%2F%2Fapi-v2.soundcloud.com%2Fmedia%2Fsoundcloud%3Atracks%3A1%2Fstream"

    def test_open_soundcloud_resolved_playlist_item_starts_playlist_zero(self, monkeypatch):
        calls = []

        monkeypatch.setattr(queue_state.kodi_api, "kodi_call", lambda method, params=None: calls.append((method, params)) or {})
        monkeypatch.setattr(queue_state, "schedule_playback_refresh", lambda: calls.append(("schedule_playback_refresh", None)))

        ok = queue_state.open_soundcloud_resolved_playlist_item(
            "artist - track",
            "https://soundcloud.com/artist/track",
        )

        assert ok is True
        assert calls == [
            ("Player.Open", {"item": {"playlistid": 0, "position": 0}}),
            ("schedule_playback_refresh", None),
        ]


class _FakeResponse:
    def __init__(self, payload, ok=True, status_code=200):
        self._payload = payload
        self.ok = ok
        self.status_code = status_code

    def json(self):
        return self._payload


class TestSoundcloudDirectResolve:
    def test_resolve_soundcloud_stream_url_prefers_api(self, monkeypatch):
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append((url, params, timeout))
            if url == "https://api-v2.soundcloud.com/resolve":
                return _FakeResponse(
                    {
                        "media": {
                            "transcodings": [
                                {
                                    "url": "https://api-v2.soundcloud.com/media/soundcloud:tracks:1/progressive",
                                    "preset": "mp3_0_1",
                                    "format": {"protocol": "progressive", "mime_type": "audio/mpeg"},
                                }
                            ]
                        }
                    }
                )
            return _FakeResponse({"url": "https://cf-media.sndcdn.com/direct.mp3"})

        monkeypatch.setattr(queue_state.kodi_api, "read_soundcloud_client_id", lambda: "client-id")
        monkeypatch.setattr(queue_state.HTTP, "get", fake_get)

        result = queue_state.resolve_soundcloud_stream_url("https://soundcloud.com/artist/track")

        assert result == "https://cf-media.sndcdn.com/direct.mp3"
        assert calls[0][0] == "https://api-v2.soundcloud.com/resolve"
        assert calls[1][0] == "https://api-v2.soundcloud.com/media/soundcloud:tracks:1/progressive"

    def test_resolve_soundcloud_stream_url_falls_back_to_ytdlp(self, monkeypatch):
        monkeypatch.setattr(queue_state, "_resolve_soundcloud_stream_url_via_api", lambda url: "")
        monkeypatch.setattr(queue_state, "_resolve_soundcloud_stream_url_via_ytdlp", lambda url: "https://cf-media.sndcdn.com/fallback.mp3")

        result = queue_state.resolve_soundcloud_stream_url("https://soundcloud.com/artist/track")

        assert result == "https://cf-media.sndcdn.com/fallback.mp3"


def test_handle_ws_play_runs_on_play_started_hook():
    """Every playback start must reset the UI layer's per-file state; the
    hook is how core reaches the telegram layer without importing it."""
    calls = []
    with patch("kodibot.core.queue_state.ON_PLAY_STARTED", lambda: calls.append(1)), \
         patch("kodibot.core.queue_state.BOT_EXPECTING_WS", 1):
        queue_state._handle_ws_play(item={}, item_params={})
    assert calls == [1]


def test_handle_ws_play_survives_a_failing_on_play_started_hook():
    """A broken hook must not take the websocket listener down with it."""
    def boom():
        raise RuntimeError("hook exploded")

    with patch("kodibot.core.queue_state.ON_PLAY_STARTED", boom), \
         patch("kodibot.core.queue_state.BOT_EXPECTING_WS", 1):
        queue_state._handle_ws_play(item={}, item_params={})


class TestOverduePlayStart:
    """A play Kodi never confirms must show up in the bot log."""

    ITEM = {"kind": "video", "title": "Clip", "url": "plugin://plugin.video.youtube/play/?video_id=abc"}

    def _waiting(self, expecting=2, reported=False):
        return patch.multiple(
            "kodibot.core.queue_state",
            BOT_EXPECTING_WS=expecting,
            PLAY_START_TS=1000.0,
            PLAY_START_ITEM=self.ITEM,
            PLAY_START_REPORTED=reported,
        )

    def test_reports_once_after_the_deadline(self, caplog):
        late = 1000.0 + queue_state.PLAY_START_OVERDUE_SEC + 1
        with self._waiting(), caplog.at_level("WARNING", logger="kodibot.core.queue_state"):
            assert queue_state.report_overdue_play_start(late) is True
            assert queue_state.report_overdue_play_start(late + 5) is False
        messages = [r.getMessage() for r in caplog.records]
        assert len(messages) == 1
        assert "video_id=abc" in messages[0]

    def test_silent_before_the_deadline(self, caplog):
        with self._waiting(), caplog.at_level("WARNING", logger="kodibot.core.queue_state"):
            assert queue_state.report_overdue_play_start(1005.0) is False
        assert not caplog.records

    def test_silent_once_kodi_confirmed_the_play(self, caplog):
        late = 1000.0 + queue_state.PLAY_START_OVERDUE_SEC + 1
        with self._waiting(expecting=0), caplog.at_level("WARNING", logger="kodibot.core.queue_state"):
            assert queue_state.report_overdue_play_start(late) is False
        assert not caplog.records


class TestSeekRelative:
    """Live streams seek relatively and stop at the live edge."""

    def _seek(self, delta, props, labels=None, file=""):
        calls = []

        def fake_call(method, params=None):
            calls.append((method, params))
            if method == "Player.GetProperties":
                return {"result": props}
            if method == "XBMC.GetInfoLabels":
                return {"result": labels or {}}
            if method == "Player.GetItem":
                return {"result": {"item": {"file": file}}}
            if method == "Player.Seek":
                return {"result": {}}
            raise AssertionError(method)

        with patch.object(queue_state.kodi_api, "get_active_playerid", return_value=0), \
                patch.object(queue_state.kodi_api, "kodi_call", side_effect=fake_call):
            ok = queue_state.seek_relative_seconds(delta)
        seeks = [params["value"] for method, params in calls if method == "Player.Seek"]
        return ok, seeks

    def _live_props(self):
        # Kodi reports zero times for PVR channels, even with timeshift.
        zero = {"hours": 0, "minutes": 0, "seconds": 0}
        return {"canseek": True, "live": True, "time": zero, "totaltime": zero}

    def test_non_live_seeks_to_absolute_time(self):
        props = {
            "canseek": True,
            "live": False,
            "time": {"hours": 0, "minutes": 0, "seconds": 10},
            "totaltime": {"hours": 0, "minutes": 1, "seconds": 0},
        }

        ok, seeks = self._seek(10, props)

        assert ok is True
        assert seeks == [{"time": {"hours": 0, "minutes": 0, "seconds": 20}}]

    def test_live_seeks_back_relatively(self):
        ok, seeks = self._seek(-10, self._live_props())

        assert ok is True
        assert seeks == [{"seconds": -10}]

    def test_live_forward_within_buffer(self):
        ok, seeks = self._seek(10, self._live_props(), {"Player.Time": "01:00", "Player.Duration": "01:25"})

        assert ok is True
        assert seeks == [{"seconds": 10}]

    def test_live_forward_stops_at_live_edge(self):
        ok, seeks = self._seek(30, self._live_props(), {"Player.Time": "01:00", "Player.Duration": "01:25"})

        assert ok is True
        assert seeks == [{"seconds": 25}]

    def test_live_forward_at_edge_does_nothing(self):
        ok, seeks = self._seek(10, self._live_props(), {"Player.Time": "01:25", "Player.Duration": "01:25"})

        assert ok is True
        assert seeks == []

    def test_live_forward_without_known_edge_fails(self):
        ok, seeks = self._seek(10, self._live_props(), {})

        assert ok is False
        assert seeks == []

    # YouTube live: Kodi reports a plain item, 30 s over HLS (YouTube's window)
    # and 4 h over DASH (the add-on's timeshift buffer). Its times cannot tell
    # how far behind live playback is; the pauses since the start can.
    YT_LIVE_FILE = (
        "https://manifest.googlevideo.com/api/manifest/hls_variant"
        "/id/82O6yOy_XwE.22/source/yt_live_broadcast/file/index.m3u8"
    )

    def setup_method(self):
        queue_state.PAUSED_AT = None
        queue_state.PAUSED_TOTAL_S = 0.0

    def _yt_props(self, seconds, total=30):
        return {
            "canseek": True,
            "live": False,
            "time": {"hours": 0, "minutes": 0, "seconds": seconds},
            "totaltime": {"hours": total // 3600, "minutes": total % 3600 // 60, "seconds": total % 60},
        }

    def test_youtube_live_forward_never_skips_the_track(self):
        queue_state.PAUSED_TOTAL_S = 8.0

        with patch.object(queue_state, "skip_queue", side_effect=AssertionError("skipped")):
            ok, seeks = self._seek(30, self._yt_props(25), file=self.YT_LIVE_FILE)

        assert ok is True
        assert seeks == [{"seconds": 8}]
        assert queue_state.PAUSED_TOTAL_S == 0.0

    def test_youtube_live_forward_at_live_does_nothing(self):
        ok, seeks = self._seek(10, self._yt_props(16), file=self.YT_LIVE_FILE)

        assert ok is True
        assert seeks == []

    def test_youtube_live_forward_within_the_offset(self):
        queue_state.PAUSED_TOTAL_S = 8.0

        ok, seeks = self._seek(5, self._yt_props(5), file=self.YT_LIVE_FILE)

        assert ok is True
        assert seeks == [{"seconds": 5}]
        assert queue_state.PAUSED_TOTAL_S == 3.0

    def test_hls_youtube_live_back_stops_at_window_start(self):
        ok, seeks = self._seek(-30, self._yt_props(14), file=self.YT_LIVE_FILE)

        assert ok is True
        assert seeks == [{"seconds": -14}]
        assert queue_state.PAUSED_TOTAL_S == 14.0

    def test_dash_youtube_live_back_uses_the_buffer(self):
        ok, seeks = self._seek(-60, self._yt_props(7, total=4 * 3600), file=self.YT_LIVE_FILE)

        assert ok is True
        assert seeks == [{"seconds": -60}]
        assert queue_state.PAUSED_TOTAL_S == 60.0


class TestPlayPause:
    """Over HLS a YouTube live stream paused too long reopens at the live edge."""

    YT_LIVE_FILE = TestSeekRelative.YT_LIVE_FILE

    def setup_method(self):
        queue_state.QUEUE.clear()
        queue_state.CURRENT_INDEX = None
        queue_state.PAUSED_AT = None
        queue_state.PAUSED_TOTAL_S = 0.0

    def _toggle(self, speed, file="", total=30):
        calls = []

        def fake_call(method, params=None):
            calls.append((method, params))
            if method == "Player.GetProperties":
                return {"result": {"speed": speed, "totaltime": {"hours": total // 3600, "minutes": 0, "seconds": total % 3600}}}
            if method == "Player.GetItem":
                return {"result": {"item": {"file": file}}}
            return {"result": "OK"}

        played = []
        with patch.object(queue_state.kodi_api, "kodi_call", side_effect=fake_call), \
                patch.object(queue_state, "play_index", side_effect=played.append):
            queue_state.play_pause(1)
        return [method for method, _ in calls], calls, played

    def test_playing_stream_is_paused(self):
        methods, _calls, played = self._toggle(1, self.YT_LIVE_FILE)

        assert "Player.PlayPause" in methods
        assert played == []

    def test_paused_ordinary_item_resumes_in_place(self):
        methods, _calls, played = self._toggle(0, "smb://movies/film.mkv")

        assert "Player.PlayPause" in methods
        assert "Player.Open" not in methods
        assert played == []

    def test_short_pause_on_youtube_live_resumes_in_place(self):
        queue_state._handle_ws_pause()
        queue_state.PAUSED_AT -= queue_state.YT_LIVE_MAX_PAUSE_S - 2

        methods, _calls, played = self._toggle(0, self.YT_LIVE_FILE)

        assert "Player.PlayPause" in methods
        assert "Player.Open" not in methods
        assert played == []

    def test_pauses_add_up_until_the_next_start(self):
        queue_state._handle_ws_pause()
        queue_state.PAUSED_AT -= 7
        queue_state._handle_ws_resume()
        queue_state._handle_ws_pause()
        queue_state.PAUSED_AT -= 5

        assert round(queue_state.paused_behind_seconds()) == 12

        queue_state._handle_ws_resume()
        assert queue_state.PAUSED_AT is None
        assert round(queue_state.paused_behind_seconds()) == 12

        queue_state._handle_ws_play(item={}, item_params={})
        assert queue_state.paused_behind_seconds() == 0

    def test_many_short_pauses_reopen_like_one_long_pause(self):
        queue_state.PAUSED_TOTAL_S = queue_state.YT_LIVE_MAX_PAUSE_S - 1
        queue_state._handle_ws_pause()
        queue_state.PAUSED_AT -= 2

        methods, calls, _played = self._toggle(0, self.YT_LIVE_FILE)

        assert "Player.PlayPause" not in methods
        assert any(method == "Player.Open" for method, _ in calls)

    def test_paused_youtube_live_from_queue_restarts_queue_item(self):
        queue_state.QUEUE.append(queue_state.make_youtube("82O6yOy_XwE", "Star TV"))
        queue_state.CURRENT_INDEX = 0
        queue_state._handle_ws_pause()
        queue_state.PAUSED_AT -= queue_state.YT_LIVE_MAX_PAUSE_S + 1

        methods, _calls, played = self._toggle(0, self.YT_LIVE_FILE)

        assert played == [0]
        assert "Player.PlayPause" not in methods

    def test_dash_youtube_live_resumes_in_place_after_a_long_pause(self):
        queue_state._handle_ws_pause()
        queue_state.PAUSED_AT -= queue_state.YT_LIVE_MAX_PAUSE_S + 60

        methods, _calls, played = self._toggle(0, self.YT_LIVE_FILE, total=4 * 3600)

        assert "Player.PlayPause" in methods
        assert "Player.Open" not in methods
        assert played == []

    def test_paused_external_youtube_live_reopens_plugin_url(self):
        methods, calls, played = self._toggle(0, self.YT_LIVE_FILE)

        opened = [params for method, params in calls if method == "Player.Open"]
        assert opened == [{"item": {"file": "plugin://plugin.video.youtube/play/?video_id=82O6yOy_XwE"}}]
        assert "Player.PlayPause" not in methods
        assert played == []
