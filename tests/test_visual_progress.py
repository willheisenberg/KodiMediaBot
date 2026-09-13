"""Tests for the self-updating Party Video progress message."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "test:token")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from kodibot.core import partyvideo
from kodibot.telegram import state as S
from kodibot.telegram import ui


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    import asyncio

    monkeypatch.setattr(S, "VISUAL_PROGRESS_LOCK", asyncio.Lock())
    S.VISUAL_PROGRESS_LAST_TS.clear()
    partyvideo.LAST_STATUS.clear()
    yield
    S.VISUAL_PROGRESS_LAST_TS.clear()
    partyvideo.LAST_STATUS.clear()


class TestThrottle:
    def test_first_update_always_passes(self):
        assert ui.visual_progress_due(1, "downloading", now=100.0) is True

    def test_second_download_update_is_held_back(self):
        ui.visual_progress_due(1, "downloading", now=100.0)
        assert ui.visual_progress_due(1, "downloading", now=100.5) is False

    def test_update_passes_again_after_the_interval(self):
        ui.visual_progress_due(1, "downloading", now=100.0)
        assert ui.visual_progress_due(1, "downloading", now=102.5) is True

    def test_final_states_are_never_held_back(self):
        ui.visual_progress_due(1, "downloading", now=100.0)
        assert ui.visual_progress_due(1, "playing", now=100.1) is True
        assert ui.visual_progress_due(1, "error", now=100.2) is True

    def test_chats_are_throttled_independently(self):
        ui.visual_progress_due(1, "downloading", now=100.0)
        assert ui.visual_progress_due(2, "downloading", now=100.1) is True


@pytest.mark.asyncio
async def test_status_events_refresh_the_panel(monkeypatch):
    import dataclasses

    monkeypatch.setattr(ui, "CFG", dataclasses.replace(ui.CFG, partyvideo_enabled=True, startup_chat_id=7))
    refresh = AsyncMock()
    monkeypatch.setattr(ui, "update_now_playing_message", refresh)
    await ui._process_visual_status({"state": "playing"})
    # Der Zustand steht jetzt in der Statuszeile, es gibt keine eigene Nachricht mehr.
    refresh.assert_awaited_once()


def test_disabled_feature_does_not_schedule_status(monkeypatch):
    import dataclasses

    monkeypatch.setattr(ui, "CFG", dataclasses.replace(ui.CFG, partyvideo_enabled=False))
    schedule = MagicMock()
    monkeypatch.setattr(ui.asyncio, "run_coroutine_threadsafe", schedule)
    ui.on_visual_status({"state": "playing"})
    schedule.assert_not_called()


