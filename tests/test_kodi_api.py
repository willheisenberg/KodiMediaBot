"""Tests for pure functions in kodi_api.py"""
import os
import sys

# Set required env vars before importing
os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "test:token")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from kodibot.core import kodi_api


class TestExtractYouTubeId:
    def test_watch_url(self):
        m = kodi_api.YT.search("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert m and m.group(1) == "dQw4w9WgXcQ"

    def test_short_url(self):
        m = kodi_api.YT.search("https://youtu.be/dQw4w9WgXcQ")
        assert m and m.group(1) == "dQw4w9WgXcQ"

    def test_shorts_url(self):
        m = kodi_api.YT.search("https://youtube.com/shorts/dQw4w9WgXcQ")
        assert m and m.group(1) == "dQw4w9WgXcQ"

    def test_no_match(self):
        m = kodi_api.YT.search("https://example.com/something")
        assert m is None


class TestPlaylistMatch:
    def test_playlist_url(self):
        m = kodi_api.PL.search("https://www.youtube.com/watch?v=abc&list=PLtest123")
        assert m and m.group(1) == "PLtest123"


class TestFormatKodiTime:
    def test_full_time(self):
        assert kodi_api.format_kodi_time({"hours": 1, "minutes": 23, "seconds": 45}) == "1:23:45"

    def test_short_time(self):
        assert kodi_api.format_kodi_time({"hours": 0, "minutes": 3, "seconds": 5}) == "03:05"

    def test_none(self):
        assert kodi_api.format_kodi_time(None) == "00:00"


class TestKodiTimeSeconds:
    def test_full(self):
        assert kodi_api.kodi_time_seconds({"hours": 1, "minutes": 2, "seconds": 3}) == 3723

    def test_none(self):
        assert kodi_api.kodi_time_seconds(None) is None


class TestNormalizeTitle:
    def test_basic(self):
        result = kodi_api.normalize_title("  Hello, World!  ")
        assert "hello" in result
        assert "world" in result

    def test_dashes(self):
        result = kodi_api.normalize_title("Artist - Song Title")
        assert "artist" in result


class TestNormalizeMatchText:
    def test_removes_special(self):
        result = kodi_api.normalize_match_text("Hello (Official Video) [HD]")
        assert "official" not in result
        assert "hd" not in result


class TestExtractSoundcloudTrackId:
    def test_track_url(self):
        result = kodi_api.extract_soundcloud_track_id("soundcloud:tracks:123456")
        assert result == "123456"

    def test_api_url(self):
        result = kodi_api.extract_soundcloud_track_id("/tracks/789012")
        assert result == "789012"

    def test_no_match(self):
        result = kodi_api.extract_soundcloud_track_id("nothing here")
        assert result == ""


class TestFavouriteMediaTarget:
    def test_media_type(self):
        fav = {"type": "media", "path": "plugin://some/url"}
        result = kodi_api.favourite_media_target(fav)
        assert result == "plugin://some/url"

    def test_windowparam_direct_url(self):
        fav = {"type": "window", "windowparameter": "plugin://some/url"}
        result = kodi_api.favourite_media_target(fav)
        assert result == "plugin://some/url"

    def test_unknown_type(self):
        fav = {"type": "window", "windowparameter": "something else"}
        result = kodi_api.favourite_media_target(fav)
        assert result is None
