"""Tests for handle_text logic in ui_text.py."""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from kodibot.telegram import ui as UI
from kodibot.telegram import ui_text

@pytest.fixture
def mock_ui(monkeypatch):
    mock = MagicMock()
    mock.queue_state = MagicMock()
    mock.playlist_store = MagicMock()
    mock.kodi_api = MagicMock()
    mock.ha = MagicMock()
    mock.CFG = MagicMock()
    
    # Mock some UI methods
    mock.delete_message_if_present = AsyncMock()
    mock.send_toast_message = AsyncMock()
    mock.request_delete_confirmation = AsyncMock()
    mock.update_list_message = AsyncMock()
    mock.update_now_playing_message = AsyncMock()
    mock.telegram_request_delete = AsyncMock()
    
    monkeypatch.setattr(ui_text, "UI", mock)
    return mock

@pytest.mark.asyncio
async def test_handle_text_delete_index_digit_skips_confirmation(mock_ui, monkeypatch):
    # Mock update and ctx
    update = MagicMock()
    update.message.text = "3"
    update.message.message_id = 111
    update.effective_chat.id = 123
    update.effective_user.id = 789
    
    ctx = MagicMock()
    ctx.user_data = {
        "await_delete_index": True,
        "await_delete_msg_id": 444
    }
    
    mock_ui.queue_delete_confirmation_payload.return_value = ({"kind": "queue_index", "index": 2, "title": "Track 3"}, None)
    
    # Mock _execute_pending_delete which is imported locally in handle_text
    fake_execute = AsyncMock(return_value=("🗑 Track deleted.", False))
    
    # We need to patch where it's imported in ui_text
    with patch("kodibot.telegram.ui_callbacks._execute_pending_delete", fake_execute):
        await ui_text.handle_text(update, ctx)
    
    fake_execute.assert_called_once()
    mock_ui.send_toast_message.assert_called_with(ctx, 123, "🗑 Track deleted.")
    # Verify request_delete_confirmation was NOT called
    assert not mock_ui.request_delete_confirmation.called
    assert ctx.user_data["await_delete_index"] is False

@pytest.mark.asyncio
async def test_handle_text_playlist_delete_index_still_requests_confirmation(mock_ui):
    update = MagicMock()
    update.message.text = "1"
    update.effective_chat.id = 123
    
    ctx = MagicMock()
    ctx.user_data = {
        "await_playlist_delete_index": True,
        "playlist_delete_files": ["fav.m3u"],
        "await_playlist_delete_msg_id": 555
    }
    
    await ui_text.handle_text(update, ctx)
    
    mock_ui.request_delete_confirmation.assert_called_once()
    assert "fav.m3u" in mock_ui.request_delete_confirmation.call_args[0][4]["filename"]


@pytest.mark.asyncio
async def test_handle_text_youtube_playlist_and_video_skips_cleanup(mock_ui):
    update = MagicMock()
    update.message.text = "https://www.youtube.com/watch?v=ABC123abc45&list=PL12345"
    update.message.message_id = 999
    update.effective_chat.id = 123
    update.effective_user.id = 789

    ctx = MagicMock()
    ctx.user_data = {}

    mock_vid = MagicMock()
    mock_vid.group.return_value = "ABC123abc45"
    mock_pl = MagicMock()
    mock_pl.group.return_value = "PL12345"

    mock_ui.kodi_api.YT.search.return_value = mock_vid
    mock_ui.kodi_api.PL.search.return_value = mock_pl
    mock_ui.kodi_api.SC_SET.search.return_value = None
    mock_ui.kodi_api.SC.search.return_value = None
    mock_ui.kodi_api.SC_SHORT.search.return_value = None

    mock_ui.send_and_track = AsyncMock()
    dummy_msg = MagicMock()
    dummy_msg.message_id = 1001
    mock_ui.send_and_track.return_value = dummy_msg

    await ui_text.handle_text(update, ctx)

    mock_ui.send_and_track.assert_called_once()
    mock_ui.activate_pending_choice.assert_called_once_with(
        ctx, 123, 789, 1001, "ABC123abc45", "PL12345"
    )

    assert not mock_ui.schedule_cleanup.called

