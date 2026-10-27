"""Tests for telling YouTube live streams apart from ordinary videos."""
import os
import sys
from unittest.mock import patch

os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "test:token")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from kodibot.core import youtube_live

LIVE_MANIFEST = (
    "https://manifest.googlevideo.com/api/manifest/hls_variant/expire/1793145817"
    "/id/82O6yOy_XwE.22/source/yt_live_broadcast/requiressl/yes/playlist_duration/30"
    "/manifest_duration/30/playlist_type/DVR/file/index.m3u8?range=0-"
)
PLUGIN_URL = "plugin://plugin.video.youtube/play/?video_id=82O6yOy_XwE"


class TestLiveVideoId:
    def setup_method(self):
        youtube_live._LIVE.clear()
        youtube_live._PENDING.clear()

    def test_live_manifest_is_live_without_lookup(self):
        with patch.object(youtube_live, "_start_lookup", side_effect=AssertionError("lookup")):
            assert youtube_live.live_video_id(LIVE_MANIFEST, 30) == "82O6yOy_XwE"

    def test_manifest_remembers_the_video_for_its_plugin_url(self):
        youtube_live.live_video_id(LIVE_MANIFEST, 30)

        with patch.object(youtube_live, "_start_lookup", side_effect=AssertionError("lookup")):
            assert youtube_live.live_video_id(PLUGIN_URL, 30) == "82O6yOy_XwE"

    def test_unknown_plugin_url_starts_one_lookup(self):
        started = []
        with patch.object(youtube_live, "_start_lookup", side_effect=started.append):
            assert youtube_live.live_video_id(PLUGIN_URL, 30) is None
            assert youtube_live.live_video_id(PLUGIN_URL, 30) is None

        assert started == ["82O6yOy_XwE"]

    def test_lookup_answer_is_used(self):
        info = {"live_status": "is_live"}
        with patch.object(youtube_live, "YoutubeDL") as ydl_cls:
            ydl_cls.return_value.__enter__.return_value.extract_info.return_value = info
            with patch.object(youtube_live, "_start_lookup", side_effect=youtube_live._lookup):
                youtube_live.live_video_id(PLUGIN_URL, 30)

        assert youtube_live.live_video_id(PLUGIN_URL, 30) == "82O6yOy_XwE"

    def test_ordinary_short_video_is_not_live(self):
        info = {"live_status": "not_live"}
        with patch.object(youtube_live, "YoutubeDL") as ydl_cls:
            ydl_cls.return_value.__enter__.return_value.extract_info.return_value = info
            with patch.object(youtube_live, "_start_lookup", side_effect=youtube_live._lookup):
                youtube_live.live_video_id(PLUGIN_URL, 30)

        assert youtube_live.live_video_id(PLUGIN_URL, 30) is None

    def test_failed_lookup_is_retried_later(self):
        with patch.object(youtube_live, "YoutubeDL", side_effect=RuntimeError("offline")):
            with patch.object(youtube_live, "_start_lookup", side_effect=youtube_live._lookup):
                youtube_live.live_video_id(PLUGIN_URL, 30)

        assert "82O6yOy_XwE" not in youtube_live._LIVE
        assert "82O6yOy_XwE" not in youtube_live._PENDING

    def test_dash_buffer_length_is_looked_up(self):
        started = []
        with patch.object(youtube_live, "_start_lookup", side_effect=started.append):
            youtube_live.live_video_id(PLUGIN_URL, 4 * 60 * 60)

        assert started == ["82O6yOy_XwE"]

    def test_long_video_is_never_looked_up(self):
        with patch.object(youtube_live, "_start_lookup", side_effect=AssertionError("lookup")):
            assert youtube_live.live_video_id(PLUGIN_URL, 600) is None

    def test_other_files_are_not_live(self):
        with patch.object(youtube_live, "_start_lookup", side_effect=AssertionError("lookup")):
            assert youtube_live.live_video_id("smb://movies/film.mkv", 30) is None
            assert youtube_live.live_video_id(None, None) is None
