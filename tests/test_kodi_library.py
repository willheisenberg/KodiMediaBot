import os
os.environ["TG_TOKEN"] = "1"
os.environ["KODI_HOST"] = "1"
os.environ["KODI_USER"] = "1"
os.environ["KODI_PASS"] = "1"
os.environ["KODI_PORT"] = "1"
os.environ["KODI_WS_PORT"] = "1"

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

def test_get_ctimes_empty():
    assert get_ctimes_via_ssh([]) == {}
