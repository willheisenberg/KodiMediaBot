import os
import sys
os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "test:token")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import Mock, AsyncMock
import asyncio

from kodibot.core import queue_state


class TestRadioReconnectState:
    def setup_method(self):
        # Reset state
        queue_state.LAST_PLAYED_RADIO = None
        queue_state.EXPECTED_STOP = False
        queue_state.ON_UNEXPECTED_RADIO_STOP = None
        queue_state.CANCEL_RECONNECT_CB = None

    def test_set_last_played_radio(self):
        queue_state.set_last_played_radio("http://radio.stream", "Awesome Radio")
        assert queue_state.LAST_PLAYED_RADIO == {"url": "http://radio.stream", "title": "Awesome Radio"}
        assert queue_state.EXPECTED_STOP is False

    def test_clear_radio_reconnect_state(self):
        queue_state.set_last_played_radio("http://radio.stream", "Awesome Radio")
        queue_state.clear_radio_reconnect_state()
        assert queue_state.LAST_PLAYED_RADIO is None
        assert queue_state.EXPECTED_STOP is True

    def test_play_item_resets_state_and_cancels(self, monkeypatch):
        monkeypatch.setattr(queue_state.media, "cleanup_active_image_session", lambda: None)
        monkeypatch.setattr(queue_state.kodi_api, "stop_all_players", lambda: None)
        monkeypatch.setattr(queue_state.kodi_api, "kodi_clear_all_playlists", lambda: None)
        monkeypatch.setattr(queue_state.kodi_api, "kodi_call", lambda m, p=None: {})
        monkeypatch.setattr(queue_state, "set_expecting_ws", lambda n: None)
        monkeypatch.setattr(queue_state, "schedule_playback_refresh", lambda: None)

        cancel_called = False
        def fake_cancel():
            nonlocal cancel_called
            cancel_called = True

        queue_state.CANCEL_RECONNECT_CB = fake_cancel
        queue_state.set_last_played_radio("http://radio.stream", "Awesome Radio")

        item = queue_state.make_item("Test Track", "http://test", "audio")
        queue_state.play_item(item)

        assert queue_state.LAST_PLAYED_RADIO is None
        assert queue_state.EXPECTED_STOP is True
        assert cancel_called is True

    def test_hard_stop_and_clear_resets_state_and_cancels(self, monkeypatch):
        monkeypatch.setattr(queue_state.media, "cleanup_active_image_session", lambda: None)
        monkeypatch.setattr(queue_state.kodi_api, "stop_all_players", lambda: None)
        monkeypatch.setattr(queue_state.kodi_api, "kodi_clear_all_playlists", lambda: None)
        monkeypatch.setattr(queue_state, "schedule_playback_refresh", lambda: None)

        cancel_called = False
        def fake_cancel():
            nonlocal cancel_called
            cancel_called = True

        queue_state.CANCEL_RECONNECT_CB = fake_cancel
        queue_state.set_last_played_radio("http://radio.stream", "Awesome Radio")

        queue_state.hard_stop_and_clear()

        assert queue_state.LAST_PLAYED_RADIO is None
        assert queue_state.EXPECTED_STOP is True
        assert cancel_called is True

    @pytest.mark.asyncio
    async def test_handle_ws_stop_unexpected(self):
        triggered_url = None
        triggered_title = None

        async def fake_on_stop(url, title):
            nonlocal triggered_url, triggered_title
            triggered_url = url
            triggered_title = title

        queue_state.set_last_played_radio("http://radio.stream", "Awesome Radio")
        queue_state.set_ui_callbacks(lambda: None, on_unexpected_radio_stop=fake_on_stop)

        # Trigger ws stop handler
        queue_state._handle_ws_stop()

        # Let the asyncio event loop run to execute task
        await asyncio.sleep(0.05)

        assert triggered_url == "http://radio.stream"
        assert triggered_title == "Awesome Radio"
        assert queue_state.EXPECTED_STOP is True

    @pytest.mark.asyncio
    async def test_handle_ws_stop_expected_does_not_trigger(self):
        triggered = False

        async def fake_on_stop(url, title):
            nonlocal triggered
            triggered = True

        queue_state.set_last_played_radio("http://radio.stream", "Awesome Radio")
        queue_state.EXPECTED_STOP = True
        queue_state.set_ui_callbacks(lambda: None, on_unexpected_radio_stop=fake_on_stop)

        queue_state._handle_ws_stop()
        await asyncio.sleep(0.05)

        assert not triggered
