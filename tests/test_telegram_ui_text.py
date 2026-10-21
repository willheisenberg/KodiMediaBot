"""Tests for handle_text logic in ui_text.py."""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from kodibot.telegram import ui as UI
from kodibot.telegram import ui_text
from kodibot.core import kodi_api, queue_state


@pytest.fixture
def batch_ui(mock_ui):
    mock_ui.kodi_api = kodi_api
    mock_ui.queue_state.is_sc_set_url = queue_state.is_sc_set_url
    mock_ui.queue_state.is_sc_track_url = queue_state.is_sc_track_url
    mock_ui.queue_state.queue_video_async = AsyncMock()
    mock_ui.queue_state.queue_playlist_async = AsyncMock(return_value=3)
    mock_ui.queue_state.queue_soundcloud_set_async = AsyncMock(return_value=2)
    mock_ui.queue_state.make_soundcloud.side_effect = lambda url: {"url": url}
    mock_ui.pending = {}
    return mock_ui


def test_extract_queue_links_preserves_order_without_duplicate_markdown_labels():
    first = "https://soundcloud.com/locoparaiso/heimlich-knueller-loco-paraiso"
    second = "https://soundcloud.com/mhan_solo/mhan-solo-auf-sendung"
    third = "https://youtu.be/SPM6lZ9Zo88"
    assert ui_text.extract_queue_links(f"[{first}]({first})\n{second} {third}") == [first, second, third]


@pytest.mark.asyncio
async def test_handle_text_queues_all_three_links_in_message_order(batch_ui):
    links = [
        "https://soundcloud.com/locoparaiso/heimlich-knueller-loco-paraiso",
        "https://soundcloud.com/mhan_solo/mhan-solo-auf-sendung",
        "https://youtu.be/SPM6lZ9Zo88",
    ]
    events = []
    batch_ui.queue_state.queue_item.side_effect = lambda item: events.append(item["url"])
    batch_ui.queue_state.queue_video_async.side_effect = lambda vid: events.append(vid)
    update = MagicMock()
    update.message.text = "\n".join(links)
    update.effective_chat.id = 123
    ctx = MagicMock()
    ctx.user_data = {}

    await ui_text.handle_text(update, ctx)

    assert events == [links[0], links[1], "SPM6lZ9Zo88"]
    batch_ui.update_list_message.assert_awaited_once_with(ctx, 123)
    batch_ui.schedule_cleanup.assert_called_once()


@pytest.mark.asyncio
async def test_batch_continues_after_failed_link_and_preserves_mixed_order(batch_ui):
    events = []
    batch_ui.queue_state.queue_video_async.side_effect = lambda vid: events.append(vid)
    batch_ui.queue_state.make_soundcloud.side_effect = ValueError("bad track")
    await ui_text.queue_multiple_links([
        "https://youtu.be/SPM6lZ9Zo88",
        "https://soundcloud.com/artist/broken",
        "https://youtu.be/ABC123abc45",
    ], MagicMock(), 123)
    assert events == ["SPM6lZ9Zo88", "ABC123abc45"]
    assert any(call.args[0] == "queue_link_failed" for call in batch_ui.t.call_args_list)


@pytest.mark.asyncio
async def test_batch_short_link_and_playlist_urls(batch_ui, monkeypatch):
    async def run_inline(func, *args):
        return func(*args)

    monkeypatch.setattr(ui_text.asyncio, "to_thread", run_inline)
    batch_ui.queue_state.resolve_sc_short.return_value = "https://soundcloud.com/artist/sets/album"
    await ui_text.queue_multiple_links([
        "https://on.soundcloud.com/abc123",
        "https://www.youtube.com/watch?v=SPM6lZ9Zo88&list=PL12345",
        "https://www.youtube.com/playlist?list=PL67890",
    ], MagicMock(), 123)
    batch_ui.queue_state.queue_soundcloud_set_async.assert_awaited_once_with("https://soundcloud.com/artist/sets/album")
    batch_ui.queue_state.queue_video_async.assert_awaited_once_with("SPM6lZ9Zo88")
    batch_ui.queue_state.queue_playlist_async.assert_awaited_once_with("PL67890")

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
    mock_ui.spotify.parse_spotify_url.return_value = None
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
