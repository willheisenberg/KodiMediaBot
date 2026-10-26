"""Tests for the Spektra visualization addon bridge."""

import os
import sys

os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "test:token")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

from kodibot.core import partyvideo, spektra


class FakeKodi:
    """Records kodi_call invocations of both bridges in one list."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, params=None):
        self.calls.append(((params or {}).get("addonid"), (params or {}).get("params")))
        return {"result": "OK"}


@pytest.fixture
def kodi(monkeypatch):
    fake = FakeKodi()
    monkeypatch.setattr(spektra, "kodi_call", fake)
    monkeypatch.setattr(partyvideo, "kodi_call", fake)
    yield fake
    spektra.LAST_STATUS.clear()
    spektra.set_status_callback(None)
    partyvideo.LAST_STATUS.clear()


def spektra_on(enabled=True):
    spektra.LAST_STATUS.clear()
    spektra.LAST_STATUS.update({"state": "on" if enabled else "off", "settings": {"enabled": enabled}})


def partyvideo_playing():
    partyvideo.LAST_STATUS.clear()
    partyvideo.LAST_STATUS.update({"state": "playing", "source": "/media/clip.mp4"})


class TestCommands:
    def test_commands_reach_the_addon(self, kodi):
        spektra.on()
        spektra.off()
        spektra.request_status()
        assert kodi.calls == [
            ("visualization.spektra", ["action=on"]),
            ("visualization.spektra", ["action=off"]),
            ("visualization.spektra", ["action=status"]),
        ]


class TestStatus:
    def test_unknown_until_the_addon_reports(self, kodi):
        assert spektra.is_on() is False

    def test_enabled_setting_decides_even_after_an_error(self, kodi):
        spektra.handle_status_event({"state": "error", "error": "invalid_value", "settings": {"enabled": True}})
        assert spektra.is_on() is True
        spektra.handle_status_event({"state": "off", "settings": {"enabled": False}})
        assert spektra.is_on() is False

    def test_state_is_the_fallback_without_settings(self, kodi):
        spektra.handle_status_event({"state": "on"})
        assert spektra.is_on() is True

    def test_callback_gets_a_copy_and_may_fail(self, kodi):
        seen = []
        spektra.set_status_callback(seen.append)
        spektra.handle_status_event({"state": "on"})
        assert seen == [{"state": "on"}]

        def broken(_status):
            raise RuntimeError("UI kaputt")

        spektra.set_status_callback(broken)
        spektra.handle_status_event({"state": "off"})  # must not raise
        assert spektra.status()["state"] == "off"

    def test_ignores_garbage(self, kodi):
        spektra.handle_status_event(None)
        spektra.handle_status_event(["on"])
        assert spektra.status() == {}


class TestOnlyOneVisualAtATime:
    def test_spektra_on_stops_a_running_party_video(self, kodi):
        partyvideo_playing()
        spektra.toggle()
        assert kodi.calls == [
            ("visualization.partyvideo", ["action=stop"]),
            ("visualization.spektra", ["action=on"]),
        ]

    def test_spektra_on_leaves_an_idle_party_video_alone(self, kodi):
        spektra.toggle()
        assert kodi.calls == [("visualization.spektra", ["action=on"])]

    def test_spektra_off_touches_only_spektra(self, kodi):
        spektra_on()
        partyvideo_playing()
        spektra.toggle()
        assert kodi.calls == [("visualization.spektra", ["action=off"])]

    def test_party_video_on_turns_spektra_off(self, kodi):
        spektra_on()
        partyvideo.toggle()
        assert kodi.calls == [
            ("visualization.spektra", ["action=off"]),
            ("visualization.partyvideo", ["action=toggle"]),
        ]

    def test_party_video_off_leaves_spektra_alone(self, kodi):
        spektra_on()
        partyvideo_playing()
        partyvideo.toggle()
        assert kodi.calls == [("visualization.partyvideo", ["action=toggle"])]

    def test_party_video_sources_turn_spektra_off(self, kodi):
        spektra_on()
        partyvideo.play_url("https://youtu.be/zbo6jUGrwdk")
        spektra_on()
        partyvideo.play_path("/media/clip.mp4")
        assert [call for call in kodi.calls if call[0] == "visualization.spektra"] == [
            ("visualization.spektra", ["action=off"]),
            ("visualization.spektra", ["action=off"]),
        ]
