"""Tests for pure functions in queue_state.py"""
import os
import sys

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
            lambda item, source_link, resume_time=None: calls.append(
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
            lambda item, source_link, resume_time=None: calls.append(
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

        assert queue_state.soundcloud_playback_started("https://soundcloud.com/artist/track") is True
        assert item_calls[0][0] == "Player.GetItem"

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
