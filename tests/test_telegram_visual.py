"""Tests for the Party Video panel page."""

import dataclasses
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

from kodibot.core import partyvideo
from kodibot.telegram import ui as UI  # noqa: F401  (bricht den Importkreis ui <-> ui_callbacks)
from kodibot.telegram import i18n, panel


@pytest.fixture(autouse=True)
def visual_enabled(monkeypatch):
    """The feature ships off; every test below exercises the enabled path."""
    monkeypatch.setattr(panel, "CFG", dataclasses.replace(panel.CFG, partyvideo_enabled=True))


def buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def callbacks(markup):
    return [button.callback_data for button in buttons(markup)]


def set_state(state, **fields):
    partyvideo.LAST_STATUS.clear()
    if state is not None:
        partyvideo.LAST_STATUS.update(dict(fields, state=state))


class TestVisualPanelPage:
    def teardown_method(self):
        partyvideo.LAST_STATUS.clear()
        panel.S.PANEL_MENU_MODE.clear()

    def test_controls_page_offers_the_visual_button(self):
        markup = panel.control_panel(mode="controls")
        assert "visual:menu" in callbacks(markup)

    def test_visual_page_has_all_entries(self):
        set_state(None)
        markup = panel.control_panel(mode="visual")
        assert callbacks(markup) == [
            "visual:toggle",
            "visual:youtube",
            "visual:video",
            "visual:movie",
            "visual:upload",
            "visual:cleanup",
            "visual:back",
        ]

    def test_visual_page_does_not_show_seek_buttons(self):
        markup = panel.control_panel(mode="visual")
        assert all(not cb.startswith("seek:") for cb in callbacks(markup))

    def test_back_returns_to_the_controls_page(self):
        panel.set_panel_menu_mode(7, "visual")
        assert panel.panel_menu_mode(7) == "visual"
        panel.set_panel_menu_mode(7, "controls")
        assert panel.panel_menu_mode(7) == "controls"

    def test_unknown_mode_falls_back_to_main(self):
        panel.set_panel_menu_mode(7, "nonsense")
        assert panel.panel_menu_mode(7) == "main"


class TestToggleLabel:
    def teardown_method(self):
        partyvideo.LAST_STATUS.clear()

    def test_label_offers_to_switch_on_while_idle(self, monkeypatch):
        monkeypatch.setattr(i18n, "LANG", "de")
        set_state("idle")
        label = buttons(panel.control_panel(mode="visual"))[0].text
        assert "ein" in label.lower()

    def test_label_offers_to_switch_off_while_playing(self, monkeypatch):
        monkeypatch.setattr(i18n, "LANG", "de")
        set_state("playing")
        label = buttons(panel.control_panel(mode="visual"))[0].text
        assert "aus" in label.lower()

    def test_label_offers_to_switch_off_while_downloading(self, monkeypatch):
        # A running download already holds a source, so the button must cancel it.
        monkeypatch.setattr(i18n, "LANG", "de")
        set_state("downloading", progress=12)
        label = buttons(panel.control_panel(mode="visual"))[0].text
        assert "aus" in label.lower()

    def test_english_labels_are_translated(self, monkeypatch):
        monkeypatch.setattr(i18n, "LANG", "en")
        set_state("playing")
        label = buttons(panel.control_panel(mode="visual"))[0].text
        assert "off" in label.lower()

    def test_unknown_state_offers_to_switch_on(self, monkeypatch):
        monkeypatch.setattr(i18n, "LANG", "de")
        set_state(None)
        label = buttons(panel.control_panel(mode="visual"))[0].text
        assert "ein" in label.lower()


class TestFeatureFlag:
    def teardown_method(self):
        partyvideo.LAST_STATUS.clear()

    def test_button_is_hidden_when_the_flag_is_off(self, monkeypatch):
        monkeypatch.setattr(panel, "CFG", dataclasses.replace(panel.CFG, partyvideo_enabled=False))
        assert "visual:menu" not in callbacks(panel.control_panel(mode="controls"))

    def test_button_appears_when_the_flag_is_on(self, monkeypatch):
        monkeypatch.setattr(panel, "CFG", dataclasses.replace(panel.CFG, partyvideo_enabled=True))
        assert "visual:menu" in callbacks(panel.control_panel(mode="controls"))

    def test_disabled_flag_falls_back_to_the_controls_page(self, monkeypatch):
        monkeypatch.setattr(panel, "CFG", dataclasses.replace(panel.CFG, partyvideo_enabled=False))
        # Even if the mode was stored earlier, the page must not be reachable.
        assert "visual:toggle" not in callbacks(panel.control_panel(mode="visual"))

    def test_default_is_off(self):
        from kodibot.config import _bool_env
        assert _bool_env("PARTYVIDEO_ENABLED_TEST_UNSET", False) is False


@pytest.mark.asyncio
async def test_disabled_feature_rejects_old_buttons(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    from kodibot.telegram import ui, ui_callbacks

    monkeypatch.setattr(ui, "CFG", dataclasses.replace(ui.CFG, partyvideo_enabled=False))
    execute = MagicMock()
    monkeypatch.setattr(partyvideo, "execute", execute)
    for command in ("visual:toggle", "visual:youtube", "visual:menu", "visual_cleanup:yes"):
        update = MagicMock()
        update.callback_query.data = command
        update.callback_query.answer = AsyncMock()
        await ui_callbacks.on_button(update, MagicMock())
        update.callback_query.answer.assert_awaited_once()
    execute.assert_not_called()


class TestStatusLine:
    def teardown_method(self):
        partyvideo.LAST_STATUS.clear()

    def parts(self, monkeypatch, enabled=True):
        monkeypatch.setattr(panel, "CFG", dataclasses.replace(panel.CFG, partyvideo_enabled=enabled))
        return panel.build_panel_status_parts()

    def test_hidden_when_the_feature_is_off(self, monkeypatch):
        set_state("playing", title="Clip")
        assert not any("🌌" in part for part in self.parts(monkeypatch, enabled=False))

    def test_shows_off_while_idle(self, monkeypatch):
        set_state("idle")
        visual = [p for p in self.parts(monkeypatch) if "🌌" in p]
        assert len(visual) == 1 and "off" in visual[0].lower()

    def test_shows_on_while_playing(self, monkeypatch):
        set_state("playing", title="Clip")
        visual = [p for p in self.parts(monkeypatch) if "🌌" in p]
        assert len(visual) == 1 and "on" in visual[0].lower()

    def test_shows_the_percentage_while_downloading(self, monkeypatch):
        set_state("downloading", progress=42)
        visual = [p for p in self.parts(monkeypatch) if "🌌" in p]
        assert len(visual) == 1 and "42" in visual[0]

    def test_marks_an_error(self, monkeypatch):
        set_state("error", error="download_failed")
        visual = [p for p in self.parts(monkeypatch) if "🌌" in p]
        assert len(visual) == 1 and "⚠" in visual[0]

    def test_without_any_status_it_shows_off(self, monkeypatch):
        set_state(None)
        visual = [p for p in self.parts(monkeypatch) if "🌌" in p]
        assert len(visual) == 1 and "off" in visual[0].lower()


class TestVideoPrompt:
    def teardown_method(self):
        partyvideo.LAST_STATUS.clear()

    def test_menu_offers_the_video_button(self):
        assert "visual:video" in callbacks(panel.control_panel(mode="visual"))

    def test_menu_keeps_every_other_entry(self):
        expected = {
            "visual:toggle", "visual:youtube", "visual:movie", "visual:video",
            "visual:upload", "visual:cleanup", "visual:back",
        }
        assert set(callbacks(panel.control_panel(mode="visual"))) == expected
