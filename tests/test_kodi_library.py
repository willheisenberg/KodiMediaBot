import os
os.environ["TG_TOKEN"] = "1"
os.environ["KODI_HOST"] = "1"
os.environ["KODI_USER"] = "1"
os.environ["KODI_PASS"] = "1"
os.environ["KODI_PORT"] = "1"
os.environ["KODI_WS_PORT"] = "1"

import subprocess

import kodibot.core.kodi_api
from unittest.mock import patch, MagicMock
from kodibot.core.kodi_library import get_ctimes_via_ssh
import pytest

@patch("kodibot.core.kodi_api.CFG")
@patch("subprocess.run")
def test_get_ctimes_via_ssh(mock_run, mock_cfg):
    # Setup mock configuration
    mock_cfg.cec_host = "192.168.1.100"
    
    # Mock files
    files = [
        "/storage/videos/movie1.mkv",
        "/storage/videos/movie2.mkv",
        "/storage/videos/movie3.mkv"
    ]
    
    # Setup mock subprocess output
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "/storage/videos/movie1.mkv|1710000000\n/storage/videos/movie2.mkv|1720000000\n/storage/videos/movie3.mkv|1730000000\n"
    mock_run.return_value = mock_result
    
    # Call function
    result = get_ctimes_via_ssh(files)
    
    # Assert correct map
    assert result == {
        "/storage/videos/movie1.mkv": 1710000000,
        "/storage/videos/movie2.mkv": 1720000000,
        "/storage/videos/movie3.mkv": 1730000000
    }
    
    # Assert subprocess was called correctly
    mock_run.assert_called_once()
    args, kwargs = mock_run.call_args
    assert "input" in kwargs

    # Critical test: make sure the trailing newline is in the input
    # so the shell 'read' command doesn't skip the last line
    assert kwargs["input"].endswith("\n")
    assert kwargs["input"].count("\n") == 3

    # The SSH call must not hang forever, and must use the same
    # BatchMode/ConnectTimeout hardening as the subtitle SSH calls.
    assert kwargs["timeout"] == 20
    assert "BatchMode=yes" in args[0]
    assert "ConnectTimeout=5" in args[0]

def test_get_ctimes_empty():
    assert get_ctimes_via_ssh([]) == {}


from kodibot.core import kodi_library


def test_subtitle_path_for_appends_language():
    assert (
        kodi_library.subtitle_path_for("/storage/videos/Inception.mkv", "de")
        == "/storage/videos/Inception.de.srt"
    )


def test_subtitle_path_for_keeps_dots_in_directories():
    assert (
        kodi_library.subtitle_path_for("/storage/my.movies/Inception", "en")
        == "/storage/my.movies/Inception.en.srt"
    )


def test_subtitle_path_for_handles_empty_path():
    assert kodi_library.subtitle_path_for("", "de") == ""


@patch("kodibot.core.kodi_api.CFG")
@patch("subprocess.run")
def test_write_subtitle_via_ssh_returns_path_on_success(mock_run, mock_cfg):
    mock_cfg.cec_host = "192.168.1.100"
    mock_run.return_value = MagicMock(returncode=0, stdout=b"ok\n", stderr=b"")
    path = kodi_library.write_subtitle_via_ssh(
        "/storage/videos/Inception.mkv", "de", b"subtitle bytes"
    )
    assert path == "/storage/videos/Inception.de.srt"
    assert mock_run.call_args.kwargs["input"] == b"subtitle bytes"


@patch("kodibot.core.kodi_api.CFG")
@patch("subprocess.run")
def test_write_subtitle_via_ssh_returns_none_on_failure(mock_run, mock_cfg):
    mock_cfg.cec_host = "192.168.1.100"
    mock_run.return_value = MagicMock(returncode=1, stdout=b"nodir\n", stderr=b"")
    assert (
        kodi_library.write_subtitle_via_ssh("/storage/videos/x.mkv", "de", b"data")
        is None
    )


@patch("kodibot.core.kodi_api.CFG")
@patch("subprocess.run")
def test_write_subtitle_via_ssh_skips_network_sources(mock_run, mock_cfg):
    mock_cfg.cec_host = "192.168.1.100"
    assert kodi_library.write_subtitle_via_ssh("smb://movies/x.mkv", "de", b"d") is None
    mock_run.assert_not_called()


@patch("kodibot.core.kodi_api.CFG")
@patch("subprocess.run")
def test_write_subtitle_quotes_paths_with_spaces(mock_run, mock_cfg):
    mock_cfg.cec_host = "192.168.1.100"
    mock_run.return_value = MagicMock(returncode=0, stdout=b"ok\n", stderr=b"")
    path = kodi_library.write_subtitle_via_ssh(
        "/storage/videos/The Big Lebowski.mkv", "en", b"data"
    )
    assert path == "/storage/videos/The Big Lebowski.en.srt"
    assert "The Big Lebowski.en.srt" in mock_run.call_args.args[0]


@patch("kodibot.core.kodi_api.CFG")
@patch("subprocess.run")
def test_subtitle_exists_via_ssh(mock_run, mock_cfg):
    mock_cfg.cec_host = "192.168.1.100"
    mock_run.return_value = MagicMock(returncode=0, stdout=b"yes\n", stderr=b"")
    assert kodi_library.subtitle_exists_via_ssh("/storage/videos/x.mkv", "de") is True
    mock_run.return_value = MagicMock(returncode=0, stdout=b"no\n", stderr=b"")
    assert kodi_library.subtitle_exists_via_ssh("/storage/videos/x.mkv", "de") is False


@patch("kodibot.core.kodi_api.CFG")
def test_ssh_prefix_hardens_against_hangs(mock_cfg):
    mock_cfg.cec_host = "192.168.1.100"
    prefix = kodi_library._ssh_prefix()
    assert "BatchMode=yes" in prefix
    assert "ConnectTimeout=5" in prefix


@patch("kodibot.core.kodi_api.CFG")
@patch("subprocess.run")
def test_subtitle_exists_via_ssh_returns_false_on_timeout(mock_run, mock_cfg):
    mock_cfg.cec_host = "192.168.1.100"
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="ssh", timeout=20)
    assert kodi_library.subtitle_exists_via_ssh("/storage/videos/x.mkv", "de") is False


@patch("kodibot.core.kodi_api.CFG")
@patch("subprocess.run")
def test_subtitle_exists_via_ssh_probe_does_not_inherit_stdin(mock_run, mock_cfg):
    mock_cfg.cec_host = "192.168.1.100"
    mock_run.return_value = MagicMock(returncode=0, stdout=b"yes\n", stderr=b"")
    kodi_library.subtitle_exists_via_ssh("/storage/videos/x.mkv", "de")
    assert mock_run.call_args.kwargs["stdin"] == subprocess.DEVNULL
    assert mock_run.call_args.kwargs["timeout"] == 20


@patch("kodibot.core.kodi_api.CFG")
@patch("subprocess.run")
def test_write_subtitle_via_ssh_returns_none_on_timeout(mock_run, mock_cfg):
    mock_cfg.cec_host = "192.168.1.100"
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="ssh", timeout=20)
    assert (
        kodi_library.write_subtitle_via_ssh("/storage/videos/x.mkv", "de", b"data")
        is None
    )


@patch("kodibot.core.kodi_api.CFG")
@patch("subprocess.run")
def test_write_subtitle_via_ssh_treats_existing_file_as_success(mock_run, mock_cfg):
    mock_cfg.cec_host = "192.168.1.100"
    mock_run.return_value = MagicMock(returncode=0, stdout=b"exists\n")
    path = kodi_library.write_subtitle_via_ssh(
        "/storage/videos/Inception.mkv", "de", b"data"
    )
    assert path == "/storage/videos/Inception.de.srt"
    assert mock_run.call_args.kwargs["timeout"] == 20


@patch("kodibot.core.kodi_api.get_active_playerid")
@patch("kodibot.core.kodi_api.kodi_call")
def test_now_playing_media_info_for_a_movie(mock_call, mock_pid):
    mock_pid.return_value = 1
    mock_call.return_value = {
        "result": {
            "item": {
                "type": "movie",
                "file": "/storage/videos/Inception.mkv",
                "uniqueid": {"imdb": "tt1375666"},
            }
        }
    }
    info = kodi_library.now_playing_media_info()
    assert info["file"] == "/storage/videos/Inception.mkv"
    assert info["imdb_id"] == "tt1375666"
    assert info["season"] is None


@patch("kodibot.core.kodi_api.get_active_playerid")
@patch("kodibot.core.kodi_api.kodi_call")
def test_now_playing_media_info_resolves_parent_show(mock_call, mock_pid):
    mock_pid.return_value = 1
    mock_call.side_effect = [
        {
            "result": {
                "item": {
                    "type": "episode",
                    "file": "/storage/tv/BB/S02E05.mkv",
                    "uniqueid": {},
                    "season": 2,
                    "episode": 5,
                    "tvshowid": 7,
                }
            }
        },
        {"result": {"tvshowdetails": {"uniqueid": {"imdb": "tt0903747"}}}},
    ]
    info = kodi_library.now_playing_media_info()
    assert info["parent_imdb_id"] == "tt0903747"
    assert info["season"] == 2
    assert info["episode"] == 5


@patch("kodibot.core.kodi_api.get_active_playerid")
def test_now_playing_media_info_without_player(mock_pid):
    mock_pid.return_value = None
    assert kodi_library.now_playing_media_info() is None
