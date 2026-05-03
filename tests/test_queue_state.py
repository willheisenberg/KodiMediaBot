"""Tests for pure functions in queue_state.py"""
import os
import sys

os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "test:token")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from kodibot.core import queue_state


class TestMakeItem:
    def test_basic(self):
        item = queue_state.make_item("Test", "http://example.com", "video")
        assert item["title"] == "Test"
        assert item["url"] == "http://example.com"
        assert item["kind"] == "video"
        assert item["link"] is None

    def test_with_link(self):
        item = queue_state.make_item("Test", "http://example.com", "audio", link="http://link")
        assert item["link"] == "http://link"


class TestMakeYoutube:
    def test_creates_plugin_url(self):
        item = queue_state.make_youtube("dQw4w9WgXcQ")
        assert "plugin://plugin.video.youtube" in item["url"]
        assert "dQw4w9WgXcQ" in item["url"]
        assert item["kind"] == "video"
        assert item["link"] == "https://youtu.be/dQw4w9WgXcQ"


class TestMakeSoundcloud:
    def test_creates_plugin_url(self):
        item = queue_state.make_soundcloud("https://soundcloud.com/artist/track")
        assert "plugin://plugin.audio.soundcloud" in item["url"]
        assert item["kind"] == "audio"
        assert item["resolver"] == "soundcloud"


class TestSoundcloudDisplayTitle:
    def test_basic(self):
        result = queue_state.soundcloud_display_title("https://soundcloud.com/some-artist/some-track")
        assert "some artist" in result
        assert "some track" in result

    def test_no_match(self):
        result = queue_state.soundcloud_display_title("not-a-url")
        assert result == "not-a-url"


class TestIsSCUrls:
    def test_track_url(self):
        assert queue_state.is_sc_track_url("https://soundcloud.com/artist/track")

    def test_set_url(self):
        assert queue_state.is_sc_set_url("https://soundcloud.com/artist/sets/album")

    def test_not_sc(self):
        assert not queue_state.is_sc_track_url("https://example.com")


class TestQueueOperations:
    def setup_method(self):
        queue_state.QUEUE.clear()
        queue_state.CURRENT_INDEX = None
        queue_state.DISPLAY_INDEX = None
        queue_state.NEXT_INDEX = 0
        queue_state._SCHEDULE_NOW_PLAYING_REFRESH = None

    def test_queue_item(self):
        item = queue_state.make_item("Test", "url", "video")
        queue_state.queue_item(item)
        assert len(queue_state.QUEUE) == 1

    def test_clear_queue(self):
        queue_state.queue_item(queue_state.make_item("A", "url1", "video"))
        queue_state.queue_item(queue_state.make_item("B", "url2", "video"))
        queue_state.clear_queue()
        assert len(queue_state.QUEUE) == 0

    def test_delete_index(self):
        queue_state.queue_item(queue_state.make_item("A", "url1", "video"))
        queue_state.queue_item(queue_state.make_item("B", "url2", "video"))
        ok, msg = queue_state.delete_index(0)
        assert ok
        assert len(queue_state.QUEUE) == 1
        assert queue_state.QUEUE[0]["title"] == "B"

    def test_delete_invalid_index(self):
        ok, msg = queue_state.delete_index(5)
        assert not ok


class TestThreadSafety:
    def test_set_expecting_ws(self):
        queue_state.set_expecting_ws(5)
        assert queue_state.get_expecting_ws() == 5

    def test_decrement_expecting_ws(self):
        queue_state.set_expecting_ws(2)
        val = queue_state.decrement_expecting_ws()
        assert val == 1
        val = queue_state.decrement_expecting_ws()
        assert val == 0
        # Should not go below 0
        val = queue_state.decrement_expecting_ws()
        assert val == 0
