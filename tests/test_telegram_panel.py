"""Tests for now-playing panel logic."""
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

from kodibot.core import kodi_api, queue_state
from kodibot.telegram import panel


class TestNowPlayingText:
    def setup_method(self):
        queue_state.QUEUE.clear()
        queue_state.CURRENT_INDEX = None
        queue_state.DISPLAY_INDEX = None
        queue_state.NEXT_INDEX = 0
        queue_state.AUTOPLAY_ENABLED = True
        queue_state.EXTERNAL_PLAYBACK = False
        queue_state.LIST_DIRTY = False
        queue_state.LAST_PROGRESS_TS = 0.0
        queue_state.LAST_PROGRESS_TIME = None
        queue_state.LAST_PROGRESS_TOTAL = None
        queue_state.LAST_PROGRESS_INDEX = None
        kodi_api.LAST_WS_PLAYERID = None
        kodi_api.LAST_WS_ITEM.clear()
        kodi_api.WS_PLAYING = False

    def test_external_player_replaces_stale_queue_title(self, monkeypatch):
        queue_state.QUEUE.append(
            {"title": "Queued Song", "url": "plugin://queued", "kind": "video", "link": "https://queued.example"}
        )
        queue_state.CURRENT_INDEX = 0
        queue_state.DISPLAY_INDEX = 0

        async def fake_call(method, params=None):
            if method == "Player.GetActivePlayers":
                return {"result": [{"playerid": 1, "type": "video"}]}
            if method == "Player.GetProperties":
                return {
                    "result": {
                        "time": {"hours": 0, "minutes": 0, "seconds": 10},
                        "totaltime": {"hours": 0, "minutes": 1, "seconds": 0},
                    }
                }
            if method == "Player.GetItem":
                return {
                    "result": {
                        "item": {
                            "type": "movie",
                            "title": "External Movie",
                            "file": "smb://movies/external.mkv",
                            "label": "External Movie",
                        }
                    }
                }
            raise AssertionError(method)

        monkeypatch.setattr(panel.kodi_api, "kodi_call_async", fake_call)
        monkeypatch.setattr(panel.kodi_api, "pick_playerid", lambda players: 1)
        monkeypatch.setattr(panel.kodi_api, "kodi_item_matches_queue", lambda item, qitem: False)
        monkeypatch.setattr(panel.kodi_api, "external_item_display", lambda item: ("External Movie", "https://external.example"))
        monkeypatch.setattr(panel.kodi_api, "maybe_cache_soundcloud_url", lambda file_url: None)

        text, progress = asyncio.run(panel.get_now_playing_text())

        assert 'External Movie' in text
        assert 'https://external.example' in text
        assert progress == "00:10 / 01:00"
        assert queue_state.EXTERNAL_PLAYBACK is True
        assert queue_state.CURRENT_INDEX is None
        assert queue_state.DISPLAY_INDEX is None

    def test_matching_player_keeps_queue_title(self, monkeypatch):
        queue_state.QUEUE.append(
            {"title": "Queued Song", "url": "plugin://queued", "kind": "video", "link": "https://queued.example"}
        )
        queue_state.CURRENT_INDEX = 0
        queue_state.DISPLAY_INDEX = 0

        async def fake_call(method, params=None):
            if method == "Player.GetActivePlayers":
                return {"result": [{"playerid": 1, "type": "video"}]}
            if method == "Player.GetProperties":
                return {
                    "result": {
                        "time": {"hours": 0, "minutes": 0, "seconds": 5},
                        "totaltime": {"hours": 0, "minutes": 1, "seconds": 0},
                    }
                }
            if method == "Player.GetItem":
                return {
                    "result": {
                        "item": {
                            "type": "video",
                            "title": "Queued Song",
                            "file": "plugin://queued",
                            "label": "Queued Song",
                        }
                    }
                }
            raise AssertionError(method)

        monkeypatch.setattr(panel.kodi_api, "kodi_call_async", fake_call)
        monkeypatch.setattr(panel.kodi_api, "pick_playerid", lambda players: 1)
        monkeypatch.setattr(panel.kodi_api, "kodi_item_matches_queue", lambda item, qitem: True)
        monkeypatch.setattr(panel.kodi_api, "external_item_display", lambda item: (_ for _ in ()).throw(AssertionError("should not be called")))
        monkeypatch.setattr(panel.kodi_api, "maybe_cache_soundcloud_url", lambda file_url: None)

        text, progress = asyncio.run(panel.get_now_playing_text())

        assert 'Queued Song' in text
        assert 'https://queued.example' in text
        assert progress == "00:05 / 01:00"
        assert queue_state.EXTERNAL_PLAYBACK is False
        assert queue_state.CURRENT_INDEX == 0
        assert queue_state.DISPLAY_INDEX == 0
