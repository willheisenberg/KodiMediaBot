"""Tests for the Party Video visualization addon bridge."""

import os
import sys

os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "test:token")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from kodibot.core import partyvideo


class FakeKodi:
    """Records kodi_call invocations instead of talking to Kodi."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result if result is not None else {"result": "OK"}

    def __call__(self, method, params=None):
        self.calls.append((method, params))
        return self.result


def install(monkeypatch, result=None):
    fake = FakeKodi(result)
    monkeypatch.setattr(partyvideo, "kodi_call", fake)
    return fake


def reset_status():
    partyvideo.LAST_STATUS.clear()
    partyvideo.set_status_callback(None)


class TestCommands:
    def test_play_url_sends_action_and_url(self, monkeypatch):
        fake = install(monkeypatch)
        partyvideo.play_url("https://youtu.be/zbo6jUGrwdk")
        method, params = fake.calls[0]
        assert method == "Addons.ExecuteAddon"
        assert params["addonid"] == "visualization.partyvideo"
        assert params["params"] == ["action=play", "url=https://youtu.be/zbo6jUGrwdk"]

    def test_play_path_sends_path_argument(self, monkeypatch):
        fake = install(monkeypatch)
        partyvideo.play_path("/media/MOVIES/Movies/a b.mkv")
        assert fake.calls[0][1]["params"] == ["action=play", "path=/media/MOVIES/Movies/a b.mkv"]

    def test_stop_and_toggle_and_status(self, monkeypatch):
        fake = install(monkeypatch)
        partyvideo.stop()
        partyvideo.toggle()
        partyvideo.request_status()
        assert [call[1]["params"] for call in fake.calls] == [
            ["action=stop"],
            ["action=toggle"],
            ["action=status"],
        ]

    def test_params_is_a_list_not_a_dict(self, monkeypatch):
        # Kodi joins dict params with commas without escaping; a URL would break apart.
        fake = install(monkeypatch)
        partyvideo.play_url("https://www.youtube.com/watch?v=a&list=b")
        assert isinstance(fake.calls[0][1]["params"], list)

    def test_empty_url_is_rejected_without_calling_kodi(self, monkeypatch):
        fake = install(monkeypatch)
        assert partyvideo.play_url("") is None
        assert fake.calls == []

    def test_empty_path_is_rejected_without_calling_kodi(self, monkeypatch):
        fake = install(monkeypatch)
        assert partyvideo.play_path("   ") is None
        assert fake.calls == []


class TestYoutubeUrls:
    def test_accepts_common_youtube_forms(self):
        for url in (
            "https://youtu.be/zbo6jUGrwdk",
            "https://www.youtube.com/watch?v=zbo6jUGrwdk",
            "http://youtube.com/shorts/abc123",
            "  https://youtu.be/zbo6jUGrwdk  ",
        ):
            assert partyvideo.is_youtube_url(url), url

    def test_rejects_other_input(self):
        for url in (
            "",
            "not a url",
            "https://soundcloud.com/foo/bar",
            "file:///etc/passwd",
            "https://youtube.evil.com/watch?v=x",
        ):
            assert not partyvideo.is_youtube_url(url), url


class TestStatusEvents:
    def test_event_is_stored_and_callback_fires(self, monkeypatch):
        reset_status()
        seen = []
        partyvideo.set_status_callback(seen.append)
        partyvideo.handle_status_event({"state": "downloading", "progress": 42, "title": "Clip"})
        assert partyvideo.LAST_STATUS["state"] == "downloading"
        assert partyvideo.LAST_STATUS["progress"] == 42
        assert seen and seen[0]["state"] == "downloading"

    def test_callback_errors_do_not_escape(self, monkeypatch):
        reset_status()

        def boom(_status):
            raise RuntimeError("callback broken")

        partyvideo.set_status_callback(boom)
        partyvideo.handle_status_event({"state": "playing"})
        assert partyvideo.LAST_STATUS["state"] == "playing"

    def test_non_dict_event_is_ignored(self):
        reset_status()
        partyvideo.handle_status_event(None)
        partyvideo.handle_status_event(["not", "a", "dict"])
        assert partyvideo.LAST_STATUS == {}

    def test_active_source_reflects_last_status(self):
        reset_status()
        assert partyvideo.active_source() == ""
        partyvideo.handle_status_event({"state": "playing", "source": "/data/uploads/clip.mp4"})
        assert partyvideo.active_source() == "/data/uploads/clip.mp4"

    def test_active_source_is_empty_when_idle(self):
        reset_status()
        partyvideo.handle_status_event({"state": "idle", "source": ""})
        assert partyvideo.active_source() == ""

    def test_is_on_follows_state(self):
        reset_status()
        assert not partyvideo.is_on()
        partyvideo.handle_status_event({"state": "downloading"})
        assert partyvideo.is_on()
        partyvideo.handle_status_event({"state": "playing"})
        assert partyvideo.is_on()
        partyvideo.handle_status_event({"state": "idle"})
        assert not partyvideo.is_on()
        partyvideo.handle_status_event({"state": "error"})
        assert not partyvideo.is_on()


def test_youtube_urls_with_ports_are_rejected():
    assert not partyvideo.is_youtube_url("https://youtube.com:8443/watch?v=abc123")
