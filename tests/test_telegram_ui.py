"""Tests for Telegram UI refresh helpers."""

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

from kodibot.core import queue_state
from kodibot.telegram import state as S
from kodibot.telegram import ui


class TestPlaybackUiRefresh:
    def setup_method(self):
        self.old_app = S.APP_INSTANCE
        S.APP_INSTANCE = object()
        queue_state.LIST_DIRTY = False

    def teardown_method(self):
        S.APP_INSTANCE = self.old_app
        queue_state.LIST_DIRTY = False

    def test_refresh_updates_list_before_panel_when_queue_dirty(self, monkeypatch):
        calls = []

        async def fake_update_list_message(ctx, chat_id):
            calls.append(("list", ctx, chat_id))
            queue_state.LIST_DIRTY = False

        async def fake_update_now_playing_message(ctx, chat_id):
            calls.append(("panel", ctx, chat_id))

        monkeypatch.setattr(ui, "update_list_message", fake_update_list_message)
        monkeypatch.setattr(ui, "update_now_playing_message", fake_update_now_playing_message)

        queue_state.LIST_DIRTY = True
        asyncio.run(ui._refresh_playback_ui(1234))

        assert calls == [
            ("list", S.APP_INSTANCE, 1234),
            ("panel", S.APP_INSTANCE, 1234),
        ]

    def test_refresh_skips_list_when_queue_not_dirty(self, monkeypatch):
        calls = []

        async def fake_update_list_message(ctx, chat_id):
            calls.append(("list", ctx, chat_id))

        async def fake_update_now_playing_message(ctx, chat_id):
            calls.append(("panel", ctx, chat_id))

        monkeypatch.setattr(ui, "update_list_message", fake_update_list_message)
        monkeypatch.setattr(ui, "update_now_playing_message", fake_update_now_playing_message)

        asyncio.run(ui._refresh_playback_ui(1234))

        assert calls == [
            ("panel", S.APP_INSTANCE, 1234),
        ]
