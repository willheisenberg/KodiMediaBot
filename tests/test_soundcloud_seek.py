"""Tests for seeking SoundCloud tracks through a local ffmpeg copy."""
import os
import shutil
import subprocess
import sys
import threading
from unittest.mock import patch

import pytest

os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "test:token")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from kodibot.core import kodi_api, queue_state, soundcloud_seek

LINK = "https://soundcloud.com/artist/track"
COPY_URL = "http://bot:8765/media/soundcloud-seek-1.m4a"
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _clock(sec):
    return {"hours": sec // 3600, "minutes": sec % 3600 // 60, "seconds": sec % 60}


@pytest.fixture
def source(tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    path = tmp_path / "source.m4a"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "sine=duration=2", "-c:a", "aac", str(path)],
        check=True,
    )
    return str(path)


@pytest.fixture(autouse=True)
def clean_state():
    soundcloud_seek.remember_copy(None, None)
    queue_state._SC_SEEK_JOB = None
    yield
    soundcloud_seek.remember_copy(None, None)
    queue_state._SC_SEEK_JOB = None


class TestRemux:
    @needs_ffmpeg
    def test_writes_a_complete_file(self, source, tmp_path):
        out = tmp_path / "copy.m4a"
        assert soundcloud_seek.remux(source, str(out), threading.Event()) is True
        assert out.stat().st_size > 0
        assert not (tmp_path / "copy.m4a.part.m4a").exists()

    @needs_ffmpeg
    def test_failure_leaves_no_files(self, tmp_path):
        out = tmp_path / "copy.m4a"
        assert soundcloud_seek.remux(str(tmp_path / "missing.m4a"), str(out), threading.Event()) is False
        assert list(tmp_path.iterdir()) == []

    @needs_ffmpeg
    def test_cancel_stops_ffmpeg_and_leaves_no_files(self, source, tmp_path):
        cancel = threading.Event()
        cancel.set()
        out = tmp_path / "copy.m4a"
        assert soundcloud_seek.remux(source, str(out), cancel) is False
        assert not out.exists()
        assert not (tmp_path / "copy.m4a.part.m4a").exists()

    def test_missing_ffmpeg_is_a_failure(self, tmp_path):
        with patch.object(soundcloud_seek.subprocess, "Popen", side_effect=FileNotFoundError("ffmpeg")):
            assert soundcloud_seek.remux("http://x", str(tmp_path / "copy.m4a"), threading.Event()) is False


class TestCopyMatchesQueue:
    def test_copy_counts_as_the_queue_item(self):
        qitem = queue_state.make_soundcloud(LINK)
        soundcloud_seek.remember_copy(LINK, COPY_URL)
        assert kodi_api.kodi_item_matches_queue({"file": COPY_URL}, qitem) is True

    def test_another_tracks_copy_does_not(self):
        qitem = queue_state.make_soundcloud(LINK)
        soundcloud_seek.remember_copy("https://soundcloud.com/artist/other", COPY_URL)
        assert kodi_api.kodi_item_matches_queue({"file": COPY_URL}, qitem) is False


class TestSeekRouting:
    ITEM = queue_state.make_soundcloud(LINK)

    def _seek(self, seek, item=None, file_url=None, cur=100, total=300, canseek=True):
        item = item or self.ITEM
        props = {"time": _clock(cur), "totaltime": _clock(total), "canseek": canseek}
        calls = []

        def kodi_call(method, params=None):
            calls.append(method)
            return {"result": props}

        with patch.multiple(
            "kodibot.core.queue_state",
            QUEUE=[item],
            DISPLAY_INDEX=0,
            _player_file=lambda pid: file_url or item["url"],
            _start_soundcloud_seek=lambda it, target: ("started", it, target),
            skip_queue=lambda: "skipped",
        ), patch.object(queue_state.kodi_api, "get_active_playerid", return_value=1), patch.object(
            queue_state.kodi_api, "kodi_call", side_effect=kodi_call,
        ):
            return seek(), calls

    def test_relative_seek_in_the_stream_goes_through_a_copy(self):
        result, calls = self._seek(lambda: queue_state.seek_relative_seconds(30))
        assert result == ("started", self.ITEM, 130)
        assert "Player.Seek" not in calls

    def test_works_when_kodi_says_the_stream_cannot_seek(self):
        result, _ = self._seek(lambda: queue_state.seek_relative_seconds(-30), canseek=False)
        assert result == ("started", self.ITEM, 70)

    def test_relative_seek_stops_at_the_start(self):
        result, _ = self._seek(lambda: queue_state.seek_relative_seconds(-300))
        assert result == ("started", self.ITEM, 0)

    def test_seek_past_the_end_skips_the_track(self):
        result, _ = self._seek(lambda: queue_state.seek_relative_seconds(300))
        assert result == "skipped"

    def test_percent_seek_goes_through_a_copy(self):
        result, calls = self._seek(lambda: queue_state.seek_percent(50))
        assert result == ("started", self.ITEM, 150)
        assert "Player.Seek" not in calls

    def test_relative_seek_adds_to_a_pending_target(self):
        queue_state._SC_SEEK_JOB = {"link": LINK, "target": 200, "cancel": threading.Event()}
        result, _ = self._seek(lambda: queue_state.seek_relative_seconds(30))
        assert result == ("started", self.ITEM, 230)

    def test_copy_seeks_normally(self):
        soundcloud_seek.remember_copy(LINK, COPY_URL)
        result, calls = self._seek(lambda: queue_state.seek_relative_seconds(30), file_url=COPY_URL)
        assert result is True
        assert "Player.Seek" in calls

    def test_other_tracks_seek_normally(self):
        item = queue_state.make_youtube("82O6yOy_XwE", "Clip")
        result, calls = self._seek(lambda: queue_state.seek_relative_seconds(30), item=item)
        assert result is True
        assert "Player.Seek" in calls


class TestSeekJob:
    ITEM = queue_state.make_soundcloud(LINK)

    def _run(self, tmp_path, stream="http://stream", remux=None, cancel=None, target=125):
        cancel = cancel or threading.Event()
        job = {"link": LINK, "target": target, "cancel": cancel}
        queue_state._SC_SEEK_JOB = job

        def fake_remux(url, path, event):
            with open(path, "w", encoding="utf-8") as f:
                f.write("audio")
            return True

        with patch.object(queue_state, "CFG", type("C", (), {"upload_dir": str(tmp_path)})), patch.object(
            queue_state.media, "ensure_upload_dir",
        ), patch.object(queue_state, "resolve_soundcloud_stream_url", return_value=stream), patch.object(
            soundcloud_seek, "remux", side_effect=remux or fake_remux,
        ), patch.object(
            queue_state.media, "register_temp_media", side_effect=lambda path, title: {"url": COPY_URL},
        ) as register, patch.object(queue_state, "play_item") as play:
            queue_state._run_soundcloud_seek(self.ITEM, job)
        return play, register

    def test_finished_copy_plays_at_the_target(self, tmp_path):
        play, _ = self._run(tmp_path)
        played, kwargs = play.call_args[0][0], play.call_args[1]
        assert played["url"] == COPY_URL
        assert played["kind"] == "audio"
        assert not queue_state.is_soundcloud_item(played)
        assert kwargs["resume_time"] == {"hours": 0, "minutes": 2, "seconds": 5}
        assert soundcloud_seek.copy_url(LINK) == COPY_URL
        assert queue_state._SC_SEEK_JOB is None

    def test_queue_item_keeps_its_stream_url(self, tmp_path):
        self._run(tmp_path)
        assert self.ITEM["url"].startswith("plugin://plugin.audio.soundcloud/")

    def test_failed_copy_keeps_the_stream_playing(self, tmp_path):
        play, register = self._run(tmp_path, remux=lambda *a: False)
        play.assert_not_called()
        register.assert_not_called()
        assert queue_state._SC_SEEK_JOB is None

    def test_unresolvable_stream_keeps_playing(self, tmp_path):
        play, _ = self._run(tmp_path, stream="")
        play.assert_not_called()

    def test_track_change_during_the_copy_drops_it(self, tmp_path):
        cancel = threading.Event()

        def cancelled_while_copying(url, path, event):
            with open(path, "w", encoding="utf-8") as f:
                f.write("audio")
            cancel.set()
            return True

        play, register = self._run(tmp_path, remux=cancelled_while_copying, cancel=cancel)
        play.assert_not_called()
        register.assert_not_called()
        assert list(tmp_path.iterdir()) == []

    def test_second_press_retargets_the_running_job(self):
        job = {"link": LINK, "target": 100, "cancel": threading.Event()}
        queue_state._SC_SEEK_JOB = job
        with patch.object(queue_state.threading, "Thread") as thread:
            result = queue_state._start_soundcloud_seek(self.ITEM, 200)
        assert result == queue_state.SEEK_PREPARING
        assert job["target"] == 200
        thread.assert_not_called()

    def test_cancelled_job_is_replaced(self):
        stale = threading.Event()
        stale.set()
        queue_state._SC_SEEK_JOB = {"link": LINK, "target": 100, "cancel": stale}
        with patch.object(queue_state.threading, "Thread") as thread:
            queue_state._start_soundcloud_seek(self.ITEM, 200, threading.Event())
        thread.assert_called_once()
        assert queue_state._SC_SEEK_JOB["target"] == 200


class TestResume:
    def _play(self, resume_time):
        with patch.multiple(
            "kodibot.core.queue_state",
            release_spotify_device=lambda: None,
            wait_for_players_stopped=lambda event: True,
            schedule_playback_refresh=lambda: None,
            set_expecting_ws=lambda n: None,
            schedule_soundcloud_plugin_fallback=lambda *a, **k: None,
        ), patch.object(queue_state.media, "cleanup_active_image_session"), patch.multiple(
            "kodibot.core.kodi_api",
            stop_all_players=lambda: None,
            kodi_clear_all_playlists=lambda: None,
            maybe_cache_soundcloud_url=lambda link: None,
            kodi_call=lambda *a, **k: {},
        ), patch.object(queue_state, "_start_soundcloud_seek") as start, patch.object(
            queue_state, "seek_when_player_ready",
        ) as raw_seek:
            queue_state.play_item(queue_state.make_soundcloud(LINK), resume_time=resume_time)
        return start, raw_seek

    def test_resume_goes_through_a_copy_not_a_raw_seek(self):
        start, raw_seek = self._play(_clock(90))
        assert start.call_args[0][1] == 90
        raw_seek.assert_not_called()

    def test_start_from_the_beginning_makes_no_copy(self):
        start, _ = self._play(None)
        start.assert_not_called()
