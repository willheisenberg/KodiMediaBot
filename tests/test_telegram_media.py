"""Tests for pure functions in media.py"""
import os
import sys

os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "test:token")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from kodibot.telegram import media


class TestSanitizeStem:
    def test_basic(self):
        result = media.sanitize_stem("Hello World")
        assert result == "Hello_World"

    def test_special_chars(self):
        result = media.sanitize_stem("file/with:bad*chars")
        assert "/" not in result
        assert ":" not in result
        assert "*" not in result

    def test_empty(self):
        result = media.sanitize_stem("")
        assert result == "upload"  # default fallback


class TestFormatBytes:
    def test_bytes(self):
        assert "B" in media.format_bytes(100)

    def test_megabytes(self):
        result = media.format_bytes(5 * 1024 * 1024)
        assert "MB" in result

    def test_zero(self):
        assert "0" in media.format_bytes(0)


class TestIsSupportedSocialVideoUrl:
    def test_tiktok(self):
        assert media.is_supported_social_video_url("https://www.tiktok.com/@user/video/123")

    def test_instagram(self):
        assert media.is_supported_social_video_url("https://instagram.com/reel/abc")

    def test_youtube_not_social(self):
        assert not media.is_supported_social_video_url("https://youtube.com/watch?v=abc")

    def test_random_url(self):
        assert not media.is_supported_social_video_url("https://example.com/video")


class TestNormalizeMediaUrl:
    def test_strips_query(self):
        result = media.normalize_media_url("http://host:8765/media/test.mp3?extra=1")
        assert "extra" not in result

    def test_empty(self):
        result = media.normalize_media_url("")
        assert result == ""


class TestChooseExtension:
    def test_video_from_filename(self):
        ext = media.choose_extension("test.mp4", "video/mp4", ".bin")
        assert ext == ".mp4"

    def test_audio_from_mime(self):
        ext = media.choose_extension(None, "audio/mpeg", ".bin")
        assert ext in (".mp3", ".mpeg", ".mp2")

    def test_fallback(self):
        ext = media.choose_extension(None, None, ".bin")
        assert ext == ".bin"
