"""Tests for the Spektra button on the visual page, the panel marker and the WebSocket event."""

import asyncio
import dataclasses
import json
import os
import sys

import pytest

os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "test:token")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from kodibot.core import kodi_api, kodi_ws, partyvideo, spektra
from kodibot.telegram import ui as UI  # noqa: F401  (bricht den Importkreis ui <-> ui_callbacks)
from kodibot.telegram import panel, ui_callbacks


def configure(monkeypatch, partyvideo_enabled, spektra_enabled):
    cfg = dataclasses.replace(panel.CFG, partyvideo_enabled=partyvideo_enabled, spektra_enabled=spektra_enabled)
    monkeypatch.setattr(panel, "CFG", cfg)
    monkeypatch.setattr(ui_callbacks.UI, "CFG", cfg)


def callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def labels(markup):
    return {button.callback_data: button.text for row in markup.inline_keyboard for button in row}


def set_spektra(enabled):
    spektra.LAST_STATUS.clear()
    if enabled is not None:
        spektra.LAST_STATUS.update({"state": "on" if enabled else "off", "settings": {"enabled": enabled}})


@pytest.fixture(autouse=True)
def clean():
    yield
    spektra.LAST_STATUS.clear()
    partyvideo.LAST_STATUS.clear()
    panel.S.PANEL_MENU_MODE.clear()


PARTY_VIDEO_BUTTONS = ("visual:toggle", "visual:youtube", "visual:video", "visual:movie", "visual:upload",
                      "visual:cleanup")


class TestVisualPage:
    def test_only_spektra_enabled_hides_every_party_video_button(self, monkeypatch):
        configure(monkeypatch, partyvideo_enabled=False, spektra_enabled=True)
        assert "visual:menu" in callbacks(panel.control_panel(mode="controls"))
        page = callbacks(panel.control_panel(mode="visual"))
        assert page == ["visual:spektra", "visual:back"]

    def test_only_party_video_enabled_has_no_spektra_button(self, monkeypatch):
        configure(monkeypatch, partyvideo_enabled=True, spektra_enabled=False)
        page = callbacks(panel.control_panel(mode="visual"))
        assert "visual:spektra" not in page
        assert all(button in page for button in PARTY_VIDEO_BUTTONS)

    def test_both_enabled_spektra_comes_first(self, monkeypatch):
        configure(monkeypatch, partyvideo_enabled=True, spektra_enabled=True)
        page = callbacks(panel.control_panel(mode="visual"))
        assert page[:2] == ["visual:spektra", "visual:toggle"]
        assert page.count("visual:back") == 1

    def test_both_off_hides_the_visual_page(self, monkeypatch):
        configure(monkeypatch, partyvideo_enabled=False, spektra_enabled=False)
        assert "visual:menu" not in callbacks(panel.control_panel(mode="controls"))

    def test_party_video_buttons_stay_while_spektra_runs(self, monkeypatch):
        configure(monkeypatch, partyvideo_enabled=True, spektra_enabled=True)
        set_spektra(True)
        page = callbacks(panel.control_panel(mode="visual"))
        assert all(button in page for button in PARTY_VIDEO_BUTTONS)

    def test_button_follows_the_reported_state(self, monkeypatch):
        configure(monkeypatch, partyvideo_enabled=False, spektra_enabled=True)
        set_spektra(False)
        assert labels(panel.control_panel(mode="visual"))["visual:spektra"] == panel.t("spektra_on")
        set_spektra(True)
        assert labels(panel.control_panel(mode="visual"))["visual:spektra"] == panel.t("spektra_off")


class TestStatusLine:
    """One 🌌 marker for both visuals: on while Party Video or Spektra runs."""

    def parts(self, monkeypatch, partyvideo_enabled=True, spektra_enabled=True):
        configure(monkeypatch, partyvideo_enabled=partyvideo_enabled, spektra_enabled=spektra_enabled)
        return panel.build_panel_status_parts()

    def marker(self, monkeypatch, **flags):
        return [part for part in self.parts(monkeypatch, **flags) if "🌌" in part]

    def test_spektra_on_lights_the_shared_marker(self, monkeypatch):
        set_spektra(True)
        assert self.marker(monkeypatch) == [f"🌌 {panel.t('visual_status_short_on')}"]

    def test_both_off_shows_off(self, monkeypatch):
        set_spektra(False)
        assert self.marker(monkeypatch) == [f"🌌 {panel.t('visual_status_short_off')}"]

    def test_no_separate_spektra_marker(self, monkeypatch):
        set_spektra(True)
        assert not any("🌑" in part for part in self.parts(monkeypatch))

    def test_only_spektra_enabled_still_shows_the_marker(self, monkeypatch):
        set_spektra(True)
        assert self.marker(monkeypatch, partyvideo_enabled=False) == [f"🌌 {panel.t('visual_status_short_on')}"]
        set_spektra(False)
        assert self.marker(monkeypatch, partyvideo_enabled=False) == [f"🌌 {panel.t('visual_status_short_off')}"]

    def test_spektra_ignored_when_its_flag_is_off(self, monkeypatch):
        set_spektra(True)
        assert self.marker(monkeypatch, spektra_enabled=False) == [f"🌌 {panel.t('visual_status_short_off')}"]

    def test_party_video_progress_wins(self, monkeypatch):
        set_spektra(False)
        partyvideo.LAST_STATUS.update({"state": "downloading", "progress": 42})
        assert self.marker(monkeypatch) == ["🌌 42%"]

    def test_hidden_when_both_flags_are_off(self, monkeypatch):
        set_spektra(True)
        assert self.marker(monkeypatch, partyvideo_enabled=False, spektra_enabled=False) == []


class TestButtonGuard:
    def test_spektra_button_needs_spektra(self, monkeypatch):
        configure(monkeypatch, partyvideo_enabled=True, spektra_enabled=False)
        assert ui_callbacks.visual_command_disabled("visual:spektra")
        assert not ui_callbacks.visual_command_disabled("visual:toggle")

    def test_party_video_buttons_need_party_video(self, monkeypatch):
        configure(monkeypatch, partyvideo_enabled=False, spektra_enabled=True)
        assert not ui_callbacks.visual_command_disabled("visual:spektra")
        assert not ui_callbacks.visual_command_disabled("visual:menu")
        assert not ui_callbacks.visual_command_disabled("visual:back")
        assert ui_callbacks.visual_command_disabled("visual:toggle")
        assert ui_callbacks.visual_command_disabled("visual_cleanup:1")

    def test_other_buttons_are_never_blocked(self, monkeypatch):
        configure(monkeypatch, partyvideo_enabled=False, spektra_enabled=False)
        assert not ui_callbacks.visual_command_disabled("controls:back")


class _Socket:
    def __init__(self, events):
        self._events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for event in self._events:
            yield event
        raise asyncio.CancelledError


class TestWebSocket:
    def test_status_event_is_stored_and_status_requested_on_connect(self, monkeypatch):
        requested = []
        monkeypatch.setattr(spektra, "request_status", lambda: requested.append(True))
        monkeypatch.setattr(kodi_api, "CFG", dataclasses.replace(
            kodi_api.CFG, partyvideo_enabled=False, spektra_enabled=True))
        event = json.dumps({"jsonrpc": "2.0", "method": "Other.spektra_status",
                            "params": {"sender": "visualization.spektra",
                                       "data": {"state": "on", "settings": {"enabled": True}}}})
        monkeypatch.setattr(kodi_ws.websockets, "connect", lambda *a, **kw: _Socket([event]))
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(kodi_ws.kodi_ws_listener())
        assert requested == [True]
        assert spektra.is_on() is True
