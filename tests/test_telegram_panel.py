"""Tests for now-playing panel logic."""
import asyncio
import dataclasses
import json
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


class TestUiStatePersistence:
    def setup_method(self):
        panel.S.LIST_MSG_ID.clear()
        panel.S.PANEL_MSG_ID.clear()
        panel.S.FIRST_BOT_ID.clear()
        panel.S.LAST_BOT_ID.clear()
        panel.S.PREV_BOT_ID.clear()
        panel.S.LAST_CLEANUP_ID.clear()

    def test_save_ui_state_creates_parent_directory(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state" / "telegram_ui_state.json"
        monkeypatch.setattr(panel, "CFG", dataclasses.replace(panel.CFG, ui_state_file=str(state_file)))
        panel.S.PANEL_MSG_ID[123] = 456

        panel.save_ui_state()

        assert state_file.exists()
        assert json.loads(state_file.read_text(encoding="utf-8"))["panel_msg_id"] == {"123": 456}

    def test_load_ui_state_migrates_from_legacy_playlist_file(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state" / "telegram_ui_state.json"
        legacy_file = tmp_path / "playlists" / "telegram_ui_state.json"
        legacy_file.parent.mkdir()
        legacy_file.write_text(
            json.dumps({"panel_msg_id": {"123": 456}, "list_msg_id": {"123": 455}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(panel, "CFG", dataclasses.replace(panel.CFG, ui_state_file=str(state_file)))
        monkeypatch.setattr(panel, "LEGACY_UI_STATE_FILE", str(legacy_file))

        panel.load_ui_state()

        assert panel.S.PANEL_MSG_ID == {123: 456}
        assert panel.S.LIST_MSG_ID == {123: 455}
        assert json.loads(state_file.read_text(encoding="utf-8"))["panel_msg_id"] == {"123": 456}


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


class TestDisplayPowerButtons:
    def test_buttons_use_configured_label_and_callbacks(self, monkeypatch):
        import dataclasses

        monkeypatch.setattr(panel.ha, "ha_available", lambda: False)
        monkeypatch.setattr(
            panel, "CFG", dataclasses.replace(panel.CFG, display_button_label="📺 TV")
        )

        markup = panel.control_panel(mode="main")
        row = markup.inline_keyboard[6]

        assert row[0].text == "📺 TV On"
        assert row[0].callback_data == "display:on"
        assert row[1].text == "📺 TV Off"
        assert row[1].callback_data == "display:off"


class TestPanelSectionFlags:
    """Each optional button group can be hidden on its own."""

    def _panel(self, monkeypatch, ha_available=True, **flags):
        import dataclasses

        monkeypatch.setattr(panel.ha, "ha_available", lambda: ha_available)
        monkeypatch.setattr(panel, "CFG", dataclasses.replace(panel.CFG, **flags))
        return panel.control_panel(mode="main")

    def _callbacks(self, markup):
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    def _status_parts(self, monkeypatch, progress_text=None, **flags):
        import dataclasses

        monkeypatch.setattr(panel, "CFG", dataclasses.replace(panel.CFG, **flags))
        return panel.build_panel_status_parts(progress_text)

    def test_all_sections_visible_by_default(self, monkeypatch):
        cbs = self._callbacks(self._panel(monkeypatch))

        for expected in ("vol:up5", "hifi:on", "display:on", "airplay:kill", "ha:menu"):
            assert expected in cbs

    def test_volume_row_can_be_hidden(self, monkeypatch):
        cbs = self._callbacks(self._panel(monkeypatch, panel_show_volume=False))

        assert not any(c.startswith("vol:") for c in cbs)
        assert "hifi:on" in cbs

    def test_hifi_row_can_be_hidden(self, monkeypatch):
        cbs = self._callbacks(self._panel(monkeypatch, panel_show_hifi=False))

        assert not any(c.startswith("hifi:") for c in cbs)
        assert "vol:up5" in cbs

    def test_display_row_can_be_hidden(self, monkeypatch):
        cbs = self._callbacks(self._panel(monkeypatch, panel_show_display=False))

        assert not any(c.startswith("display:") for c in cbs)
        assert "hifi:on" in cbs

    def test_airplay_can_be_hidden_leaving_ha(self, monkeypatch):
        cbs = self._callbacks(self._panel(monkeypatch, panel_show_airplay=False))

        assert "airplay:kill" not in cbs
        assert "ha:menu" in cbs

    def test_ha_can_be_hidden_leaving_airplay(self, monkeypatch):
        cbs = self._callbacks(self._panel(monkeypatch, panel_show_ha=False))

        assert "ha:menu" not in cbs
        assert "airplay:kill" in cbs

    def test_last_row_disappears_when_both_hidden(self, monkeypatch):
        markup = self._panel(
            monkeypatch, panel_show_airplay=False, panel_show_ha=False
        )
        cbs = self._callbacks(markup)

        assert "airplay:kill" not in cbs
        assert "ha:menu" not in cbs
        assert all(len(row) > 0 for row in markup.inline_keyboard)

    def test_airplay_survives_without_home_assistant(self, monkeypatch):
        """AirPlay Kill is a CEC command and no longer depends on HA."""
        cbs = self._callbacks(self._panel(monkeypatch, ha_available=False))

        assert "airplay:kill" in cbs
        assert "ha:menu" not in cbs

    def test_all_optional_sections_off(self, monkeypatch):
        markup = self._panel(
            monkeypatch,
            panel_show_volume=False,
            panel_show_hifi=False,
            panel_show_display=False,
            panel_show_airplay=False,
            panel_show_ha=False,
        )
        cbs = self._callbacks(markup)

        assert "playpause" in cbs
        assert "controls:menu" in cbs
        assert not any(
            c.startswith(("vol:", "hifi:", "display:", "airplay:", "ha:")) for c in cbs
        )
        assert all(len(row) > 0 for row in markup.inline_keyboard)

    def test_status_line_hides_disabled_sections(self, monkeypatch):
        panel.S.HIFI_STATUS_CACHE = "⚪ Hifi: Unknown"
        panel.S.AIRPLAY_STATUS_CACHE = "AirPlay: Unknown"
        panel.S.DENON_VOLUME_CACHE = "🔊 --"
        queue_state.REPEAT_MODE = "off"

        parts = self._status_parts(
            monkeypatch,
            panel_show_volume=False,
            panel_show_hifi=False,
            panel_show_airplay=False,
        )

        assert parts[0] == "🔁 Repeat: off"
        assert set(parts[1]) == {"─"}
        assert len(" | ".join(parts)) == panel.PANEL_STATUS_MIN_WIDTH

    def test_status_line_keeps_enabled_sections(self, monkeypatch):
        panel.S.HIFI_STATUS_CACHE = "🟢 Hifi: On"
        panel.S.AIRPLAY_STATUS_CACHE = "AirPlay: On"
        panel.S.DENON_VOLUME_CACHE = "🔊 42"
        queue_state.REPEAT_MODE = "one"

        parts = self._status_parts(monkeypatch, progress_text="00:01 / 00:02")

        assert parts == [
            "🟢 Hifi: On",
            "AirPlay: On",
            "🔁 Repeat: one",
            "🔊 42",
            "⏱ 00:01 / 00:02",
        ]

    def test_status_line_has_no_filler_when_all_flags_are_enabled(self, monkeypatch):
        panel.S.HIFI_STATUS_CACHE = "Hifi: On"
        panel.S.AIRPLAY_STATUS_CACHE = "AirPlay: On"
        panel.S.DENON_VOLUME_CACHE = "Vol: 1"
        queue_state.REPEAT_MODE = "off"

        parts = self._status_parts(monkeypatch)

        assert parts == ["Hifi: On", "AirPlay: On", "🔁 Repeat: off", "Vol: 1"]
        assert all("─" not in part for part in parts)

    def test_status_line_filler_shrinks_for_visible_sections(self, monkeypatch):
        panel.S.HIFI_STATUS_CACHE = "🟢 Hifi: On"
        panel.S.AIRPLAY_STATUS_CACHE = "AirPlay: On"
        queue_state.REPEAT_MODE = "off"

        parts = self._status_parts(
            monkeypatch,
            panel_show_volume=False,
            panel_show_hifi=True,
            panel_show_airplay=True,
        )

        assert parts[:-1] == ["🟢 Hifi: On", "AirPlay: On", "🔁 Repeat: off"]
        assert set(parts[-1]) == {"─"}
        assert len(" | ".join(parts)) == panel.PANEL_STATUS_MIN_WIDTH


class TestButtonReference:
    """The ❓ button shows a picture that hides itself again."""

    class _Photo:
        def __init__(self, file_id):
            self.file_id = file_id

    class _Msg:
        def __init__(self, message_id, file_id="cached-id"):
            self.message_id = message_id
            self.photo = [TestButtonReference._Photo(file_id)]

    class _Bot:
        def __init__(self, msg):
            self._msg = msg
            self.sent = []
            self.deleted = []

        async def send_photo(self, **kwargs):
            self.sent.append(kwargs)
            return self._msg

        async def delete_message(self, **kwargs):
            self.deleted.append(kwargs["message_id"])

    class _Ctx:
        def __init__(self, bot):
            self.bot = bot

    def _ctx(self, message_id=99):
        return self._Ctx(self._Bot(self._Msg(message_id)))

    def setup_method(self):
        panel.S.HELP_MSG_ID.clear()
        panel.S.HELP_PHOTO_FILE_ID = None
        # These tests drive the rate-limited wrappers; without this each call
        # would sleep for the real inter-request interval.
        self._intervals = (panel.S.TG_MIN_INTERVAL, panel.S.TG_DELETE_MIN_INTERVAL)
        panel.S.TG_MIN_INTERVAL = 0
        panel.S.TG_DELETE_MIN_INTERVAL = 0

    def teardown_method(self):
        panel.S.HELP_MSG_ID.clear()
        panel.S.HELP_PHOTO_FILE_ID = None
        panel.S.TG_MIN_INTERVAL, panel.S.TG_DELETE_MIN_INTERVAL = self._intervals

    def test_controls_panel_has_help_button_above_back(self):
        markup = panel.control_panel(mode="controls")

        assert markup.inline_keyboard[-2][0].text == "❓ Buttons"
        assert markup.inline_keyboard[-2][0].callback_data == "help:show"
        assert markup.inline_keyboard[-1][0].callback_data == "controls:back"

    def test_main_panel_has_no_help_button(self, monkeypatch):
        monkeypatch.setattr(panel.ha, "ha_available", lambda: False)

        markup = panel.control_panel(mode="main")

        cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert "help:show" not in cbs

    def test_hide_markup_carries_the_hide_callback(self):
        markup = panel.button_reference_markup()

        assert markup.inline_keyboard[0][0].text == "🙈 Ausblenden"
        assert markup.inline_keyboard[0][0].callback_data == "help:hide"

    def test_show_sends_photo_and_records_message_id(self):
        ctx = self._ctx(message_id=42)

        assert asyncio.run(panel.show_button_reference(ctx, 7)) is True
        assert panel.S.HELP_MSG_ID[7] == 42
        assert len(ctx.bot.sent) == 1
        assert ctx.bot.sent[0]["reply_markup"].inline_keyboard[0][0].callback_data == "help:hide"

    def test_first_send_caches_the_file_id(self):
        ctx = self._ctx()

        asyncio.run(panel.show_button_reference(ctx, 7))

        assert panel.S.HELP_PHOTO_FILE_ID == "cached-id"

    def test_second_send_reuses_the_file_id_instead_of_the_file(self):
        asyncio.run(panel.show_button_reference(self._ctx(), 7))
        ctx = self._ctx()

        asyncio.run(panel.show_button_reference(ctx, 7))

        assert ctx.bot.sent[0]["photo"] == "cached-id"

    def test_hide_deletes_the_message_and_clears_state(self):
        ctx = self._ctx(message_id=42)
        asyncio.run(panel.show_button_reference(ctx, 7))

        assert asyncio.run(panel.hide_button_reference(ctx, 7)) is True
        assert ctx.bot.deleted == [42]
        assert 7 not in panel.S.HELP_MSG_ID

    def test_hide_without_a_visible_image_is_a_no_op(self):
        ctx = self._ctx()

        assert asyncio.run(panel.hide_button_reference(ctx, 7)) is False
        assert ctx.bot.deleted == []

    def test_pressing_show_twice_replaces_instead_of_stacking(self):
        ctx = self._ctx(message_id=42)
        asyncio.run(panel.show_button_reference(ctx, 7))
        second = self._ctx(message_id=43)

        asyncio.run(panel.show_button_reference(second, 7))

        assert second.bot.deleted == [42], "the old image must be removed first"
        assert panel.S.HELP_MSG_ID[7] == 43

    def test_show_recovers_when_the_message_was_deleted_by_hand(self):
        """A manual delete must not lock the button out for good."""
        ctx = self._ctx(message_id=42)
        asyncio.run(panel.show_button_reference(ctx, 7))

        class _GoneBot(self._Bot):
            async def delete_message(self, **kwargs):
                raise RuntimeError("message to delete not found")

        recovered = self._Ctx(_GoneBot(self._Msg(44)))
        assert asyncio.run(panel.show_button_reference(recovered, 7)) is True
        assert panel.S.HELP_MSG_ID[7] == 44

    def test_show_reports_failure_when_the_image_is_missing(self, monkeypatch):
        monkeypatch.setattr(panel, "BUTTON_REFERENCE_PATH", "/nope/missing.png")
        ctx = self._ctx()

        assert asyncio.run(panel.show_button_reference(ctx, 7)) is False
        assert 7 not in panel.S.HELP_MSG_ID

    def test_shipped_reference_image_exists(self):
        assert os.path.isfile(panel.BUTTON_REFERENCE_PATH)
