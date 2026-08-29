"""Tests for the per-chat message cleanup queue."""

import asyncio
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "test:token")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from telegram.error import BadRequest

from kodibot.telegram import state as S
from kodibot.telegram import ui

CHAT = 4242


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    for store in (
        S.LAST_BOT_ID, S.PREV_BOT_ID, S.LAST_SEEN_ID, S.LAST_CLEANUP_ID,
        S.FIRST_BOT_ID, S.LIST_MSG_ID, S.PANEL_MSG_ID, S.HELP_MSG_ID,
        S.CLEANUP_TASKS, S.CLEANUP_PENDING, S.CLEANUP_DEFERRED, S.CLEANUP_FAILED,
    ):
        store.clear()
    monkeypatch.setattr(S, "MAIN_LOOP", None)
    monkeypatch.setattr(S, "CLEANUP_DELAY_SECONDS", 0)
    monkeypatch.setattr(ui, "save_ui_state", lambda: None)

    async def passthrough_delete(call, *args, **kwargs):
        return await call(*args, **kwargs)

    monkeypatch.setattr(ui, "telegram_request_delete", passthrough_delete)
    yield
    for store in (S.CLEANUP_TASKS, S.CLEANUP_PENDING, S.CLEANUP_DEFERRED, S.CLEANUP_FAILED):
        store.clear()


def make_ctx(recorded, missing=()):
    async def delete_message(chat_id, message_id):
        recorded.append(message_id)
        if message_id in missing:
            raise BadRequest("Message to delete not found")
        return True

    return SimpleNamespace(bot=SimpleNamespace(delete_message=delete_message))


async def drain(chat_id=CHAT):
    """Await the running cleanup worker, if any."""
    for _ in range(50):
        task = S.CLEANUP_TASKS.get(chat_id)
        if task is None:
            await asyncio.sleep(0)
            if S.CLEANUP_TASKS.get(chat_id) is None:
                return
            continue
        try:
            await task
        except asyncio.CancelledError:
            return


class TestCleanupQueue:
    def test_deletes_range_between_bot_messages(self):
        recorded = []
        ctx = make_ctx(recorded)

        async def run():
            S.PREV_BOT_ID[CHAT] = 100
            S.LAST_BOT_ID[CHAT] = 104
            ui.schedule_cleanup(ctx, CHAT, 100)
            await drain()

        asyncio.run(run())
        assert recorded == [101, 102, 103, 104]
        assert S.LAST_CLEANUP_ID[CHAT] == 104

    def test_second_pass_does_not_redo_finished_range(self):
        recorded = []
        ctx = make_ctx(recorded)

        async def run():
            S.PREV_BOT_ID[CHAT] = 100
            S.LAST_BOT_ID[CHAT] = 103
            ui.schedule_cleanup(ctx, CHAT, 100)
            await drain()
            recorded.clear()
            # Same old prev_id, but the bot has posted again since.
            S.LAST_BOT_ID[CHAT] = 106
            ui.schedule_cleanup(ctx, CHAT, 100)
            await drain()

        asyncio.run(run())
        assert recorded == [104, 105, 106]

    def test_concurrent_schedules_share_one_worker(self):
        recorded = []
        ctx = make_ctx(recorded)

        async def run():
            S.PREV_BOT_ID[CHAT] = 200
            S.LAST_BOT_ID[CHAT] = 203
            ui.schedule_cleanup(ctx, CHAT, 200)
            worker = S.CLEANUP_TASKS[CHAT]
            S.LAST_BOT_ID[CHAT] = 205
            ui.schedule_cleanup(ctx, CHAT, 200)
            # No second task was spawned; the queue was extended instead.
            assert S.CLEANUP_TASKS[CHAT] is worker
            await drain()

        asyncio.run(run())
        assert recorded == [201, 202, 203, 204, 205]

    def test_missing_message_is_not_retried(self):
        recorded = []
        ctx = make_ctx(recorded, missing={102})

        async def run():
            S.PREV_BOT_ID[CHAT] = 100
            S.LAST_BOT_ID[CHAT] = 103
            ui.schedule_cleanup(ctx, CHAT, 100)
            await drain()
            recorded.clear()
            # Force a pass that would otherwise cover 102 again.
            S.LAST_CLEANUP_ID[CHAT] = 100
            S.LAST_BOT_ID[CHAT] = 103
            ui.schedule_cleanup(ctx, CHAT, 100)
            await drain()

        asyncio.run(run())
        assert 102 in S.CLEANUP_FAILED[CHAT]
        assert recorded == [101, 103]

    def test_panel_message_is_deferred_then_deleted_after_replacement(self):
        recorded = []
        ctx = make_ctx(recorded)

        async def run():
            S.PANEL_MSG_ID[CHAT] = 102
            S.PREV_BOT_ID[CHAT] = 100
            S.LAST_BOT_ID[CHAT] = 103
            ui.schedule_cleanup(ctx, CHAT, 100)
            await drain()
            assert recorded == [101, 103]
            assert S.CLEANUP_DEFERRED[CHAT] == {102}
            # Cleanup must not claim to be done past the deferred panel.
            assert S.LAST_CLEANUP_ID[CHAT] == 101

            # Panel gets recreated; the old one is now fair game.
            S.PANEL_MSG_ID[CHAT] = 110
            S.PREV_BOT_ID[CHAT] = 108
            S.LAST_BOT_ID[CHAT] = 110
            recorded.clear()
            ui.schedule_cleanup(ctx, CHAT, 108)
            await drain()

        asyncio.run(run())
        # Old panel finally removed; the live one (110) is now the deferred id.
        assert recorded == [102, 109]
        assert S.CLEANUP_DEFERRED[CHAT] == {110}
        assert S.LAST_CLEANUP_ID[CHAT] == 109


class TestButtonReferenceIsProtected:
    """The shown button reference must survive the cleanup sweep."""

    def test_visible_image_is_protected(self):
        S.HELP_MSG_ID[CHAT] = 105

        assert 105 in ui._protected_message_ids(CHAT)

    def test_hidden_image_is_not_protected(self):
        assert 105 not in ui._protected_message_ids(CHAT)

    def test_cleanup_defers_the_image_instead_of_deleting_it(self):
        recorded = []
        ctx = make_ctx(recorded)
        S.FIRST_BOT_ID[CHAT] = 100
        S.LAST_BOT_ID[CHAT] = 104
        S.PREV_BOT_ID[CHAT] = 100
        S.HELP_MSG_ID[CHAT] = 102

        async def run():
            ui.schedule_cleanup(ctx, CHAT, None)
            await drain()

        asyncio.run(run())

        assert 102 not in recorded, "the visible button reference was deleted"
        assert 102 in S.CLEANUP_DEFERRED.get(CHAT, set())
