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
        S.PROMPT_TIMEOUT_TASKS, S.PROMPT_REGISTRY,
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


USER = 77


def make_prompt_ctx(recorded):
    """Context whose application runs prompt timeout tasks on the test loop."""
    ctx = make_ctx(recorded)
    ctx.user_data = {}
    ctx.application = SimpleNamespace(
        create_task=asyncio.get_running_loop().create_task,
        user_data={USER: ctx.user_data},
    )
    return ctx


async def cancel_prompt_tasks():
    tasks = list(S.PROMPT_TIMEOUT_TASKS.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


class TestOpenPromptIsProtected:
    """A prompt deleted under its state flag leaves its button dead."""

    @pytest.mark.asyncio
    async def test_open_prompt_survives_cleanup_until_it_is_answered(self):
        recorded = []
        ctx = make_prompt_ctx(recorded)
        S.LAST_BOT_ID[CHAT] = 106
        S.PREV_BOT_ID[CHAT] = 103
        ui.activate_prompt(ctx, CHAT, USER, "await_seek_percent", "await_seek_percent_msg_id", 105)

        ui.schedule_cleanup(ctx, CHAT, None)
        await drain()

        assert 105 not in recorded
        assert 105 in S.CLEANUP_DEFERRED[CHAT]

        # Answering clears the flag; the next sweep collects the message.
        ctx.user_data["await_seek_percent"] = False
        ui.schedule_cleanup(ctx, CHAT, None)
        await drain()

        assert 105 in recorded
        await cancel_prompt_tasks()

    @pytest.mark.asyncio
    async def test_replaced_prompt_message_is_not_protected(self):
        ctx = make_prompt_ctx([])
        ui.activate_prompt(ctx, CHAT, USER, "await_seek_percent", "await_seek_percent_msg_id", 105)
        ui.activate_prompt(ctx, CHAT, USER, "await_seek_percent", "await_seek_percent_msg_id", 108)
        await asyncio.sleep(0)

        protected = ui._protected_message_ids(CHAT)
        assert 108 in protected
        assert 105 not in protected
        # The replaced prompt's task must not unregister its successor
        assert (CHAT, USER, "await_seek_percent") in S.PROMPT_TIMEOUT_TASKS
        await cancel_prompt_tasks()

    @pytest.mark.asyncio
    async def test_prompt_in_another_chat_is_not_protected_here(self):
        ctx = make_prompt_ctx([])
        ui.activate_prompt(ctx, CHAT + 1, USER, "await_play_index", "await_play_msg_id", 105)

        assert 105 not in ui._protected_message_ids(CHAT)
        await cancel_prompt_tasks()

    @pytest.mark.asyncio
    async def test_expired_prompt_is_deleted_and_unregistered(self, monkeypatch):
        monkeypatch.setattr(ui, "PROMPT_TIMEOUT_SECONDS", 0)
        recorded = []
        ctx = make_prompt_ctx(recorded)
        ui.activate_prompt(ctx, CHAT, USER, "await_play_index", "await_play_msg_id", 105)

        await S.PROMPT_TIMEOUT_TASKS[(CHAT, USER, "await_play_index")]

        assert recorded == [105]
        assert ctx.user_data == {}
        assert S.PROMPT_REGISTRY == {}
        assert S.PROMPT_TIMEOUT_TASKS == {}


class TestClosePrompt:
    """Pressing a prompt's button again replaces the prompt."""

    @pytest.mark.asyncio
    async def test_closes_the_open_prompt(self):
        recorded = []
        ctx = make_prompt_ctx(recorded)
        ui.activate_prompt(
            ctx, CHAT, USER, "await_fav", "await_fav_msg_id", 105, extra_keys=("favourites",)
        )
        ctx.user_data["favourites"] = ["a"]
        task = S.PROMPT_TIMEOUT_TASKS[(CHAT, USER, "await_fav")]

        await ui.close_prompt(ctx, CHAT, USER, "await_fav")
        await asyncio.gather(task, return_exceptions=True)

        assert recorded == [105]
        assert ctx.user_data == {}
        assert task.cancelled()
        assert S.PROMPT_REGISTRY == {}
        assert 105 not in ui._protected_message_ids(CHAT)

    @pytest.mark.asyncio
    async def test_closes_only_the_active_step_of_a_multi_step_prompt(self):
        recorded = []
        ctx = make_prompt_ctx(recorded)
        ui.activate_prompt(ctx, CHAT, USER, "await_movie_index", "await_movie_msg_id", 105)

        await ui.close_prompt(ctx, CHAT, USER, *ui.MEDIA_PROMPT_KEYS)

        assert recorded == [105]
        assert not ui.media_prompt_active(ctx.user_data)
        await cancel_prompt_tasks()

    @pytest.mark.asyncio
    async def test_without_open_prompt_does_nothing(self):
        recorded = []
        ctx = make_prompt_ctx(recorded)

        await ui.close_prompt(ctx, CHAT, USER, "await_play_index")

        assert recorded == []
        assert ctx.user_data == {}

