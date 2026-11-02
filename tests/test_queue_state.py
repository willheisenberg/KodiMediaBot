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


class TestYoutubeAudioOnly:
    VIDEO = queue_state.make_youtube("82O6yOy_XwE", "Clip")
    AUDIO_URL = VIDEO["url"] + "&audio_only=true"

    def _play(self, item, live=False):
        with patch.multiple(
            "kodibot.core.queue_state",
            release_spotify_device=lambda: None,
            kodi_add_to_playlist=lambda url, playlistid: added.append((url, playlistid)),
            schedule_playback_refresh=lambda: None,
            set_expecting_ws=lambda n: None,
        ), patch.object(queue_state.media, "cleanup_active_image_session"), patch.multiple(
            "kodibot.core.kodi_api",
            stop_all_players=lambda: None,
            kodi_clear_all_playlists=lambda: None,
            kodi_call=lambda *a, **k: {},
        ), patch.object(queue_state.youtube_live, "is_live", return_value=live) as is_live:
            added = []
            queue_state.play_item(item)
        return added, is_live

    def test_video_mode_opens_the_queued_url_without_a_lookup(self):
        with patch.object(queue_state, "YOUTUBE_AUDIO_ONLY", False):
            added, is_live = self._play(self.VIDEO)
        assert added == [(self.VIDEO["url"], 1)]
        is_live.assert_not_called()

    def test_audio_mode_opens_audio_and_leaves_the_queue_item_alone(self):
        item = dict(self.VIDEO)
        with patch.object(queue_state, "YOUTUBE_AUDIO_ONLY", True):
            added, _ = self._play(item)
        assert added == [(self.AUDIO_URL, 1)]
        assert item == self.VIDEO

    def test_audio_mode_keeps_live_streams_on_video(self):
        with patch.object(queue_state, "YOUTUBE_AUDIO_ONLY", True):
            added, _ = self._play(self.VIDEO, live=True)
        assert added == [(self.VIDEO["url"], 1)]

    def test_audio_mode_ignores_other_video_items(self):
        item = queue_state.make_item("Film", "smb://movies/film.mkv", "video")
        with patch.object(queue_state, "YOUTUBE_AUDIO_ONLY", True):
            assert queue_state.youtube_open_url(item) == "smb://movies/film.mkv"

    def test_toggle_flips_the_mode(self):
        with patch.object(queue_state, "YOUTUBE_AUDIO_ONLY", False):
            assert queue_state.toggle_youtube_audio_only() is True
            assert queue_state.toggle_youtube_audio_only() is False

    def _reopen(self, item, file_url, live=None, pid=1):
        position = {"hours": 0, "minutes": 1, "seconds": 2, "milliseconds": 0}
        result = {"result": {"time": position, "totaltime": {"hours": 0, "minutes": 3, "seconds": 0}}}
        with patch.multiple(
            "kodibot.core.queue_state", QUEUE=[item], DISPLAY_INDEX=0, _player_file=lambda p: file_url,
        ), patch.object(queue_state.kodi_api, "get_active_playerid", return_value=pid), patch.object(
            queue_state.kodi_api, "kodi_call", return_value=result,
        ), patch.object(queue_state.youtube_live, "live_video_id", return_value=live), patch.object(
            queue_state, "resume_item_at_time",
        ) as resume:
            queue_state.reopen_current_youtube()
        return resume, position

    def test_reopen_resumes_the_playing_video_at_its_position(self):
        resume, position = self._reopen(self.VIDEO, self.VIDEO["url"])
        resume.assert_called_once_with(self.VIDEO, position)

    def test_reopen_leaves_a_live_stream_alone(self):
        resume, _ = self._reopen(self.VIDEO, self.VIDEO["url"], live="82O6yOy_XwE")
        resume.assert_not_called()

    def test_reopen_needs_a_running_player(self):
        resume, _ = self._reopen(self.VIDEO, self.VIDEO["url"], pid=None)
        resume.assert_not_called()

    def test_reopen_leaves_other_playback_alone(self):
        resume, _ = self._reopen(self.VIDEO, "http://radio.example/stream")
        resume.assert_not_called()

    def test_reopen_leaves_a_soundcloud_track_alone(self):
        item = queue_state.make_soundcloud("https://soundcloud.com/artist/track")
        resume, _ = self._reopen(item, item["url"])
        resume.assert_not_called()


class TestResumeSeek:
    """The seek has to reach the new player, however long it takes to open."""

    TARGET = {"hours": 0, "minutes": 2, "seconds": 0, "milliseconds": 0}

    @staticmethod
    def _at(sec, total=300, canseek=True):
        clock = {"hours": 0, "minutes": sec // 60, "seconds": sec % 60, "milliseconds": 0}
        length = {"hours": 0, "minutes": total // 60, "seconds": total % 60, "milliseconds": 0}
        return {"time": clock, "totaltime": length, "canseek": canseek}

    def _run(self, states, starts=1, starts_seen=0, wait_sec=5, cancel=None):
        """Run the seek against a player whose state advances with every read."""
        states = list(states)
        seeks = []

        def kodi_call(method, params=None):
            if method == "Player.Seek":
                seeks.append(params["value"]["time"])
                return {}
            return {"result": states.pop(0) if len(states) > 1 else states[0]}

        with patch.multiple(
            "kodibot.core.queue_state",
            PLAY_STARTS=starts,
            RESUME_SEEK_POLL_SEC=0.001,
            RESUME_SEEK_SETTLE_SEC=0.001,
            RESUME_SEEK_WAIT_SEC=wait_sec,
        ), patch.object(queue_state.kodi_api, "get_active_players", return_value=[{"playerid": 1}]), patch.object(
            queue_state.kodi_api, "kodi_call", side_effect=kodi_call,
        ):
            queue_state._seek_until_at(self.TARGET, "test", starts_seen, cancel or threading.Event())
        return seeks

    def test_waits_until_the_new_player_accepts_a_seek(self):
        seeks = self._run([self._at(0, canseek=False), self._at(0, total=0), self._at(0), self._at(120)])
        assert seeks == [self.TARGET]

    def test_repeats_a_seek_kodi_dropped(self):
        seeks = self._run([self._at(1), self._at(2), self._at(120)])
        assert seeks == [self.TARGET, self.TARGET]

    def test_old_player_is_left_alone_until_kodi_reports_a_new_start(self):
        seeks = self._run([self._at(50)], starts=3, starts_seen=3, wait_sec=0.02)
        assert seeks == []

    def test_cancelled_start_stops_the_seek(self):
        cancel = threading.Event()
        cancel.set()
        assert self._run([self._at(0)], cancel=cancel) == []

    def test_ws_play_event_counts_as_a_new_start(self):
        with patch.multiple(
            "kodibot.core.queue_state", PLAY_STARTS=4, BOT_EXPECTING_WS=1, ON_PLAY_STARTED=None,
        ):
            queue_state._handle_ws_play(item={}, item_params={})
            assert queue_state.PLAY_STARTS == 5

    def test_play_item_hands_over_the_count_from_before_the_open(self):
        seen = {}

        def kodi_call(method, params=None):
            if method == "Player.Open":
                queue_state.PLAY_STARTS += 1
            return {}

        with patch.multiple(
            "kodibot.core.queue_state",
            PLAY_STARTS=7,
            release_spotify_device=lambda: None,
            kodi_add_to_playlist=lambda url, playlistid: None,
            schedule_playback_refresh=lambda: None,
            set_expecting_ws=lambda n: None,
            seek_when_player_ready=lambda t, ctx, starts, cancel: seen.update(starts=starts),
        ), patch.object(queue_state.media, "cleanup_active_image_session"), patch.multiple(
            "kodibot.core.kodi_api",
            stop_all_players=lambda: None,
            kodi_clear_all_playlists=lambda: None,
            kodi_call=kodi_call,
        ):
            queue_state.play_item(queue_state.make_youtube("82O6yOy_XwE", "Clip"), resume_time=self.TARGET)
        assert seen == {"starts": 7}


class TestSoundcloudSets:
    DISCOVER = "https://soundcloud.com/discover/sets/liked-by::350337"
    API = "https://api-v2.soundcloud.com/tracks/"

    def test_discover_link_is_a_set_not_a_track(self):
        assert queue_state.is_sc_set_url(self.DISCOVER)
        assert not queue_state.is_sc_track_url(self.DISCOVER)

    def test_discover_link_cut_off_before_its_name_is_neither(self):
        assert not queue_state.is_sc_set_url("https://soundcloud.com/discover/sets")
        assert not queue_state.is_sc_track_url("https://soundcloud.com/discover/sets")

    def _expand(self, entries, lookups):
        def extract_info(url, download=False, process=True):
            if process:
                return {"entries": entries}
            answer = lookups[url]
            if isinstance(answer, Exception):
                raise answer
            return {"webpage_url": answer}

        with patch.object(queue_state, "YoutubeDL") as ydl:
            ydl.return_value.__enter__.return_value.extract_info.side_effect = extract_info
            return queue_state.expand_soundcloud_set(self.DISCOVER)

    def test_api_entries_become_track_links_in_set_order(self):
        result = self._expand(
            [{"url": self.API + "1"}, {"url": "https://soundcloud.com/a/direct"}, {"url": self.API + "2"}],
            {self.API + "1": "https://soundcloud.com/a/one", self.API + "2": "https://soundcloud.com/a/two"},
        )
        assert result == [
            "https://soundcloud.com/a/one",
            "https://soundcloud.com/a/direct",
            "https://soundcloud.com/a/two",
        ]

    def test_failed_lookup_drops_only_that_track(self, caplog):
        with caplog.at_level("WARNING", logger="kodibot.core.queue_state"):
            result = self._expand(
                [{"url": self.API + "1"}, {"url": self.API + "2"}],
                {self.API + "1": RuntimeError("gone"), self.API + "2": "https://soundcloud.com/a/two"},
            )
        assert result == ["https://soundcloud.com/a/two"]
        assert "/tracks/1" in caplog.text


class TestVisualWindowCheck:
    VIS = {"result": {"currentwindow": {"id": queue_state.VISUALISATION_WINDOW_ID, "label": "Visualisation"}}}
    HOME = {"result": {"currentwindow": {"id": 10000, "label": "Home"}}}

    def _run(self, windows, players=None, delays=(0, 0), current=True, **state):
        windows = list(windows)
        calls = []

        def kodi_call(method, params=None):
            calls.append((method, params))
            return windows.pop(0) if method == "GUI.GetProperties" else {}

        check = threading.Event()
        defaults = dict(BOT_EXPECTING_WS=0, LAST_PLAYED_RADIO=None, AUTOPLAY_ENABLED=False, NEXT_INDEX=0, QUEUE=[])
        with patch.multiple(
            "kodibot.core.queue_state",
            VISUAL_WINDOW_CHECK_DELAYS_SEC=delays,
            _VISUAL_WINDOW_CHECK=check if current else threading.Event(),
            **{**defaults, **state},
        ), patch.object(queue_state.kodi_api, "get_active_players", return_value=players or []), patch.object(
            queue_state.kodi_api, "kodi_call", side_effect=kodi_call,
        ):
            queue_state._check_visual_window(check)
        return [c for c in calls if c[0] == "GUI.ActivateWindow"], calls

    def test_leftover_window_goes_home(self):
        left, _ = self._run([self.VIS])
        assert left == [("GUI.ActivateWindow", {"window": "home"})]

    def test_window_that_opens_late_is_caught_by_the_second_look(self):
        left, _ = self._run([self.HOME, self.VIS])
        assert left == [("GUI.ActivateWindow", {"window": "home"})]

    def test_other_windows_stay(self):
        left, _ = self._run([self.HOME, self.HOME])
        assert left == []

    def test_failed_kodi_call_changes_nothing(self):
        left, _ = self._run([{}, {}])
        assert left == []

    def test_running_player_keeps_the_window(self):
        left, calls = self._run([self.VIS], players=[{"playerid": 0}])
        assert left == []
        assert calls == []

    def test_starting_track_keeps_the_window(self):
        left, calls = self._run([self.VIS], BOT_EXPECTING_WS=2)
        assert left == []
        assert calls == []

    def test_next_queue_track_keeps_the_window(self):
        left, _ = self._run([self.VIS], AUTOPLAY_ENABLED=True, NEXT_INDEX=0, QUEUE=[{"url": "x"}])
        assert left == []

    def test_end_of_queue_goes_home(self):
        left, _ = self._run([self.VIS], AUTOPLAY_ENABLED=True, NEXT_INDEX=1, QUEUE=[{"url": "x"}])
        assert len(left) == 1

    def test_radio_reconnect_keeps_the_window(self):
        left, _ = self._run([self.VIS], LAST_PLAYED_RADIO={"url": "x"})
        assert left == []

    def test_newer_stop_replaces_this_check(self):
        left, calls = self._run([self.VIS], current=False)
        assert left == []
        assert calls == []

    def test_stop_event_schedules_a_check(self):
        with patch.object(queue_state, "_schedule_visual_window_check") as schedule, patch.object(
            queue_state, "schedule_now_playing_refresh",
        ):
            queue_state._handle_ws_stop(item_params={}, player_params={})
        schedule.assert_called_once_with()

    def _stop(self, players=(), **state):
        with patch.multiple(
            "kodibot.core.queue_state",
            release_spotify_device=lambda: None,
            schedule_playback_refresh=lambda: None,
            **state,
        ), patch.object(queue_state.media, "cleanup_active_image_session"), patch.multiple(
            "kodibot.core.kodi_api",
            get_active_players=lambda: list(players),
            stop_all_players=lambda: None,
            kodi_clear_all_playlists=lambda: None,
        ), patch.object(queue_state, "_schedule_visual_window_check") as schedule:
            queue_state.hard_stop_and_clear()
            return schedule, queue_state.BOT_EXPECTING_WS

    def test_stop_forgets_a_start_kodi_never_confirmed(self):
        # A leftover count would read as "a track is starting" and keep the window.
        _, expecting = self._stop(BOT_EXPECTING_WS=1)
        assert expecting == 0

    def test_stop_with_nothing_playing_checks_right_away(self):
        # Kodi sends no stop event then, so nothing else would leave the window.
        schedule, _ = self._stop()
        schedule.assert_called_once_with(delays=(0.0,))

    def test_stop_during_playback_leaves_the_check_to_the_stop_event(self):
        schedule, _ = self._stop(players=[{"playerid": 0, "type": "audio"}])
        schedule.assert_not_called()

    def test_given_delays_replace_the_default_ones(self):
        check = threading.Event()
        with patch.object(queue_state, "_VISUAL_WINDOW_CHECK", check), patch.object(
            queue_state, "_next_playback_pending", return_value=False,
        ), patch.object(queue_state.kodi_api, "get_active_players", return_value=[]), patch.object(
            queue_state.kodi_api, "kodi_call", side_effect=lambda method, params=None: self.VIS,
        ) as kodi_call, patch.object(queue_state.time, "sleep") as sleep:
            queue_state._check_visual_window(check, delays=(0.0,))
        sleep.assert_called_once_with(0.0)
        kodi_call.assert_any_call("GUI.ActivateWindow", {"window": "home"})
