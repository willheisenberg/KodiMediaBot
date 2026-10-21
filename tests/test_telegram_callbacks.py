"""Tests for deletion logic in ui_callbacks.py."""
import asyncio
import os
import sys

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "test:token")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from kodibot.telegram import ui as UI
from kodibot.telegram import ui_callbacks

@pytest.fixture
def mock_ui(monkeypatch):
    mock = MagicMock()
    mock.queue_state = MagicMock()
    mock.playlist_store = MagicMock()
    mock.kodi_api = MagicMock()
    mock.ha = MagicMock()
    mock.CFG = MagicMock()
    mock.CFG.playlist_dir = "/tmp/playlists"
    mock.HA_MENU_MSG_ID = {}
    
    # Mock some UI methods used in callbacks
    mock.delete_message_if_present = AsyncMock()
    mock.show_ha_preset_menu = AsyncMock()
    mock.send_toast_message = AsyncMock()
    mock.update_list_message = AsyncMock()
    mock.update_now_playing_message = AsyncMock()
    mock.request_delete_confirmation = AsyncMock()
    mock.telegram_request_delete = AsyncMock()

    async def run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(ui_callbacks.asyncio, "to_thread", run_inline)
    
    monkeypatch.setattr(ui_callbacks, "UI", mock)
    return mock

@pytest.mark.asyncio
async def test_execute_pending_delete_queue_all(mock_ui):
    pending = {"kind": "queue_all"}
    msg, skip = await ui_callbacks._execute_pending_delete(None, 123, pending)
    
    assert msg == "🗑 Queue cleared"
    assert skip is False
    mock_ui.queue_state.clear_queue.assert_called_once()

@pytest.mark.asyncio
async def test_execute_pending_delete_queue_index(mock_ui):
    mock_ui.queue_state.delete_index.return_value = (True, "Deleted")
    mock_ui.queue_delete_target_matches.return_value = True
    
    pending = {
        "kind": "queue_index",
        "index": 0,
        "identity": {"title": "Test"},
        "success_text": "Custom success"
    }
    msg, skip = await ui_callbacks._execute_pending_delete(None, 123, pending)
    
    assert msg == "Custom success"
    assert skip is False
    mock_ui.queue_state.delete_index.assert_called_with(0)

@pytest.mark.asyncio
async def test_execute_pending_delete_playlist_file(mock_ui):
    mock_ui.playlist_store.delete_playlist_from_disk.return_value = (True, "file.m3u")
    
    pending = {"kind": "playlist_file", "filename": "file.m3u"}
    msg, skip = await ui_callbacks._execute_pending_delete(None, 123, pending)
    
    assert "🗑 Deleted: file.m3u" in msg
    assert skip is True
    mock_ui.playlist_store.delete_playlist_from_disk.assert_called_once()

@pytest.mark.asyncio
async def test_execute_pending_delete_favourite(mock_ui):
    mock_ui.kodi_api.remove_favourite.return_value = True
    
    pending = {"kind": "favourite", "title": "My Fav"}
    msg, skip = await ui_callbacks._execute_pending_delete(None, 123, pending)
    
    assert "🗑 Deleted favourite: My Fav" in msg
    assert skip is True
    mock_ui.kodi_api.remove_favourite.assert_called_with("My Fav")

@pytest.mark.asyncio
async def test_execute_pending_delete_ha_color(mock_ui):
    mock_ui.ha.delete_saved_color.return_value = True
    mock_ui.HA_MENU_MSG_ID[123] = 456
    
    pending = {"kind": "ha_color", "name": "Red", "label": "Bright Red"}
    msg, skip = await ui_callbacks._execute_pending_delete(None, 123, pending)
    
    assert "🗑 Color deleted: Bright Red" in msg
    assert skip is True
    mock_ui.ha.delete_saved_color.assert_called_with("Red")
    mock_ui.show_ha_preset_menu.assert_called_once()

@pytest.mark.asyncio
async def test_on_button_delete_first_skips_confirmation(mock_ui, monkeypatch):
    # Mock update and ctx
    update = MagicMock()
    update.callback_query.data = "delete:first"
    update.callback_query.answer = AsyncMock()
    update.effective_chat.id = 123
    update.effective_user.id = 789
    
    ctx = MagicMock()
    
    mock_ui.queue_delete_confirmation_payload.return_value = ({"kind": "queue_index", "index": 0}, None)
    
    # We want to check that _execute_pending_delete was called
    fake_execute = AsyncMock(return_value=("Success", False))
    monkeypatch.setattr(ui_callbacks, "_execute_pending_delete", fake_execute)
    
    await ui_callbacks.on_button(update, ctx)
    
    fake_execute.assert_called_once()
    update.callback_query.answer.assert_any_call(text="Success")
    # Verify request_delete_confirmation was NOT called
    assert not mock_ui.request_delete_confirmation.called

@pytest.mark.asyncio
async def test_on_button_delete_last_skips_confirmation(mock_ui, monkeypatch):
    update = MagicMock()
    update.callback_query.data = "delete:last"
    update.callback_query.answer = AsyncMock()
    update.effective_chat.id = 123
    
    ctx = MagicMock()
    mock_ui.queue_state.QUEUE = [{}, {}]
    mock_ui.queue_delete_confirmation_payload.return_value = ({"kind": "queue_index", "index": 1}, None)
    
    fake_execute = AsyncMock(return_value=("Success", False))
    monkeypatch.setattr(ui_callbacks, "_execute_pending_delete", fake_execute)
    
    await ui_callbacks.on_button(update, ctx)
    
    fake_execute.assert_called_once()
    assert fake_execute.call_args[0][2]["index"] == 1


@pytest.mark.asyncio
async def test_on_button_help_show_displays_the_reference(mock_ui):
    update = MagicMock()
    update.callback_query.data = "help:show"
    update.callback_query.answer = AsyncMock()
    update.effective_chat.id = 123
    update.effective_user.id = 789
    mock_ui.show_button_reference = AsyncMock(return_value=True)

    await ui_callbacks.on_button(update, MagicMock())

    mock_ui.show_button_reference.assert_called_once()
    assert mock_ui.show_button_reference.call_args[0][1] == 123
    update.callback_query.answer.assert_called_with()


@pytest.mark.asyncio
async def test_on_button_help_show_warns_when_image_is_unavailable(mock_ui):
    update = MagicMock()
    update.callback_query.data = "help:show"
    update.callback_query.answer = AsyncMock()
    update.effective_chat.id = 123
    update.effective_user.id = 789
    mock_ui.show_button_reference = AsyncMock(return_value=False)

    await ui_callbacks.on_button(update, MagicMock())

    update.callback_query.answer.assert_called_with(text="⚠ Button reference unavailable")


@pytest.mark.asyncio
async def test_on_button_help_hide_removes_the_reference(mock_ui):
    update = MagicMock()
    update.callback_query.data = "help:hide"
    update.callback_query.answer = AsyncMock()
    update.effective_chat.id = 123
    update.effective_user.id = 789
    mock_ui.hide_button_reference = AsyncMock(return_value=True)

    await ui_callbacks.on_button(update, MagicMock())

    mock_ui.hide_button_reference.assert_called_once()
    assert mock_ui.hide_button_reference.call_args[0][1] == 123


def _episode_playback(mock_ui, goto_status, queue=()):
    """Kodi plays its own playlist; the bot queue is not driving playback."""
    mock_ui.queue_state.QUEUE = list(queue)
    mock_ui.queue_state.DISPLAY_INDEX = None
    mock_ui.queue_state.EXTERNAL_PLAYBACK = True
    mock_ui.kodi_api.kodi_playlist_goto.return_value = goto_status
    update = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.effective_chat.id = 123
    update.effective_user.id = 789
    return update


@pytest.mark.asyncio
async def test_on_button_skip_steps_kodi_playlist(mock_ui):
    update = _episode_playback(mock_ui, "moved")
    update.callback_query.data = "skip"

    await ui_callbacks.on_button(update, MagicMock())

    mock_ui.kodi_api.kodi_playlist_goto.assert_called_once_with("next")
    update.callback_query.answer.assert_any_call(text="⏭ Next")
    assert not mock_ui.schedule_playback_action.called


@pytest.mark.asyncio
async def test_on_button_back_steps_kodi_playlist(mock_ui):
    update = _episode_playback(mock_ui, "moved")
    update.callback_query.data = "back"

    await ui_callbacks.on_button(update, MagicMock())

    mock_ui.kodi_api.kodi_playlist_goto.assert_called_once_with("previous")
    update.callback_query.answer.assert_any_call(text="⏮ Previous")
    assert not mock_ui.schedule_playback_action.called


@pytest.mark.asyncio
async def test_on_button_skip_keeps_playlist_at_its_end(mock_ui):
    # The queue holds tracks, but the last episode must not fall through to it.
    update = _episode_playback(mock_ui, "end", queue=[{}, {}])
    update.callback_query.data = "skip"

    await ui_callbacks.on_button(update, MagicMock())

    update.callback_query.answer.assert_any_call(text="⏹ End of the Kodi playlist.")
    assert not mock_ui.schedule_playback_action.called


@pytest.mark.asyncio
async def test_on_button_skip_uses_queue_without_kodi_playlist(mock_ui):
    update = _episode_playback(mock_ui, "inactive", queue=[{}, {}])
    update.callback_query.data = "skip"

    await ui_callbacks.on_button(update, MagicMock())

    update.callback_query.answer.assert_any_call(text="⏭ Next")
    assert mock_ui.schedule_playback_action.call_args[0][2] is mock_ui.queue_state.skip_queue


@pytest.mark.asyncio
async def test_on_button_skip_reports_empty_queue_without_kodi_playlist(mock_ui):
    update = _episode_playback(mock_ui, "inactive")
    update.callback_query.data = "skip"

    await ui_callbacks.on_button(update, MagicMock())

    update.callback_query.answer.assert_any_call(text="⏹ End of queue.")
    assert not mock_ui.schedule_playback_action.called


@pytest.mark.asyncio
async def test_on_button_skip_leaves_the_queue_alone_while_it_plays(mock_ui):
    update = _episode_playback(mock_ui, "moved", queue=[{}, {}])
    update.callback_query.data = "skip"
    mock_ui.queue_state.DISPLAY_INDEX = 0
    mock_ui.queue_state.EXTERNAL_PLAYBACK = False

    await ui_callbacks.on_button(update, MagicMock())

    assert not mock_ui.kodi_api.kodi_playlist_goto.called
    assert mock_ui.schedule_playback_action.call_args[0][2] is mock_ui.queue_state.skip_queue


def _seek_update(data):
    update = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.message_id = 55
    update.effective_chat.id = 123
    update.effective_user.id = 789
    return update


@pytest.mark.asyncio
async def test_seek_percent_button_reopens_prompt_while_one_is_pending(mock_ui):
    """A stale prompt must not make the % button dead until it times out."""
    mock_ui.close_prompt = AsyncMock()
    mock_ui.send_button_selection = AsyncMock(return_value=77)
    ctx = MagicMock()
    ctx.user_data = {"await_seek_percent": True, "await_seek_percent_msg_id": 55}

    await ui_callbacks.on_button(_seek_update("seek:percent"), ctx)

    mock_ui.close_prompt.assert_awaited_once_with(ctx, 123, 789, "await_seek_percent")
    mock_ui.send_button_selection.assert_awaited_once()
    mock_ui.activate_prompt.assert_called_once_with(
        ctx, 123, 789, "await_seek_percent", "await_seek_percent_msg_id", 77
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cmd, state_key, msg_key",
    [
        ("radio:ask", "await_radio_search", "await_radio_search_msg_id"),
        ("tv:ask", "await_tv_search", "await_tv_search_msg_id"),
        ("ha:sethex", "await_ha_hex", "await_ha_hex_msg_id"),
    ],
)
async def test_text_prompt_button_reopens_prompt_while_one_is_pending(
    mock_ui, cmd, state_key, msg_key
):
    mock_ui.close_prompt = AsyncMock()
    mock_ui.touch_ha_menu_timeout = MagicMock()
    mock_ui.send_and_track = AsyncMock(return_value=MagicMock(message_id=77))
    ctx = MagicMock()
    ctx.user_data = {state_key: True, msg_key: 55}

    await ui_callbacks.on_button(_seek_update(cmd), ctx)

    mock_ui.close_prompt.assert_awaited_once_with(ctx, 123, 789, state_key)
    mock_ui.send_and_track.assert_awaited_once()
    assert mock_ui.activate_prompt.call_args[0][3:6] == (state_key, msg_key, 77)


def test_no_prompt_button_ignores_a_repeated_press():
    """Every prompt button closes its open prompt instead of returning early."""
    import inspect
    import re

    source = inspect.getsource(ui_callbacks.on_button)

    guards = re.findall(
        r'if (?:ctx\.user_data\.get\("await_\w+"\)|UI\.\w+_prompt_active\(ctx\.user_data\)):'
        r"\s+await q\.answer\(\)\s+return",
        source,
    )
    assert guards == []
    assert source.count("await UI.close_prompt(") == 17


@pytest.mark.asyncio
async def test_failed_seek_to_closes_the_prompt(mock_ui):
    mock_ui.queue_state.seek_percent.return_value = False
    ctx = MagicMock()
    ctx.user_data = {"await_seek_percent": True, "await_seek_percent_msg_id": 55}
    update = _seek_update("seek_to:50")

    await ui_callbacks.on_button(update, ctx)

    update.callback_query.answer.assert_any_call(text=ui_callbacks.t("seek_failed"))
    mock_ui.delete_message_if_present.assert_awaited_once_with(ctx, 123, 55)
    mock_ui.cancel_prompt_timeout.assert_called_once_with(123, 789, "await_seek_percent")
    assert not ctx.user_data.get("await_seek_percent")
    assert "await_seek_percent_msg_id" not in ctx.user_data


@pytest.mark.asyncio
async def test_successful_seek_to_closes_the_prompt(mock_ui):
    mock_ui.queue_state.seek_percent.return_value = True
    ctx = MagicMock()
    ctx.user_data = {"await_seek_percent": True, "await_seek_percent_msg_id": 55}
    update = _seek_update("seek_to:50")

    await ui_callbacks.on_button(update, ctx)

    update.callback_query.answer.assert_any_call(text=ui_callbacks.t("seeked_to", pct=50))
    mock_ui.queue_state.seek_percent.assert_called_once_with(50)
    assert not ctx.user_data.get("await_seek_percent")

