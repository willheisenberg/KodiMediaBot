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


# TEMPORARY with the playback action diagnostics in ui.schedule_playback_action:
# remove the queued/start/done tests together with those log lines.
class TestPlaybackAction:
    """A playback action stuck on the lock must show up in the log."""

    def test_logs_queue_start_and_end(self, monkeypatch, caplog):
        monkeypatch.setattr(ui, "PLAYBACK_TASK_LOCK", asyncio.Lock())
        ran = []

        def reopen():
            ran.append(True)

        async def run():
            ui.schedule_playback_action(None, 1234, reopen)
            await asyncio.sleep(0.05)

        with caplog.at_level("INFO", logger=ui.log.name):
            asyncio.run(run())
        assert ran == [True]
        assert "PLAYBACK ACTION queued action=reopen busy=False" in caplog.text
        assert "PLAYBACK ACTION start action=reopen" in caplog.text
        assert "PLAYBACK ACTION done action=reopen" in caplog.text

    def test_logs_when_an_earlier_action_still_holds_the_lock(self, monkeypatch, caplog):
        lock = asyncio.Lock()
        monkeypatch.setattr(ui, "PLAYBACK_TASK_LOCK", lock)

        async def run():
            await lock.acquire()
            ui.schedule_playback_action(None, 1234, lambda: None)
            await asyncio.sleep(0.05)
            assert "PLAYBACK ACTION start" not in caplog.text
            lock.release()
            await asyncio.sleep(0.05)

        with caplog.at_level("INFO", logger=ui.log.name):
            asyncio.run(run())
        assert "busy=True" in caplog.text
        assert "PLAYBACK ACTION start" in caplog.text

    def test_logs_a_failing_action(self, monkeypatch, caplog):
        monkeypatch.setattr(ui, "PLAYBACK_TASK_LOCK", asyncio.Lock())

        def skip_queue():
            raise RuntimeError("kodi gone")

        async def run():
            ui.schedule_playback_action(None, 1234, skip_queue)
            await asyncio.sleep(0.05)

        with caplog.at_level("INFO", logger=ui.log.name):
            asyncio.run(run())
        assert "PLAYBACK ACTION ERROR action=skip_queue err=kodi gone" in caplog.text
        assert "PLAYBACK ACTION done action=skip_queue" in caplog.text
