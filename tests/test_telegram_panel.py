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


class TestControlPanelMarkup:
    def test_main_panel_includes_controls_button(self, monkeypatch):
        monkeypatch.setattr(panel.ha, "ha_available", lambda: False)

        markup = panel.control_panel(mode="main")

        assert markup.inline_keyboard[2][0].text == "🎛 Controls"
        assert markup.inline_keyboard[2][0].callback_data == "controls:menu"
        assert all(button.text != "-10s" for row in markup.inline_keyboard for button in row)

    def test_controls_panel_includes_seek_queue_and_back(self):
        markup = panel.control_panel(mode="controls")

        assert markup.inline_keyboard[0][0].callback_data == "seek:-10s"
        assert markup.inline_keyboard[2][0].callback_data == "seek:percent"
        assert markup.inline_keyboard[3][0].callback_data == "delete:ask"
        assert markup.inline_keyboard[4][0].callback_data == "plist:save"
        assert markup.inline_keyboard[-1][0].callback_data == "controls:back"

    def test_delete_confirm_markup_uses_yes_no_callbacks(self):
        markup = panel.delete_confirm_markup("abc")

        assert markup.inline_keyboard[0][0].text == "✅ Yes"
        assert markup.inline_keyboard[0][0].callback_data == "delete_confirm:abc:yes"
        assert markup.inline_keyboard[0][1].text == "❌ No"
        assert markup.inline_keyboard[0][1].callback_data == "delete_confirm:abc:no"


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

    def test_external_movie_replaces_stale_soundcloud_queue_title(self, monkeypatch):
        queue_state.QUEUE.append(
            {
                "title": "artist - track",
                "url": "plugin://plugin.audio.soundcloud/play/?url=https://soundcloud.com/artist/track",
                "kind": "audio",
                "link": "https://soundcloud.com/artist/track",
            }
        )
        queue_state.CURRENT_INDEX = 0
        queue_state.DISPLAY_INDEX = 0
        kodi_api.LAST_WS_SC_URL = "https://soundcloud.com/artist/track"

        async def fake_call(method, params=None):
            if method == "Player.GetActivePlayers":
                return {"result": [{"playerid": 1, "type": "video"}]}
            if method == "Player.GetProperties":
                return {
                    "result": {
                        "time": {"hours": 0, "minutes": 0, "seconds": 12},
                        "totaltime": {"hours": 1, "minutes": 30, "seconds": 0},
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
        monkeypatch.setattr(panel.kodi_api, "external_item_display", lambda item: ("External Movie", "https://www.imdb.com/title/tt1234567/"))
        monkeypatch.setattr(panel.kodi_api, "maybe_cache_soundcloud_url", lambda file_url: None)

        text, progress = asyncio.run(panel.get_now_playing_text())

        assert "External Movie" in text
        assert "https://www.imdb.com/title/tt1234567/" in text
        assert "soundcloud.com/artist/track" not in text
        assert progress == "00:12 / 1:30:00"
        assert queue_state.EXTERNAL_PLAYBACK is True


class TestAvStreamLabel:
    def test_language_only(self):
        stream = {"language": "deu", "index": 1}
        label = panel.av_stream_label(stream)
        assert label == "1. 🇩🇪 Deutsch"

    def test_technical_only(self):
        stream = {"name": "DD+5.1", "codec": "eac3", "channels": 6, "index": 2}
        label = panel.av_stream_label(stream)
        assert label == "2. DD+5.1 · eac3 · 6ch"

    def test_language_and_technical(self):
        stream = {"language": "deu", "name": "DD+5.1(sinde)", "codec": "eac3", "channels": 6, "index": 3}
        label = panel.av_stream_label(stream)
        assert label == "3. 🇩🇪 Deutsch · DD+5.1(sinde) · eac3 · 6ch"

    def test_redundant_language_in_name(self):
        stream = {"language": "eng", "name": "English", "codec": "eac3", "channels": 6, "index": 4}
        label = panel.av_stream_label(stream)
        assert label == "4. 🇬🇧 English · eac3 · 6ch"

    def test_language_extraction_from_name(self):
        stream = {"name": "DD+5.1(ger)", "codec": "eac3", "channels": 6, "index": 5}
        label = panel.av_stream_label(stream)
        assert label == "5. 🇩🇪 Deutsch · DD+5.1(ger) · eac3 · 6ch"

    def test_unknown_language_code(self):
        stream = {"language": "xyz", "index": 6}
        label = panel.av_stream_label(stream)
        assert label == "6. 🏳 XYZ"

    def test_combined_languages(self):
        stream = {"language": "deu/eng", "index": 7}
        label = panel.av_stream_label(stream)
        assert label == "7. 🇩🇪 Deutsch / 🇬🇧 English"

    def test_region_explicit(self):
        stream = {"language": "de-ch", "index": 8}
        label = panel.av_stream_label(stream)
        assert label == "8. 🇨🇭 Deutsch (CH)"

    def test_region_fallback(self):
        stream = {"language": "en-ca", "index": 9}
        label = panel.av_stream_label(stream)
        assert label == "9. 🇬🇧 English"


class TestCurrentSubtitleLabel:
    def test_subtitle_off_when_disabled(self):
        av_state = {
            "subtitleenabled": False,
            "currentsubtitle": {"index": 0, "name": "German Forced", "language": "deu"}
        }
        assert panel.current_subtitle_label(av_state) == "Off"

    def test_subtitle_off_when_no_index(self):
        av_state = {
            "subtitleenabled": True,
            "currentsubtitle": {"name": "German Forced", "language": "deu"}
        }
        assert panel.current_subtitle_label(av_state) == "Off"

    def test_subtitle_on_when_enabled(self):
        av_state = {
            "subtitleenabled": True,
            "currentsubtitle": {"index": 1, "name": "German Forced", "language": "deu"}
        }
        assert panel.current_subtitle_label(av_state) == "🇩🇪 Deutsch · German Forced"
