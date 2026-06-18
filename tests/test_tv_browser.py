import pytest
from unittest.mock import MagicMock, patch
from kodibot.core import tv_browser

MOCK_M3U = """#EXTM3U
#EXTINF:-1 tvg-logo="http://example.com/logo1.png",Das Erste
http://stream.example.com/daserste/master.m3u8
#EXTINF:-1 tvg-logo="http://example.com/logo2.png",ZDF HD
http://stream.example.com/zdf/mono.m3u8
"""

@pytest.fixture(autouse=True)
def mock_cfg():
    with patch("kodibot.core.tv_browser.CFG") as mock:
        mock.iptv_m3u_url = "http://mock-playlist.m3u"
        yield mock

@patch("requests.get")
def test_search_tv_channels(mock_get):
    mock_resp = MagicMock()
    mock_resp.text = MOCK_M3U
    mock_get.return_value = mock_resp

    results = tv_browser.search_tv_channels("Erste")
    assert len(results) == 1
    assert results[0]["name"] == "Das Erste"
    assert results[0]["url"] == "http://stream.example.com/daserste/master.m3u8"
    assert results[0]["logo"] == "http://example.com/logo1.png"

@patch("requests.get")
def test_get_tv_channel_info_by_url(mock_get):
    mock_resp = MagicMock()
    mock_resp.text = MOCK_M3U
    mock_get.return_value = mock_resp

    # Test exact match
    info = tv_browser.get_tv_channel_info_by_url("http://stream.example.com/zdf/mono.m3u8")
    assert info is not None
    assert info["name"] == "ZDF HD"
    assert info["logo"] == "http://example.com/logo2.png"

    # Test match with Kodi suffix (e.g. pipe with headers)
    info_with_suffix = tv_browser.get_tv_channel_info_by_url("http://stream.example.com/zdf/mono.m3u8|User-Agent=Kodi/21.0")
    assert info_with_suffix is not None
    assert info_with_suffix["name"] == "ZDF HD"

    # Test mismatch
    info_none = tv_browser.get_tv_channel_info_by_url("http://stream.example.com/unknown.m3u8")
    assert info_none is None
