"""Tests for Telegram UI translations."""

import os
import sys

os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "test:token")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from kodibot.telegram import i18n


def test_english_is_default(monkeypatch):
    monkeypatch.setattr(i18n, "LANG", "en")

    assert i18n.t("hide") == "🙈 Hide"


def test_german_translation(monkeypatch):
    monkeypatch.setattr(i18n, "LANG", "de")

    assert i18n.t("hide") == "🙈 Ausblenden"
    assert i18n.t("now_playing_title") == "🎛 Kodi Remote - Aktueller Titel:"
    assert i18n.t("queue_empty") == "Warteschlange leer."


def test_localized_asset_uses_language(monkeypatch):
    monkeypatch.setattr(i18n, "LANG", "de")

    assert i18n.localized_asset("panel_button_reference.png", "panel_button_reference_de.png").endswith(
        "assets/panel_button_reference_de.png"
    )
