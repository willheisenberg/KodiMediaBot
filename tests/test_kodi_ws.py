"""Tests for the Kodi WebSocket listener helpers."""
import asyncio
import json
import os
import sys

os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "test:token")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from kodibot.core import kodi_api, kodi_ws


class _Socket:
    """Stands in for the Kodi WebSocket: yields the given events, then ends."""

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
        # The listener reconnects forever; this ends the test run.
        raise asyncio.CancelledError


def _run_listener(monkeypatch, events, item_file):
    calls = []

    async def fake_call(method, params=None):
        calls.append((method, params))
        if method == "Player.GetItem":
            return {"result": {"item": {"file": item_file, "type": "unknown"}}}
        return {"result": "OK"}

    monkeypatch.setattr(kodi_api, "kodi_call_async", fake_call)
    monkeypatch.setattr(kodi_api, "_ws_on_play", None)
    monkeypatch.setattr(kodi_api, "_ws_on_playback_refresh", None)
    monkeypatch.setattr(kodi_api, "_ws_on_spotify_takeover", None)
    monkeypatch.setattr(kodi_api, "CFG", kodi_api.CFG.__class__(
        **{**kodi_api.CFG.__dict__, "partyvideo_enabled": False}
    ))
    monkeypatch.setattr(kodi_ws.websockets, "connect", lambda *a, **kw: _Socket(events))

    try:
        asyncio.run(kodi_ws.kodi_ws_listener())
    except asyncio.CancelledError:
        pass
    return [params for method, params in calls if method == "Player.SetSubtitle"]


def _play_event(method):
    return json.dumps({
        "jsonrpc": "2.0",
        "method": method,
        "params": {"data": {"item": {"type": "unknown"}, "player": {"playerid": 1, "speed": 1}}},
    })


YOUTUBE_FILE = "plugin://plugin.video.youtube/play/?video_id=dYPXINFcvmI"


class TestIsYoutubePlaybackFile:
    def test_plugin_url(self):
        assert kodi_ws.is_youtube_playback_file(YOUTUBE_FILE)

    def test_local_manifest(self):
        assert kodi_ws.is_youtube_playback_file(
            "http://127.0.0.1:50152/youtube/manifest/dash/dYPXINFcvmI.mpd"
        )

    def test_resolved_stream(self):
        assert kodi_ws.is_youtube_playback_file(
            "https://rr3---sn-4g5e6nsz.googlevideo.com/videoplayback?expire=1"
        )

    def test_library_film_and_empty(self):
        assert not kodi_ws.is_youtube_playback_file("/storage/videos/Film (2020)/Film.mkv")
        assert not kodi_ws.is_youtube_playback_file("")
        assert not kodi_ws.is_youtube_playback_file(None)


class TestYoutubeSubtitlesStartOff:
    def test_avstart_of_a_youtube_video_switches_subtitles_off(self, monkeypatch):
        calls = _run_listener(monkeypatch, [_play_event("Player.OnAVStart")], YOUTUBE_FILE)

        assert calls == [{"playerid": 1, "subtitle": "off", "enable": False}]

    def test_onplay_alone_does_not_touch_subtitles(self, monkeypatch):
        # The streams are not open yet; Kodi would pick its default afterwards.
        calls = _run_listener(monkeypatch, [_play_event("Player.OnPlay")], YOUTUBE_FILE)

        assert calls == []

    def test_library_film_keeps_kodis_own_choice(self, monkeypatch):
        calls = _run_listener(
            monkeypatch,
            [_play_event("Player.OnAVStart")],
            "/storage/videos/Film (2020)/Film.mkv",
        )

        assert calls == []
