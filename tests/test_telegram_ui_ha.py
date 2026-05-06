"""Tests for Home Assistant menu helpers in ui.py."""

import os
import sys

os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "test:token")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from telegram import InlineKeyboardButton

from kodibot.telegram import ui


class TestFormatHaStateText:
    def test_returns_empty_string_without_state(self):
        assert ui.format_ha_state_text(None) == ""

    def test_includes_entity_id_state_hex_color_and_brightness(self):
        old_light_id = ui.CFG.ha_light_id
        object.__setattr__(ui.CFG, "ha_light_id", "light.test")
        try:
            text = ui.format_ha_state_text(
                {
                    "state": "on",
                    "friendly_name": "Wohnzimmer",
                    "rgb_color": [255, 85, 0],
                    "brightness": 128,
                }
            )
        finally:
            object.__setattr__(ui.CFG, "ha_light_id", old_light_id)

        assert "light.test" in text
        assert "on" in text
        assert "#FF5500" in text
        assert "50%" in text


class TestHaMenuMarkup:
    def test_build_main_mini_app_url(self):
        url = ui.build_main_mini_app_url("@KodiBot", "ha_color", "compact")

        assert url == "https://t.me/KodiBot?startapp=ha_color&mode=compact"

    def test_build_main_mini_app_url_rejects_invalid_start_param(self):
        url = ui.build_main_mini_app_url("KodiBot", "ha color", "compact")

        assert url == ""

    def test_build_main_mini_app_url_rejects_invalid_mode(self):
        url = ui.build_main_mini_app_url("KodiBot", "ha_color", "half")

        assert url == ""

    def test_main_menu_includes_load_color_button(self):
        markup = ui.build_ha_main_menu_markup()

        assert markup.inline_keyboard[0][1].text == "🎨 Load Color"
        assert markup.inline_keyboard[0][1].callback_data == "ha:loadcolor"

    def test_main_menu_includes_live_color_and_load_color_in_private_layout(self):
        markup = ui.build_ha_main_menu_markup(
            live_color_button=InlineKeyboardButton("🎛 Live Color", callback_data="ha:noop")
        )

        assert markup.inline_keyboard[0][1].text == "🎛 Live Color"
        assert markup.inline_keyboard[1][0].text == "🎨 Load Color"
        assert markup.inline_keyboard[2][0].text == "🔆 Brightness"

    def test_main_menu_includes_brightness_button(self):
        markup = ui.build_ha_main_menu_markup()

        assert markup.inline_keyboard[1][1].text == "🔆 Brightness"
        assert markup.inline_keyboard[1][1].callback_data == "ha:brightness"

    def test_main_menu_includes_cancel_button(self):
        markup = ui.build_ha_main_menu_markup()

        assert markup.inline_keyboard[-1][0].text == "Cancel"
        assert markup.inline_keyboard[-1][0].callback_data == "ha:close"

    def test_preset_menu_includes_saved_color_buttons_below_presets(self):
        markup = ui.build_ha_preset_menu_markup(
            [
                {"name": "Sunset", "r": 255, "g": 85, "b": 0},
                {"name": "Ocean", "r": 0, "g": 120, "b": 255},
            ]
        )

        assert markup.inline_keyboard[4][0].text == "🪩 Disco"
        assert markup.inline_keyboard[4][0].callback_data == "ha:effect:colorloop"
        assert markup.inline_keyboard[5][0].text == "💾 Saved Colors"
        assert markup.inline_keyboard[6][0].text == "Sunset"
        assert markup.inline_keyboard[6][0].callback_data == "ha:savedcolor:0"
        assert markup.inline_keyboard[7][0].text == "Ocean"
        assert markup.inline_keyboard[7][0].callback_data == "ha:savedcolor:1"
        assert markup.inline_keyboard[8][0].text == "🗑 Delete Color"
        assert markup.inline_keyboard[8][0].callback_data == "ha:deletecolor:ask"

    def test_preset_menu_includes_back_and_cancel(self):
        markup = ui.build_ha_preset_menu_markup([])
        bottom_row = markup.inline_keyboard[-1]

        assert bottom_row[0].callback_data == "ha:back"
        assert bottom_row[1].callback_data == "ha:close"
