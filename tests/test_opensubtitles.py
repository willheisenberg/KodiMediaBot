import os

os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "1")

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import requests

from kodibot.config import Config
from kodibot.core import opensubtitles


def test_opensubtitles_disabled_without_credentials(monkeypatch):
    monkeypatch.delenv("OPENSUBTITLES_API_KEY", raising=False)
    monkeypatch.delenv("OPENSUBTITLES_USER", raising=False)
    monkeypatch.delenv("OPENSUBTITLES_PASS", raising=False)
    monkeypatch.delenv("OPENSUBTITLES_LANGUAGES", raising=False)
    cfg = Config.from_env()
    assert cfg.opensubtitles_enabled is False
    assert cfg.opensubtitles_language_list == ["de", "en"]


def test_opensubtitles_enabled_with_credentials(monkeypatch):
    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "key")
    monkeypatch.setenv("OPENSUBTITLES_USER", "user")
    monkeypatch.setenv("OPENSUBTITLES_PASS", "pass")
    monkeypatch.setenv("OPENSUBTITLES_LANGUAGES", "de, en , fr")
    cfg = Config.from_env()
    assert cfg.opensubtitles_enabled is True
    assert cfg.opensubtitles_language_list == ["de", "en", "fr"]


def test_opensubtitles_needs_all_three_credentials(monkeypatch):
    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "key")
    monkeypatch.setenv("OPENSUBTITLES_USER", "user")
    monkeypatch.delenv("OPENSUBTITLES_PASS", raising=False)
    cfg = Config.from_env()
    assert cfg.opensubtitles_enabled is False


def test_normalize_language_maps_iso_639_2():
    assert opensubtitles.normalize_language("ger") == "de"
    assert opensubtitles.normalize_language("deu") == "de"
    assert opensubtitles.normalize_language("DE") == "de"
    assert opensubtitles.normalize_language("eng") == "en"
    assert opensubtitles.normalize_language("") == ""


def _response(status=200, payload=None, content=b""):
    res = MagicMock()
    res.status_code = status
    res.json.return_value = payload if payload is not None else {}
    res.content = content
    return res


def _credentials(cfg):
    """Fill a patched CFG mock with usable OpenSubtitles credentials."""
    cfg.opensubtitles_api_key = "key"
    cfg.opensubtitles_user = "user"
    cfg.opensubtitles_pass = "pass"
    cfg.opensubtitles_enabled = True
    cfg.opensubtitles_language_list = ["de", "en"]
    return cfg


def test_ensure_token_logs_in_once():
    opensubtitles.reset_session()
    with patch("kodibot.core.opensubtitles.CFG") as cfg, \
         patch("kodibot.core.opensubtitles.requests.post") as post:
        _credentials(cfg)
        post.return_value = _response(payload={"token": "abc"})
        assert opensubtitles.ensure_token() is True
        assert opensubtitles.ensure_token() is True
        assert post.call_count == 1


def test_ensure_token_returns_false_on_bad_credentials():
    opensubtitles.reset_session()
    with patch("kodibot.core.opensubtitles.CFG") as cfg, \
         patch("kodibot.core.opensubtitles.requests.post") as post:
        _credentials(cfg)
        post.return_value = _response(status=401)
        assert opensubtitles.ensure_token() is False


def test_headers_carry_api_key_and_user_agent():
    with patch("kodibot.core.opensubtitles.CFG") as cfg:
        _credentials(cfg)
        headers = opensubtitles._headers()
    assert headers["Api-Key"] == "key"
    assert headers["User-Agent"] == opensubtitles.USER_AGENT
    assert "Authorization" not in headers


def test_strip_tt_removes_prefix_and_leading_zeros():
    assert opensubtitles.strip_tt("tt1375666") == "1375666"
    assert opensubtitles.strip_tt("tt0133093") == "133093"
    assert opensubtitles.strip_tt("") == ""


def test_search_uses_imdb_id_for_movies():
    payload = {"data": [{"attributes": {"files": [{"file_id": 998877}]}}]}
    with patch("kodibot.core.opensubtitles.ensure_token", return_value=True), \
         patch("kodibot.core.opensubtitles.requests.get") as get:
        get.return_value = _response(payload=payload)
        file_id = opensubtitles.search("tt1375666", "de")
    assert file_id == 998877
    params = get.call_args.kwargs["params"]
    assert params["imdb_id"] == "1375666"
    assert params["type"] == "movie"
    assert params["languages"] == "de"
    assert "season_number" not in params


def test_search_uses_parent_id_and_numbers_for_episodes():
    payload = {"data": [{"attributes": {"files": [{"file_id": 42}]}}]}
    with patch("kodibot.core.opensubtitles.ensure_token", return_value=True), \
         patch("kodibot.core.opensubtitles.requests.get") as get:
        get.return_value = _response(payload=payload)
        file_id = opensubtitles.search(
            "", "en", season=2, episode=5, parent_imdb_id="tt0903747"
        )
    assert file_id == 42
    params = get.call_args.kwargs["params"]
    assert params["parent_imdb_id"] == "903747"
    assert params["season_number"] == 2
    assert params["episode_number"] == 5
    assert params["type"] == "episode"


def test_search_without_any_id_returns_none():
    with patch("kodibot.core.opensubtitles.ensure_token") as ensure, \
         patch("kodibot.core.opensubtitles.requests.get") as get:
        assert opensubtitles.search("", "de") is None
        get.assert_not_called()
        # No id means no request at all -- not even the token refresh.
        ensure.assert_not_called()


def test_search_returns_none_without_results():
    with patch("kodibot.core.opensubtitles.ensure_token", return_value=True), \
         patch("kodibot.core.opensubtitles.requests.get") as get:
        get.return_value = _response(payload={"data": []})
        assert opensubtitles.search("tt1375666", "de") is None


def test_search_calls_ensure_token_before_the_request():
    """A fresh session must resolve a possible VIP base before searching --
    otherwise search and the following download can land on different
    hosts."""
    payload = {"data": [{"attributes": {"files": [{"file_id": 5}]}}]}
    with patch("kodibot.core.opensubtitles.ensure_token", return_value=True) as ensure, \
         patch("kodibot.core.opensubtitles.requests.get") as get:
        get.return_value = _response(payload=payload)
        opensubtitles.search("tt1375666", "de")
    ensure.assert_called_once()


def test_download_fetches_the_temporary_link():
    opensubtitles.reset_session()
    with patch("kodibot.core.opensubtitles.CFG") as cfg, \
         patch("kodibot.core.opensubtitles.requests.post") as post, \
         patch("kodibot.core.opensubtitles.requests.get") as get:
        _credentials(cfg)
        post.side_effect = [
            _response(payload={"token": "abc"}),
            _response(payload={"link": "https://dl.example/sub.srt", "remaining": 17}),
        ]
        get.return_value = _response(content=b"1\n00:00:01,000 --> 00:00:02,000\nHallo\n")
        content = opensubtitles.download(998877)
        assert get.call_args.args[0] == "https://dl.example/sub.srt"
    assert content.startswith(b"1\n")


def test_download_retries_once_after_401():
    opensubtitles.reset_session()
    with patch("kodibot.core.opensubtitles.CFG") as cfg, \
         patch("kodibot.core.opensubtitles.requests.post") as post, \
         patch("kodibot.core.opensubtitles.requests.get") as get:
        _credentials(cfg)
        post.side_effect = [
            _response(payload={"token": "stale"}),
            _response(status=401),
            _response(payload={"token": "fresh"}),
            _response(payload={"link": "https://dl.example/sub.srt"}),
        ]
        srt_bytes = b"1\n00:00:01,000 --> 00:00:02,000\nHi\n"
        get.return_value = _response(content=srt_bytes)
        assert opensubtitles.download(1) == srt_bytes
        assert post.call_count == 4


def test_download_raises_on_quota_exceeded():
    opensubtitles.reset_session()
    with patch("kodibot.core.opensubtitles.CFG") as cfg, \
         patch("kodibot.core.opensubtitles.requests.post") as post:
        _credentials(cfg)
        post.side_effect = [
            _response(payload={"token": "abc"}),
            _response(status=406, payload={"reset_time": "2026-09-15T00:00:00Z"}),
        ]
        try:
            opensubtitles.download(1)
        except opensubtitles.OpenSubtitlesError as err:
            assert err.message == "quota_exceeded"
            assert err.reset_time == "2026-09-15T00:00:00Z"
        else:
            raise AssertionError("expected OpenSubtitlesError")


def test_download_raises_opensubtitles_error_on_connection_error():
    opensubtitles.reset_session()
    with patch("kodibot.core.opensubtitles.CFG") as cfg, \
         patch("kodibot.core.opensubtitles.requests.post") as post:
        _credentials(cfg)
        post.side_effect = [
            _response(payload={"token": "abc"}),
            requests.exceptions.ConnectionError("boom"),
        ]
        try:
            opensubtitles.download(1)
        except opensubtitles.OpenSubtitlesError:
            pass
        else:
            raise AssertionError("expected OpenSubtitlesError")


def test_download_rejects_oversized_content():
    opensubtitles.reset_session()
    with patch("kodibot.core.opensubtitles.CFG") as cfg, \
         patch("kodibot.core.opensubtitles.requests.post") as post, \
         patch("kodibot.core.opensubtitles.requests.get") as get:
        _credentials(cfg)
        post.side_effect = [
            _response(payload={"token": "abc"}),
            _response(payload={"link": "https://dl.example/sub.srt"}),
        ]
        oversized = b"1\n00:00:01,000 --> 00:00:02,000\n" + b"x" * (
            opensubtitles.MAX_SUBTITLE_BYTES + 1
        )
        get.return_value = _response(content=oversized)
        try:
            opensubtitles.download(1)
        except opensubtitles.OpenSubtitlesError as err:
            assert err.message == "content_too_large"
        else:
            raise AssertionError("expected OpenSubtitlesError")


def test_download_rejects_content_that_does_not_look_like_a_subtitle():
    opensubtitles.reset_session()
    with patch("kodibot.core.opensubtitles.CFG") as cfg, \
         patch("kodibot.core.opensubtitles.requests.post") as post, \
         patch("kodibot.core.opensubtitles.requests.get") as get:
        _credentials(cfg)
        post.side_effect = [
            _response(payload={"token": "abc"}),
            _response(payload={"link": "https://dl.example/sub.srt"}),
        ]
        get.return_value = _response(content=b"<html><body>Not Found</body></html>")
        try:
            opensubtitles.download(1)
        except opensubtitles.OpenSubtitlesError as err:
            assert err.message == "invalid_content"
        else:
            raise AssertionError("expected OpenSubtitlesError")


def test_download_accepts_webvtt_content():
    opensubtitles.reset_session()
    with patch("kodibot.core.opensubtitles.CFG") as cfg, \
         patch("kodibot.core.opensubtitles.requests.post") as post, \
         patch("kodibot.core.opensubtitles.requests.get") as get:
        _credentials(cfg)
        post.side_effect = [
            _response(payload={"token": "abc"}),
            _response(payload={"link": "https://dl.example/sub.vtt"}),
        ]
        vtt = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHi\n"
        get.return_value = _response(content=vtt)
        assert opensubtitles.download(1) == vtt


from kodibot.telegram import ui as UI  # noqa: F401 (import order avoids circular import below)
from kodibot.telegram import state as S
from kodibot.telegram import ui_callbacks


def test_fallback_writer_stores_in_upload_dir(tmp_path):
    with patch("kodibot.telegram.ui.CFG") as cfg:
        cfg.upload_dir = str(tmp_path)
        cfg.kodi_upload_dir = "/storage/uploads"
        path = ui_callbacks._write_subtitle_fallback(
            "/storage/videos/Inception.mkv", "de", b"data"
        )
    assert path == "/storage/uploads/subs/Inception.de.srt"
    assert (tmp_path / "subs" / "Inception.de.srt").read_bytes() == b"data"


def test_fetch_missing_subtitles_skips_when_feature_disabled():
    av_state = {"subtitles": []}
    with patch("kodibot.telegram.ui.CFG") as cfg:
        cfg.opensubtitles_enabled = False
        result = asyncio.run(ui_callbacks._fetch_missing_subtitles(None, 1, av_state))
    assert result is av_state


def test_fetch_missing_subtitles_skips_when_both_languages_present():
    av_state = {
        "playerid": 1,
        "subtitles": [
            {"language": "ger", "isforced": False},
            {"language": "ger", "isforced": True},
            {"language": "eng", "isforced": False},
            {"language": "eng", "isforced": True},
        ],
    }
    with patch("kodibot.telegram.ui.CFG") as cfg, \
         patch("kodibot.core.opensubtitles.CFG") as os_cfg, \
         patch("kodibot.core.kodi_library.now_playing_media_info") as info:
        cfg.opensubtitles_enabled = True
        os_cfg.opensubtitles_language_list = ["de", "en"]
        result = asyncio.run(ui_callbacks._fetch_missing_subtitles(None, 1, av_state))
        info.assert_not_called()
    assert result is av_state


def test_subtitle_language_name_falls_back_to_code():
    assert ui_callbacks._subtitle_language_name("de") == "Deutsch"
    assert ui_callbacks._subtitle_language_name("zz") == "ZZ"


def test_fetch_missing_subtitles_happy_path_disables_and_refreshes_state():
    av_state = {"playerid": 1, "subtitles": []}
    info = {
        "file": "/storage/videos/Show.S01E02.mkv",
        "imdb_id": "tt1234567",
        "parent_imdb_id": None,
        "season": None,
        "episode": None,
    }
    status = MagicMock(message_id=42)
    refreshed = {
        "subtitles": [{"language": "deu", "index": 0}],
        "currentsubtitle": {"index": None},
        "subtitleenabled": False,
    }
    with patch("kodibot.telegram.state.SUBTITLE_ATTACHED_PATHS", {}), \
         patch("kodibot.telegram.ui.CFG") as cfg, \
         patch("kodibot.core.opensubtitles.CFG") as os_cfg, \
         patch("kodibot.core.kodi_library.now_playing_media_info", return_value=info), \
         patch("kodibot.core.kodi_library.subtitle_exists_via_ssh", return_value=False), \
         patch("kodibot.core.kodi_library.write_subtitle_via_ssh", return_value="/storage/videos/Show.S01E02.de.srt"), \
         patch("kodibot.core.opensubtitles.fetch_for", return_value=b"1\n00:00:01,000 --> 00:00:02,000\nHallo\n"), \
         patch("kodibot.telegram.ui.send_and_track", new=AsyncMock(return_value=status)), \
         patch("kodibot.telegram.ui.send_toast_message", new=AsyncMock()), \
         patch("kodibot.telegram.ui.delete_message_if_present", new=AsyncMock()), \
         patch("kodibot.core.kodi_api.add_subtitle_file", return_value=True), \
         patch("kodibot.core.kodi_api.disable_subtitles") as disable, \
         patch("kodibot.core.kodi_api.set_subtitle_stream") as restore, \
         patch("kodibot.core.kodi_api.get_av_settings", return_value=refreshed) as get_av, \
         patch("asyncio.sleep", new=AsyncMock()):
        cfg.opensubtitles_enabled = True
        os_cfg.opensubtitles_language_list = ["de", "en"]
        result = asyncio.run(ui_callbacks._fetch_missing_subtitles(None, 1, av_state))
    # AddSubtitle turns the track on right away; disable_subtitles() must undo
    # that, and get_av_settings() must be re-read since the old stream indexes
    # are stale once a track was added.
    disable.assert_called_once()
    get_av.assert_called_once()
    # Nothing was active before the fetch (no subtitleenabled/currentsubtitle
    # on the input av_state), so there is nothing to restore.
    restore.assert_not_called()
    assert result is refreshed


def test_fetch_missing_subtitles_restores_previously_active_track():
    """CRITICAL: opening the menu must not silently mute a track the guest
    already had running -- disable_subtitles() is global, not scoped to the
    newly added tracks, so whatever was active before has to come back."""
    av_state = {
        "playerid": 1,
        "subtitles": [{"language": "eng", "index": 0}],
        "subtitleenabled": True,
        "currentsubtitle": {"index": 0},
    }
    info = {
        "file": "/storage/videos/Show.S01E02.mkv",
        "imdb_id": "tt1234567",
        "parent_imdb_id": None,
        "season": None,
        "episode": None,
    }
    status = MagicMock(message_id=42)
    refreshed = {
        "subtitles": [{"language": "eng", "index": 0}, {"language": "deu", "index": 1}],
        "currentsubtitle": {"index": 1},
        "subtitleenabled": True,
    }
    with patch("kodibot.telegram.state.SUBTITLE_ATTACHED_PATHS", {}), \
         patch("kodibot.telegram.ui.CFG") as cfg, \
         patch("kodibot.core.opensubtitles.CFG") as os_cfg, \
         patch("kodibot.core.kodi_library.now_playing_media_info", return_value=info), \
         patch("kodibot.core.kodi_library.subtitle_exists_via_ssh", return_value=False), \
         patch("kodibot.core.kodi_library.write_subtitle_via_ssh", return_value="/storage/videos/Show.S01E02.de.srt"), \
         patch("kodibot.core.opensubtitles.fetch_for", return_value=b"1\n00:00:01,000 --> 00:00:02,000\nHallo\n"), \
         patch("kodibot.telegram.ui.send_and_track", new=AsyncMock(return_value=status)), \
         patch("kodibot.telegram.ui.send_toast_message", new=AsyncMock()), \
         patch("kodibot.telegram.ui.delete_message_if_present", new=AsyncMock()), \
         patch("kodibot.core.kodi_api.add_subtitle_file", return_value=True), \
         patch("kodibot.core.kodi_api.disable_subtitles") as disable, \
         patch("kodibot.core.kodi_api.set_subtitle_stream") as restore, \
         patch("kodibot.core.kodi_api.get_av_settings", return_value=refreshed), \
         patch("asyncio.sleep", new=AsyncMock()):
        cfg.opensubtitles_enabled = True
        os_cfg.opensubtitles_language_list = ["de", "en"]
        asyncio.run(ui_callbacks._fetch_missing_subtitles(None, 1, av_state))
    disable.assert_called_once()
    restore.assert_called_once_with(0)


def test_fetch_missing_subtitles_never_raises_when_telegram_call_fails():
    av_state = {"playerid": 1, "subtitles": []}
    info = {
        "file": "/storage/videos/Show.S01E02.mkv",
        "imdb_id": "tt1234567",
        "parent_imdb_id": None,
        "season": None,
        "episode": None,
    }
    with patch("kodibot.telegram.ui.CFG") as cfg, \
         patch("kodibot.core.opensubtitles.CFG") as os_cfg, \
         patch("kodibot.core.kodi_library.now_playing_media_info", return_value=info), \
         patch("kodibot.telegram.ui.send_and_track", new=AsyncMock(side_effect=RuntimeError("network down"))):
        cfg.opensubtitles_enabled = True
        os_cfg.opensubtitles_language_list = ["de", "en"]
        result = asyncio.run(ui_callbacks._fetch_missing_subtitles(None, 1, av_state))
    assert result is av_state


def test_fetch_missing_subtitles_quota_exceeded_returns_state_without_raising():
    av_state = {"playerid": 1, "subtitles": []}
    info = {
        "file": "/storage/videos/Show.S01E02.mkv",
        "imdb_id": "tt1234567",
        "parent_imdb_id": None,
        "season": None,
        "episode": None,
    }
    status = MagicMock(message_id=7)
    quota_error = opensubtitles.OpenSubtitlesError(
        "quota_exceeded", reset_time="2026-09-15T00:00:00Z"
    )
    with patch("kodibot.telegram.ui.CFG") as cfg, \
         patch("kodibot.core.opensubtitles.CFG") as os_cfg, \
         patch("kodibot.core.kodi_library.now_playing_media_info", return_value=info), \
         patch("kodibot.core.kodi_library.subtitle_exists_via_ssh", return_value=False), \
         patch("kodibot.core.opensubtitles.fetch_for", side_effect=quota_error), \
         patch("kodibot.telegram.ui.send_and_track", new=AsyncMock(return_value=status)), \
         patch("kodibot.telegram.ui.send_toast_message", new=AsyncMock()) as toast, \
         patch("kodibot.telegram.ui.delete_message_if_present", new=AsyncMock()):
        cfg.opensubtitles_enabled = True
        os_cfg.opensubtitles_language_list = ["de", "en"]
        result = asyncio.run(ui_callbacks._fetch_missing_subtitles(None, 1, av_state))
    toast.assert_called_once()
    assert result is av_state


def test_fetch_missing_subtitles_skips_on_av_state_error():
    """CRITICAL: get_av_settings() reports a Kodi RPC failure without a
    "subtitles" key at all -- that must not be read as "every language is
    missing", or a single hiccup burns the daily download quota."""
    av_state = {"playerid": 1, "error": {"code": -32602, "message": "boom"}}
    with patch("kodibot.telegram.ui.CFG") as cfg, \
         patch("kodibot.core.kodi_library.now_playing_media_info") as info:
        cfg.opensubtitles_enabled = True
        result = asyncio.run(ui_callbacks._fetch_missing_subtitles(None, 1, av_state))
        info.assert_not_called()
    assert result is av_state


def test_fetch_missing_subtitles_skips_when_no_active_player():
    av_state = {"playerid": None, "error": "nothing_playing"}
    with patch("kodibot.telegram.ui.CFG") as cfg, \
         patch("kodibot.core.kodi_library.now_playing_media_info") as info:
        cfg.opensubtitles_enabled = True
        result = asyncio.run(ui_callbacks._fetch_missing_subtitles(None, 1, av_state))
        info.assert_not_called()
    assert result is av_state


def test_fetch_missing_subtitles_uses_fallback_cache_before_downloading(tmp_path):
    """IMPORTANT: the design promises the fallback upload dir is checked as a
    cache too, not just the path next to the movie -- otherwise an smb://- or
    nfs://-mounted library (subtitle_exists_via_ssh always False) redownloads
    and rewrites the same file on every single playback."""
    av_state = {"playerid": 1, "subtitles": []}
    info = {
        "file": "/storage/videos/Cached.mkv",
        "imdb_id": "tt1111111",
        "parent_imdb_id": None,
        "season": None,
        "episode": None,
    }
    subs_dir = tmp_path / "subs"
    subs_dir.mkdir()
    (subs_dir / "Cached.de.srt").write_bytes(b"cached-de")
    (subs_dir / "Cached.en.srt").write_bytes(b"cached-en")
    (subs_dir / "Cached.de.forced.srt").write_bytes(b"cached-de-forced")
    (subs_dir / "Cached.en.forced.srt").write_bytes(b"cached-en-forced")
    status = MagicMock(message_id=5)
    refreshed = {
        "subtitles": [{"language": "deu"}, {"language": "eng"}],
        "currentsubtitle": {},
        "subtitleenabled": False,
    }
    with patch("kodibot.telegram.state.SUBTITLE_ATTACHED_PATHS", {}), \
         patch("kodibot.telegram.ui.CFG") as cfg, \
         patch("kodibot.core.opensubtitles.CFG") as os_cfg, \
         patch("kodibot.core.kodi_library.now_playing_media_info", return_value=info), \
         patch("kodibot.core.kodi_library.subtitle_exists_via_ssh", return_value=False), \
         patch("kodibot.core.opensubtitles.fetch_for") as fetch_for, \
         patch("kodibot.telegram.ui.send_and_track", new=AsyncMock(return_value=status)), \
         patch("kodibot.telegram.ui.send_toast_message", new=AsyncMock()), \
         patch("kodibot.telegram.ui.delete_message_if_present", new=AsyncMock()), \
         patch("kodibot.core.kodi_api.add_subtitle_file", return_value=True) as add_file, \
         patch("kodibot.core.kodi_api.disable_subtitles"), \
         patch("kodibot.core.kodi_api.set_subtitle_stream"), \
         patch("kodibot.core.kodi_api.get_av_settings", return_value=refreshed), \
         patch("asyncio.sleep", new=AsyncMock()):
        cfg.opensubtitles_enabled = True
        cfg.upload_dir = str(tmp_path)
        cfg.kodi_upload_dir = "/storage/uploads"
        os_cfg.opensubtitles_language_list = ["de", "en"]
        asyncio.run(ui_callbacks._fetch_missing_subtitles(None, 1, av_state))
    fetch_for.assert_not_called()
    called_paths = {c.args[0] for c in add_file.call_args_list}
    assert called_paths == {
        "/storage/uploads/subs/Cached.de.srt",
        "/storage/uploads/subs/Cached.en.srt",
        "/storage/uploads/subs/Cached.de.forced.srt",
        "/storage/uploads/subs/Cached.en.forced.srt",
    }


def test_fetch_missing_subtitles_skips_already_attached_path_for_same_file():
    """IMPORTANT: Kodi can label an AddSubtitle-loaded track empty/"und",
    which keeps it showing up as "missing" forever -- each menu open must not
    re-attach (and thus stack) the same file again."""
    av_state = {"playerid": 1, "subtitles": []}
    info = {
        "file": "/storage/videos/Dup.mkv",
        "imdb_id": "tt9999999",
        "parent_imdb_id": None,
        "season": None,
        "episode": None,
    }
    status = MagicMock(message_id=1)
    refreshed = {
        "subtitles": [{"language": "und", "index": 0}],
        "currentsubtitle": {},
        "subtitleenabled": False,
        "playerid": 1,
    }
    attached_paths = {}
    add_calls = []

    def fake_add(path):
        add_calls.append(path)
        return True

    with patch("kodibot.telegram.state.SUBTITLE_ATTACHED_PATHS", attached_paths), \
         patch("kodibot.telegram.ui.CFG") as cfg, \
         patch("kodibot.core.opensubtitles.CFG") as os_cfg, \
         patch("kodibot.core.kodi_library.now_playing_media_info", return_value=info), \
         patch("kodibot.core.kodi_library.subtitle_exists_via_ssh", return_value=True), \
         patch(
             "kodibot.core.kodi_library.subtitle_path_for",
             side_effect=lambda video, lang: f"/storage/videos/Dup.{lang}.srt",
         ), \
         patch("kodibot.telegram.ui.send_and_track", new=AsyncMock(return_value=status)), \
         patch("kodibot.telegram.ui.send_toast_message", new=AsyncMock()), \
         patch("kodibot.telegram.ui.delete_message_if_present", new=AsyncMock()), \
         patch("kodibot.core.kodi_api.add_subtitle_file", side_effect=fake_add), \
         patch("kodibot.core.kodi_api.disable_subtitles") as disable, \
         patch("kodibot.core.kodi_api.set_subtitle_stream"), \
         patch("kodibot.core.kodi_api.get_av_settings", return_value=refreshed), \
         patch("asyncio.sleep", new=AsyncMock()):
        cfg.opensubtitles_enabled = True
        os_cfg.opensubtitles_language_list = ["de", "en"]
        asyncio.run(ui_callbacks._fetch_missing_subtitles(None, 1, av_state))
        assert sorted(add_calls) == [
            "/storage/videos/Dup.de.forced.srt",
            "/storage/videos/Dup.de.srt",
            "/storage/videos/Dup.en.forced.srt",
            "/storage/videos/Dup.en.srt",
        ]
        disable.assert_called_once()

        # Second open: Kodi still reports both as missing (mislabeled track),
        # but both paths were already attached for this exact file -- must
        # not add them again.
        result = asyncio.run(ui_callbacks._fetch_missing_subtitles(None, 1, av_state))
    assert sorted(add_calls) == [
        "/storage/videos/Dup.de.forced.srt",
        "/storage/videos/Dup.de.srt",
        "/storage/videos/Dup.en.forced.srt",
        "/storage/videos/Dup.en.srt",
    ]
    disable.assert_called_once()
    assert result is av_state


def test_download_accepts_srt_with_utf8_bom():
    """A UTF-8 BOM is common in the OpenSubtitles corpus. Rejecting it would
    spend a download from the daily quota and still deliver no subtitle."""
    opensubtitles.reset_session()
    with patch("kodibot.core.opensubtitles.CFG") as cfg, \
         patch("kodibot.core.opensubtitles.requests.post") as post, \
         patch("kodibot.core.opensubtitles.requests.get") as get:
        _credentials(cfg)
        post.side_effect = [
            _response(payload={"token": "abc"}),
            _response(payload={"link": "https://dl.example/sub.srt"}),
        ]
        srt = b"\xef\xbb\xbf1\n00:00:01,000 --> 00:00:02,000\nHallo\n"
        get.return_value = _response(content=srt)
        assert opensubtitles.download(1) == srt


def test_download_accepts_webvtt_with_utf8_bom():
    """The WebVTT spec explicitly allows a leading BOM before the header."""
    opensubtitles.reset_session()
    with patch("kodibot.core.opensubtitles.CFG") as cfg, \
         patch("kodibot.core.opensubtitles.requests.post") as post, \
         patch("kodibot.core.opensubtitles.requests.get") as get:
        _credentials(cfg)
        post.side_effect = [
            _response(payload={"token": "abc"}),
            _response(payload={"link": "https://dl.example/sub.vtt"}),
        ]
        vtt = b"\xef\xbb\xbfWEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHi\n"
        get.return_value = _response(content=vtt)
        assert opensubtitles.download(1) == vtt


def test_fetch_missing_subtitles_reattaches_after_playback_restart():
    """The dedup guard is scoped to one playback. Once a new playback clears
    it, the same file must be attachable again -- otherwise a network-mounted
    library silently loses its subtitles on every replay."""
    av_state = {"playerid": 1, "subtitles": []}
    info = {
        "file": "/storage/videos/Replay.mkv",
        "imdb_id": "tt9999999",
        "parent_imdb_id": None,
        "season": None,
        "episode": None,
    }
    status = MagicMock(message_id=1)
    refreshed = {
        "subtitles": [{"language": "und", "index": 0}],
        "currentsubtitle": {},
        "subtitleenabled": False,
        "playerid": 1,
    }
    attached_paths = {}
    add_calls = []

    with patch("kodibot.telegram.state.SUBTITLE_ATTACHED_PATHS", attached_paths), \
         patch("kodibot.telegram.ui.CFG") as cfg, \
         patch("kodibot.core.opensubtitles.CFG") as os_cfg, \
         patch("kodibot.core.kodi_library.now_playing_media_info", return_value=info), \
         patch("kodibot.core.kodi_library.subtitle_exists_via_ssh", return_value=True), \
         patch(
             "kodibot.core.kodi_library.subtitle_path_for",
             side_effect=lambda video, lang: f"/storage/videos/Replay.{lang}.srt",
         ), \
         patch("kodibot.telegram.ui.send_and_track", new=AsyncMock(return_value=status)), \
         patch("kodibot.telegram.ui.send_toast_message", new=AsyncMock()), \
         patch("kodibot.telegram.ui.delete_message_if_present", new=AsyncMock()), \
         patch(
             "kodibot.core.kodi_api.add_subtitle_file",
             side_effect=lambda path: add_calls.append(path) or True,
         ), \
         patch("kodibot.core.kodi_api.disable_subtitles"), \
         patch("kodibot.core.kodi_api.set_subtitle_stream"), \
         patch("kodibot.core.kodi_api.get_av_settings", return_value=refreshed), \
         patch("asyncio.sleep", new=AsyncMock()):
        cfg.opensubtitles_enabled = True
        os_cfg.opensubtitles_language_list = ["de", "en"]
        asyncio.run(ui_callbacks._fetch_missing_subtitles(None, 1, av_state))
        assert len(add_calls) == 4

        # Player.OnPlay fires and clears the guard -- this is what
        # queue_state._handle_ws_play does through its on_play_started hook.
        attached_paths.clear()

        asyncio.run(ui_callbacks._fetch_missing_subtitles(None, 1, av_state))
    assert len(add_calls) == 8


def test_is_forced_track_reads_the_kodi_flag():
    assert opensubtitles.is_forced_track({"language": "ger", "isforced": True}) is True
    assert opensubtitles.is_forced_track({"language": "ger", "isforced": False}) is False


def test_is_forced_track_falls_back_to_the_name():
    """Kodi below v20 has no isforced field -- the label is all we get."""
    assert opensubtitles.is_forced_track({"language": "ger", "name": "German Forced"}) is True
    assert opensubtitles.is_forced_track({"language": "ger", "name": "German"}) is False
    assert opensubtitles.is_forced_track({"language": "ger"}) is False


def test_missing_tracks_wants_both_variants_when_nothing_is_there():
    assert opensubtitles.missing_tracks({}, ["de", "en"]) == [
        ("de", False),
        ("de", True),
        ("en", False),
        ("en", True),
    ]


def test_missing_tracks_still_wants_forced_next_to_a_full_track():
    """The whole point: a present German full track must not hide the forced one."""
    av_state = {"subtitles": [{"language": "deu", "name": "German"}]}
    assert opensubtitles.missing_tracks(av_state, ["de"]) == [("de", True)]


def test_missing_tracks_still_wants_the_full_track_next_to_a_forced_one():
    av_state = {"subtitles": [{"language": "deu", "name": "German Forced"}]}
    assert opensubtitles.missing_tracks(av_state, ["de"]) == [("de", False)]


def test_missing_tracks_reports_nothing_when_both_variants_are_present():
    av_state = {
        "subtitles": [
            {"language": "deu", "isforced": False},
            {"language": "deu", "isforced": True},
        ]
    }
    assert opensubtitles.missing_tracks(av_state, ["de"]) == []


def test_missing_tracks_ignores_unrelated_languages():
    av_state = {"subtitles": [{"language": "fre"}, {"language": "spa"}]}
    assert opensubtitles.missing_tracks(av_state, ["de"]) == [("de", False), ("de", True)]


def test_search_excludes_forced_results_by_default():
    """Without this the most-downloaded hit can be a 30-line forced track
    that lands on disk as the full German subtitle."""
    payload = {"data": [{"attributes": {"files": [{"file_id": 1}]}}]}
    with patch("kodibot.core.opensubtitles.ensure_token", return_value=True), \
         patch("kodibot.core.opensubtitles.requests.get") as get:
        get.return_value = _response(payload=payload)
        opensubtitles.search("tt1375666", "de")
    assert get.call_args.kwargs["params"]["foreign_parts_only"] == "exclude"


def test_search_asks_for_forced_results_only_when_forced():
    payload = {"data": [{"attributes": {"files": [{"file_id": 1}]}}]}
    with patch("kodibot.core.opensubtitles.ensure_token", return_value=True), \
         patch("kodibot.core.opensubtitles.requests.get") as get:
        get.return_value = _response(payload=payload)
        opensubtitles.search("tt1375666", "de", forced=True)
    assert get.call_args.kwargs["params"]["foreign_parts_only"] == "only"


def test_fetch_for_passes_forced_through_to_search():
    with patch("kodibot.core.opensubtitles.CFG") as cfg, \
         patch("kodibot.core.opensubtitles.search", return_value=7) as search, \
         patch("kodibot.core.opensubtitles.download", return_value=b"1\n") as download:
        _credentials(cfg)
        assert opensubtitles.fetch_for("tt1375666", "de", forced=True) == b"1\n"
    assert search.call_args.kwargs["forced"] is True
    download.assert_called_once_with(7)


def test_subtitle_suffix_marks_forced_files():
    assert opensubtitles.subtitle_suffix("de", False) == "de"
    assert opensubtitles.subtitle_suffix("de", True) == "de.forced"


def test_fetch_missing_subtitles_fetches_forced_next_to_an_existing_full_track():
    """The feature: German is already there as a full track, the forced one
    is still missing -- and lands next to the movie under its own name."""
    av_state = {"playerid": 1, "subtitles": [{"language": "deu", "name": "German"}]}
    info = {
        "file": "/storage/videos/Lotr.mkv",
        "imdb_id": "tt0120737",
        "parent_imdb_id": None,
        "season": None,
        "episode": None,
    }
    status = MagicMock(message_id=3)
    refreshed = {"subtitles": [], "currentsubtitle": {}, "subtitleenabled": False}
    written = []
    with patch("kodibot.telegram.state.SUBTITLE_ATTACHED_PATHS", {}), \
         patch("kodibot.telegram.ui.CFG") as cfg, \
         patch("kodibot.core.opensubtitles.CFG") as os_cfg, \
         patch("kodibot.core.kodi_library.now_playing_media_info", return_value=info), \
         patch("kodibot.core.kodi_library.subtitle_exists_via_ssh", return_value=False), \
         patch(
             "kodibot.core.kodi_library.write_subtitle_via_ssh",
             side_effect=lambda video, lang, content: written.append(lang)
             or f"/storage/videos/Lotr.{lang}.srt",
         ), \
         patch("kodibot.core.opensubtitles.fetch_for", return_value=b"1\n") as fetch_for, \
         patch("kodibot.telegram.ui.send_and_track", new=AsyncMock(return_value=status)), \
         patch("kodibot.telegram.ui.send_toast_message", new=AsyncMock()), \
         patch("kodibot.telegram.ui.delete_message_if_present", new=AsyncMock()), \
         patch("kodibot.core.kodi_api.add_subtitle_file", return_value=True) as add_file, \
         patch("kodibot.core.kodi_api.disable_subtitles"), \
         patch("kodibot.core.kodi_api.set_subtitle_stream"), \
         patch("kodibot.core.kodi_api.get_av_settings", return_value=refreshed), \
         patch("asyncio.sleep", new=AsyncMock()):
        cfg.opensubtitles_enabled = True
        os_cfg.opensubtitles_language_list = ["de"]
        asyncio.run(ui_callbacks._fetch_missing_subtitles(None, 1, av_state))
    assert written == ["de.forced"]
    assert fetch_for.call_args.args[-1] is True
    assert add_file.call_args.args[0] == "/storage/videos/Lotr.de.forced.srt"


def test_fetch_missing_subtitles_stays_quiet_when_no_forced_track_exists():
    """Most films have no forced track at all -- a toast for every one of them
    would bury the chat, while a missing full track must still be reported."""
    av_state = {"playerid": 1, "subtitles": []}
    info = {
        "file": "/storage/videos/Quiet.mkv",
        "imdb_id": "tt0111161",
        "parent_imdb_id": None,
        "season": None,
        "episode": None,
    }
    status = MagicMock(message_id=4)
    toast = AsyncMock()
    with patch("kodibot.telegram.state.SUBTITLE_ATTACHED_PATHS", {}), \
         patch("kodibot.telegram.ui.CFG") as cfg, \
         patch("kodibot.core.opensubtitles.CFG") as os_cfg, \
         patch("kodibot.core.kodi_library.now_playing_media_info", return_value=info), \
         patch("kodibot.core.kodi_library.subtitle_exists_via_ssh", return_value=False), \
         patch("kodibot.core.opensubtitles.fetch_for", return_value=None), \
         patch("kodibot.telegram.ui.send_and_track", new=AsyncMock(return_value=status)), \
         patch("kodibot.telegram.ui.send_toast_message", new=toast), \
         patch("kodibot.telegram.ui.delete_message_if_present", new=AsyncMock()), \
         patch("kodibot.core.kodi_api.add_subtitle_file", return_value=True), \
         patch("asyncio.sleep", new=AsyncMock()):
        cfg.opensubtitles_enabled = True
        cfg.upload_dir = "/nonexistent"
        cfg.kodi_upload_dir = "/storage/uploads"
        os_cfg.opensubtitles_language_list = ["de"]
        result = asyncio.run(ui_callbacks._fetch_missing_subtitles(None, 1, av_state))
    # One toast for the missing full track, none for the missing forced one.
    assert toast.await_count == 1
    assert result is av_state


def test_reset_subtitle_playback_state_clears_both_guards():
    """Both are scoped to one playback -- the attach guard and the record of
    what OpenSubtitles had nothing for."""
    S.SUBTITLE_ATTACHED_PATHS["/a.mkv"] = {"/a.de.srt"}
    S.SUBTITLE_SEARCH_MISSES["/a.mkv"] = {("de", True)}
    S.reset_subtitle_playback_state()
    assert S.SUBTITLE_ATTACHED_PATHS == {}
    assert S.SUBTITLE_SEARCH_MISSES == {}


def _fetch_twice_counting_searches(av_state, info, misses):
    """Run the fetch twice for one file and report how often it searched."""
    status = MagicMock(message_id=9)
    refreshed = {"subtitles": [], "currentsubtitle": {}, "subtitleenabled": False}
    with patch("kodibot.telegram.state.SUBTITLE_ATTACHED_PATHS", {}), \
         patch("kodibot.telegram.state.SUBTITLE_SEARCH_MISSES", misses), \
         patch("kodibot.telegram.ui.CFG") as cfg, \
         patch("kodibot.core.opensubtitles.CFG") as os_cfg, \
         patch("kodibot.core.kodi_library.now_playing_media_info", return_value=info), \
         patch("kodibot.core.kodi_library.subtitle_exists_via_ssh", return_value=False), \
         patch("kodibot.core.opensubtitles.fetch_for", return_value=None) as fetch_for, \
         patch("kodibot.telegram.ui.send_and_track", new=AsyncMock(return_value=status)), \
         patch("kodibot.telegram.ui.send_toast_message", new=AsyncMock()), \
         patch("kodibot.telegram.ui.delete_message_if_present", new=AsyncMock()), \
         patch("kodibot.core.kodi_api.add_subtitle_file", return_value=True), \
         patch("kodibot.core.kodi_api.get_av_settings", return_value=refreshed), \
         patch("asyncio.sleep", new=AsyncMock()):
        cfg.opensubtitles_enabled = True
        cfg.upload_dir = "/nonexistent"
        cfg.kodi_upload_dir = "/storage/uploads"
        os_cfg.opensubtitles_language_list = ["de"]
        asyncio.run(ui_callbacks._fetch_missing_subtitles(None, 1, av_state))
        first = fetch_for.call_count
        asyncio.run(ui_callbacks._fetch_missing_subtitles(None, 1, av_state))
    return first, fetch_for.call_count


def test_fetch_missing_subtitles_searches_a_dead_end_only_once_per_playback():
    """Opening the menu again during the same playback must not repeat a
    search that already came back empty -- nothing can have changed since."""
    av_state = {"playerid": 1, "subtitles": []}
    info = {
        "file": "/storage/videos/Nothing.mkv",
        "imdb_id": "tt0111161",
        "parent_imdb_id": None,
        "season": None,
        "episode": None,
    }
    first, total = _fetch_twice_counting_searches(av_state, info, {})
    # Full and forced on the first open, nothing at all on the second.
    assert first == 2
    assert total == 2


def test_fetch_missing_subtitles_searches_again_after_a_new_playback():
    av_state = {"playerid": 1, "subtitles": []}
    info = {
        "file": "/storage/videos/Nothing.mkv",
        "imdb_id": "tt0111161",
        "parent_imdb_id": None,
        "season": None,
        "episode": None,
    }
    misses = {}
    _fetch_twice_counting_searches(av_state, info, misses)
    assert misses["/storage/videos/Nothing.mkv"] == {("de", False), ("de", True)}
    # The hook clears the record; the next playback may find a fresh upload.
    S.reset_subtitle_playback_state()
    first, total = _fetch_twice_counting_searches(av_state, info, {})
    assert first == 2


def test_fetch_missing_subtitles_does_not_remember_a_failed_request_as_a_miss():
    """A network or quota error says nothing about whether the track exists --
    only an empty search result may be remembered."""
    av_state = {"playerid": 1, "subtitles": []}
    info = {
        "file": "/storage/videos/Broken.mkv",
        "imdb_id": "tt0111161",
        "parent_imdb_id": None,
        "season": None,
        "episode": None,
    }
    status = MagicMock(message_id=9)
    misses = {}
    with patch("kodibot.telegram.state.SUBTITLE_ATTACHED_PATHS", {}), \
         patch("kodibot.telegram.state.SUBTITLE_SEARCH_MISSES", misses), \
         patch("kodibot.telegram.ui.CFG") as cfg, \
         patch("kodibot.core.opensubtitles.CFG") as os_cfg, \
         patch("kodibot.core.kodi_library.now_playing_media_info", return_value=info), \
         patch("kodibot.core.kodi_library.subtitle_exists_via_ssh", return_value=False), \
         patch(
             "kodibot.core.opensubtitles.fetch_for",
             side_effect=opensubtitles.OpenSubtitlesError("request_failed"),
         ), \
         patch("kodibot.telegram.ui.send_and_track", new=AsyncMock(return_value=status)), \
         patch("kodibot.telegram.ui.send_toast_message", new=AsyncMock()), \
         patch("kodibot.telegram.ui.delete_message_if_present", new=AsyncMock()), \
         patch("asyncio.sleep", new=AsyncMock()):
        cfg.opensubtitles_enabled = True
        cfg.upload_dir = "/nonexistent"
        cfg.kodi_upload_dir = "/storage/uploads"
        os_cfg.opensubtitles_language_list = ["de"]
        asyncio.run(ui_callbacks._fetch_missing_subtitles(None, 1, av_state))
    assert not any(misses.values())
