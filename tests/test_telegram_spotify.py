"""Dispatch of Spotify links in handle_text."""
import os
os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "1")

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import MagicMock, AsyncMock

from kodibot.core import spotify as real_spotify
from kodibot.telegram import ui as _UI  # noqa: F401  lädt ui_text ohne Zirkel
from kodibot.telegram import ui_text


@pytest.fixture
def mock_ui(monkeypatch):
    mock = MagicMock()
    mock.queue_state = MagicMock()
    mock.kodi_api = MagicMock()
    mock.media = MagicMock()

    # The real exception class, so `except UI.spotify.X` actually catches.
    mock.spotify = MagicMock()
    mock.spotify.SpotifyUnavailable = real_spotify.SpotifyUnavailable
    mock.spotify.EMBED_TRACK_LIMIT = real_spotify.EMBED_TRACK_LIMIT

    mock.delete_message_if_present = AsyncMock()
    mock.send_toast_message = AsyncMock()
    mock.send_and_track = AsyncMock()
    mock.update_list_message = AsyncMock()
    mock.update_now_playing_message = AsyncMock()
    mock.telegram_request_delete = AsyncMock()
    mock.warn_and_cleanup_chat = AsyncMock()
    mock.pending = {}

    monkeypatch.setattr(ui_text, "UI", mock)
    return mock


def spotify_update(link="https://open.spotify.com/playlist/PL1"):
    update = MagicMock()
    update.message.text = link
    update.message.message_id = 999
    update.effective_chat.id = 123
    update.effective_user.id = 789
    return update


@pytest.mark.asyncio
async def test_queues_spotify_playlist(mock_ui):
    ctx = MagicMock()
    ctx.user_data = {}
    tracks = [("Daft Punk", "One More Time"), ("Justice", "D.A.N.C.E."), ("Air", "Sexy Boy")]
    mock_ui.spotify.parse_spotify_url.return_value = ("playlist", "PL1")
    mock_ui.spotify.fetch_tracks.return_value = tracks
    mock_ui.queue_state.queue_spotify_async = AsyncMock(return_value=(2, 3))

    await ui_text.handle_text(spotify_update(), ctx)

    mock_ui.spotify.fetch_tracks.assert_called_once_with("playlist", "PL1")
    mock_ui.queue_state.queue_spotify_async.assert_awaited_once_with(tracks)
    mock_ui.t.assert_any_call("spotify_added", added=2, total=3)
    mock_ui.update_list_message.assert_awaited()


@pytest.mark.asyncio
async def test_announces_resolving_before_the_wait(mock_ui):
    ctx = MagicMock()
    ctx.user_data = {}
    mock_ui.spotify.parse_spotify_url.return_value = ("playlist", "PL1")
    mock_ui.spotify.fetch_tracks.return_value = [("A", "1")] * 87
    mock_ui.queue_state.queue_spotify_async = AsyncMock(return_value=(87, 87))

    await ui_text.handle_text(spotify_update(), ctx)

    mock_ui.t.assert_any_call("spotify_resolving", count=87)


@pytest.mark.asyncio
async def test_warns_when_the_embed_page_capped_the_playlist(mock_ui):
    """At exactly the embed limit the playlist was probably longer than we see."""
    ctx = MagicMock()
    ctx.user_data = {}
    tracks = [("A", str(i)) for i in range(real_spotify.EMBED_TRACK_LIMIT)]
    mock_ui.spotify.parse_spotify_url.return_value = ("playlist", "PL1")
    mock_ui.spotify.fetch_tracks.return_value = tracks
    mock_ui.queue_state.queue_spotify_async = AsyncMock(return_value=(95, 100))

    await ui_text.handle_text(spotify_update(), ctx)

    mock_ui.t.assert_any_call("spotify_added_capped", added=95, total=100)


@pytest.mark.asyncio
async def test_reports_plain_count_below_the_embed_limit(mock_ui):
    ctx = MagicMock()
    ctx.user_data = {}
    tracks = [("A", str(i)) for i in range(real_spotify.EMBED_TRACK_LIMIT - 1)]
    mock_ui.spotify.parse_spotify_url.return_value = ("playlist", "PL1")
    mock_ui.spotify.fetch_tracks.return_value = tracks
    mock_ui.queue_state.queue_spotify_async = AsyncMock(return_value=(99, 99))

    await ui_text.handle_text(spotify_update(), ctx)

    mock_ui.t.assert_any_call("spotify_added", added=99, total=99)


@pytest.mark.asyncio
async def test_reports_unavailable_playlist(mock_ui):
    ctx = MagicMock()
    ctx.user_data = {}
    mock_ui.spotify.parse_spotify_url.return_value = ("playlist", "PL1")
    mock_ui.spotify.fetch_tracks.side_effect = real_spotify.SpotifyUnavailable()
    mock_ui.queue_state.queue_spotify_async = AsyncMock()

    await ui_text.handle_text(spotify_update(), ctx)

    mock_ui.t.assert_any_call("spotify_unavailable")
    assert not mock_ui.queue_state.queue_spotify_async.called


@pytest.mark.asyncio
async def test_reports_playlist_without_usable_tracks(mock_ui):
    ctx = MagicMock()
    ctx.user_data = {}
    mock_ui.spotify.parse_spotify_url.return_value = ("playlist", "PL1")
    mock_ui.spotify.fetch_tracks.return_value = []
    mock_ui.queue_state.queue_spotify_async = AsyncMock()

    await ui_text.handle_text(spotify_update(), ctx)

    mock_ui.t.assert_any_call("spotify_unavailable")
    assert not mock_ui.queue_state.queue_spotify_async.called


@pytest.mark.asyncio
async def test_spotify_link_never_reaches_social_video_download(mock_ui):
    ctx = MagicMock()
    ctx.user_data = {}
    mock_ui.spotify.parse_spotify_url.return_value = ("track", "TR1")
    mock_ui.spotify.fetch_tracks.return_value = [("Daft Punk", "Veridis Quo")]
    mock_ui.queue_state.queue_spotify_async = AsyncMock(return_value=(1, 1))
    mock_ui.media.download_social_video_item = AsyncMock()

    await ui_text.handle_text(spotify_update("https://open.spotify.com/track/TR1"), ctx)

    assert not mock_ui.media.download_social_video_item.called
    assert not mock_ui.warn_and_cleanup_chat.called


@pytest.mark.asyncio
async def test_non_spotify_text_falls_through(mock_ui):
    ctx = MagicMock()
    ctx.user_data = {}
    mock_ui.spotify.parse_spotify_url.return_value = None
    mock_ui.queue_state.queue_spotify_async = AsyncMock()
    mock_ui.kodi_api.SC_SET.search.return_value = None
    mock_ui.kodi_api.SC.search.return_value = None
    mock_ui.kodi_api.SC_SHORT.search.return_value = None
    mock_ui.kodi_api.YT.search.return_value = None
    mock_ui.kodi_api.PL.search.return_value = None
    mock_ui.media.download_social_video_item = AsyncMock(return_value=None)

    await ui_text.handle_text(spotify_update("nur irgendein text"), ctx)

    assert not mock_ui.queue_state.queue_spotify_async.called
    mock_ui.warn_and_cleanup_chat.assert_awaited()
