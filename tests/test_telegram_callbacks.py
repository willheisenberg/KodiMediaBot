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
    # Verify it was run in a thread
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
