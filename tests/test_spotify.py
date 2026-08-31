import os
os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "1")

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import asyncio
import dataclasses
import json

import pytest

import kodibot.core.kodi_api  # noqa: F401  lädt kodi_metadata vor dem Zugriff
from kodibot.core import kodi_metadata as KM
from kodibot.core import queue_state as QS
from kodibot.core import spotify


# ── URL-Erkennung ────────────────────────────────────────────────────


def test_parses_playlist_url():
    assert spotify.parse_spotify_url(
        "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
    ) == ("playlist", "37i9dQZF1DXcBWIGoYBM5M")


def test_parses_album_url():
    assert spotify.parse_spotify_url(
        "https://open.spotify.com/album/1DFixLWuPkv3KT3TnV35m3"
    ) == ("album", "1DFixLWuPkv3KT3TnV35m3")


def test_parses_track_url():
    assert spotify.parse_spotify_url(
        "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"
    ) == ("track", "4cOdK2wGLETKBW3PvgPWqT")


def test_parses_regional_url():
    assert spotify.parse_spotify_url(
        "https://open.spotify.com/intl-de/playlist/37i9dQZF1DXcBWIGoYBM5M"
    ) == ("playlist", "37i9dQZF1DXcBWIGoYBM5M")


def test_strips_share_query_parameter():
    assert spotify.parse_spotify_url(
        "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc123&pt=x"
    ) == ("playlist", "37i9dQZF1DXcBWIGoYBM5M")


def test_finds_url_inside_surrounding_text():
    assert spotify.parse_spotify_url(
        "schau mal https://open.spotify.com/album/1DFixLWuPkv3KT3TnV35m3 gute mucke"
    ) == ("album", "1DFixLWuPkv3KT3TnV35m3")


def test_ignores_artist_url():
    assert spotify.parse_spotify_url(
        "https://open.spotify.com/artist/0OdUWJ0sBjDrqHygGUXeCF"
    ) is None


def test_ignores_short_link():
    assert spotify.parse_spotify_url("https://spotify.link/abc123") is None


def test_ignores_non_spotify_url():
    assert spotify.parse_spotify_url("https://youtu.be/dQw4w9WgXcQ") is None
    assert spotify.parse_spotify_url("kein link") is None
    assert spotify.parse_spotify_url("") is None


# ── Trackabruf über die Embed-Seite ──────────────────────────────────


def embed_html(entity):
    """Build an embed page the way Spotify serves it."""
    payload = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
    return (
        "<html><body>"
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script></body></html>"
    )


def track_list_entity(pairs, kind="playlist"):
    return {
        "type": kind,
        "name": "Testliste",
        "trackList": [{"title": title, "subtitle": artist} for artist, title in pairs],
    }


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


class FakeHTTP:
    def __init__(self, response=None):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def spotify_config(max_tracks=100):
    from kodibot.config import CFG as REAL

    return dataclasses.replace(REAL, spotify_max_tracks=max_tracks, spotify_timeout=10.0)


@pytest.fixture
def http(monkeypatch):
    monkeypatch.setattr(spotify, "CFG", spotify_config())
    fake = FakeHTTP()
    monkeypatch.setattr(spotify, "HTTP", fake)
    return fake


def test_fetches_playlist_tracks(http):
    http.response = FakeResponse(embed_html(track_list_entity([
        ("Daft Punk", "One More Time"),
        ("Justice", "D.A.N.C.E."),
    ])))

    assert spotify.fetch_tracks("playlist", "PL1") == [
        ("Daft Punk", "One More Time"),
        ("Justice", "D.A.N.C.E."),
    ]


def test_requests_the_embed_page(http):
    http.response = FakeResponse(embed_html(track_list_entity([("A", "1")])))

    spotify.fetch_tracks("playlist", "PL1")

    url, kwargs = http.calls[0]
    assert url == "https://open.spotify.com/embed/playlist/PL1"
    # Without a browser user agent Spotify serves a different page.
    assert "Mozilla" in kwargs["headers"]["User-Agent"]


def test_fetches_album_tracks(http):
    http.response = FakeResponse(embed_html(
        track_list_entity([("Daft Punk", "Contact")], kind="album")
    ))

    assert spotify.fetch_tracks("album", "AL1") == [("Daft Punk", "Contact")]


def test_fetches_single_track_from_entity(http):
    """A track embed carries no trackList; artist and title sit on the entity."""
    http.response = FakeResponse(embed_html({
        "type": "track",
        "title": "Veridis Quo",
        "artists": [{"name": "Daft Punk"}],
        "trackList": [],
    }))

    assert spotify.fetch_tracks("track", "TR1") == [("Daft Punk", "Veridis Quo")]


def test_joins_multiple_artists_on_a_single_track(http):
    http.response = FakeResponse(embed_html({
        "type": "track",
        "title": "Get Lucky",
        "artists": [{"name": "Daft Punk"}, {"name": "Pharrell Williams"}],
        "trackList": [],
    }))

    assert spotify.fetch_tracks("track", "TR1") == [
        ("Daft Punk, Pharrell Williams", "Get Lucky")
    ]


def test_stops_at_max_tracks(monkeypatch, http):
    monkeypatch.setattr(spotify, "CFG", spotify_config(max_tracks=2))
    http.response = FakeResponse(embed_html(track_list_entity([
        ("A", "1"), ("B", "2"), ("C", "3"),
    ])))

    assert spotify.fetch_tracks("playlist", "PL1") == [("A", "1"), ("B", "2")]


def test_skips_entries_without_artist_or_title(http):
    http.response = FakeResponse(embed_html({
        "type": "playlist",
        "trackList": [
            {"title": "", "subtitle": "Ohne Titel"},
            {"title": "Ohne Artist", "subtitle": ""},
            {"title": "Da", "subtitle": "Wer"},
            None,
        ],
    }))

    assert spotify.fetch_tracks("playlist", "PL1") == [("Wer", "Da")]


def test_returns_empty_list_for_empty_playlist(http):
    http.response = FakeResponse(embed_html(track_list_entity([])))
    assert spotify.fetch_tracks("playlist", "PL1") == []


def test_raises_unavailable_on_not_found(http):
    http.response = FakeResponse("<html>nope</html>", status_code=404)

    with pytest.raises(spotify.SpotifyUnavailable):
        spotify.fetch_tracks("playlist", "PL1")


def test_raises_unavailable_when_markup_changed(http):
    """Spotify can change the embed page; that must not look like an empty playlist."""
    http.response = FakeResponse("<html><body>kein next data</body></html>")

    with pytest.raises(spotify.SpotifyUnavailable):
        spotify.fetch_tracks("playlist", "PL1")


def test_raises_unavailable_on_malformed_json(http):
    http.response = FakeResponse(
        '<script id="__NEXT_DATA__" type="application/json">{kaputt</script>'
    )

    with pytest.raises(spotify.SpotifyUnavailable):
        spotify.fetch_tracks("playlist", "PL1")


def test_raises_unavailable_on_network_error(monkeypatch, http):
    def boom(*args, **kwargs):
        raise OSError("kein netz")

    monkeypatch.setattr(http, "get", boom)

    with pytest.raises(spotify.SpotifyUnavailable):
        spotify.fetch_tracks("playlist", "PL1")


def test_rejects_unsupported_kind(http):
    http.response = FakeResponse(embed_html(track_list_entity([("A", "1")])))

    with pytest.raises(spotify.SpotifyUnavailable):
        spotify.fetch_tracks("artist", "AR1")


def test_embed_track_limit_is_documented():
    """The embed page caps long playlists; the dispatch reports that."""
    assert spotify.EMBED_TRACK_LIMIT == 100


# ── YouTube-Auflösung ────────────────────────────────────────────────


def test_resolves_track_to_video_id(monkeypatch):
    seen = {}

    def fake_search(query, expected_title="", timeout=None):
        seen["query"] = query
        seen["expected_title"] = expected_title
        return "https://youtu.be/dQw4w9WgXcQ"

    monkeypatch.setattr(KM, "search_youtube_link", fake_search)

    assert KM.spotify_track_to_youtube_id("Daft Punk", "One More Time") == "dQw4w9WgXcQ"
    assert seen["query"] == "Daft Punk One More Time"
    assert seen["expected_title"] == "Daft Punk - One More Time"


def test_checks_only_primary_artist(monkeypatch):
    """YouTube nennt Feature-Artists anders als Spotify, sonst greift die Prüfung nie."""
    seen = {}

    def fake_search(query, expected_title="", timeout=None):
        seen["query"] = query
        seen["expected_title"] = expected_title
        return "https://youtu.be/dQw4w9WgXcQ"

    monkeypatch.setattr(KM, "search_youtube_link", fake_search)

    KM.spotify_track_to_youtube_id("Daft Punk, Pharrell Williams", "Get Lucky")

    assert seen["query"] == "Daft Punk, Pharrell Williams Get Lucky"
    assert seen["expected_title"] == "Daft Punk - Get Lucky"


def test_returns_empty_string_without_match(monkeypatch):
    monkeypatch.setattr(KM, "search_youtube_link", lambda q, expected_title="", timeout=None: "")
    assert KM.spotify_track_to_youtube_id("Wer", "Was") == ""


def test_returns_empty_string_for_incomplete_track(monkeypatch):
    monkeypatch.setattr(KM, "search_youtube_link", lambda q, expected_title="", timeout=None: "x")
    assert KM.spotify_track_to_youtube_id("", "Was") == ""
    assert KM.spotify_track_to_youtube_id("Wer", "") == ""


def test_match_check_accepts_real_youtube_titles():
    """Die für ICY-Strings gebaute Prüfung muss auch mit Spotify-Feldern urteilen."""
    assert KM.youtube_result_matches_radio_track(
        "Daft Punk - Get Lucky",
        "Daft Punk - Get Lucky (Official Video) ft. Pharrell Williams",
    )
    assert KM.youtube_result_matches_radio_track(
        "Fleetwood Mac - Dreams",
        "Fleetwood Mac - Dreams [Remastered]",
    )


def test_match_check_rejects_wrong_track():
    assert not KM.youtube_result_matches_radio_track(
        "Daft Punk - Get Lucky",
        "Bee Gees - Stayin' Alive",
    )


# ── Einreihen in die Warteschlange ───────────────────────────────────


@pytest.fixture
def empty_queue(monkeypatch):
    monkeypatch.setattr(QS, "QUEUE", [])
    monkeypatch.setattr(QS, "mark_list_dirty", lambda: None)
    return QS.QUEUE


def test_queues_resolved_tracks_in_playlist_order(monkeypatch, empty_queue):
    ids = {
        ("Daft Punk", "One More Time"): "aaaaaaaaaaa",
        ("Justice", "D.A.N.C.E."): "bbbbbbbbbbb",
        ("Air", "La Femme d'Argent"): "ccccccccccc",
    }
    monkeypatch.setattr(
        QS.kodi_api, "spotify_track_to_youtube_id", lambda a, t: ids[(a, t)]
    )

    added, total = asyncio.run(QS.queue_spotify_async(list(ids.keys())))

    assert (added, total) == (3, 3)
    assert [item["url"] for item in empty_queue] == [
        "plugin://plugin.video.youtube/play/?video_id=aaaaaaaaaaa",
        "plugin://plugin.video.youtube/play/?video_id=bbbbbbbbbbb",
        "plugin://plugin.video.youtube/play/?video_id=ccccccccccc",
    ]


def test_uses_spotify_title_for_display(monkeypatch, empty_queue):
    monkeypatch.setattr(
        QS.kodi_api, "spotify_track_to_youtube_id", lambda a, t: "aaaaaaaaaaa"
    )

    asyncio.run(QS.queue_spotify_async([("Daft Punk", "One More Time")]))

    assert empty_queue[0]["title"] == "Daft Punk - One More Time"


def test_skips_tracks_without_match(monkeypatch, empty_queue):
    monkeypatch.setattr(
        QS.kodi_api,
        "spotify_track_to_youtube_id",
        lambda a, t: "aaaaaaaaaaa" if t == "Treffer" else "",
    )

    added, total = asyncio.run(
        QS.queue_spotify_async([("A", "Treffer"), ("B", "Fehlschlag"), ("C", "Treffer")])
    )

    assert (added, total) == (2, 3)
    assert len(empty_queue) == 2


def test_survives_failing_search(monkeypatch, empty_queue):
    def flaky(artist, title):
        if title == "Bombe":
            raise RuntimeError("yt-dlp kaputt")
        return "aaaaaaaaaaa"

    monkeypatch.setattr(QS.kodi_api, "spotify_track_to_youtube_id", flaky)

    added, total = asyncio.run(QS.queue_spotify_async([("A", "Bombe"), ("B", "Geht")]))

    assert (added, total) == (1, 2)


def test_handles_empty_track_list(empty_queue):
    assert asyncio.run(QS.queue_spotify_async([])) == (0, 0)
    assert empty_queue == []


# ── Timeout der YouTube-Suche ────────────────────────────────────────


def test_search_uses_caller_timeout_when_given(monkeypatch):
    """Spotify needs a longer timeout than radio; the caller decides."""
    seen = {}

    class FakeCompleted:
        returncode = 0
        stdout = "dQw4w9WgXcQ\tWer - Was"

    def fake_run(cmd, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return FakeCompleted()

    monkeypatch.setattr(KM.subprocess, "run", fake_run)
    monkeypatch.setattr(KM.KA, "YT_SEARCH_CACHE", {})

    KM.search_youtube_link("Wer Was", expected_title="Wer - Was", timeout=25.0)

    assert seen["timeout"] == 25.0


def test_search_falls_back_to_radio_timeout(monkeypatch):
    seen = {}

    class FakeCompleted:
        returncode = 0
        stdout = "dQw4w9WgXcQ\tWer - Was"

    def fake_run(cmd, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return FakeCompleted()

    monkeypatch.setattr(KM.subprocess, "run", fake_run)
    monkeypatch.setattr(KM.KA, "YT_SEARCH_CACHE", {})

    KM.search_youtube_link("Wer Was", expected_title="Wer - Was")

    assert seen["timeout"] == KM.KA.CFG.yt_search_timeout


def test_spotify_resolution_uses_the_spotify_timeout(monkeypatch):
    """The 8s radio timeout starves parallel yt-dlp searches on slow hardware."""
    seen = {}

    def fake_search(query, expected_title="", timeout=None):
        seen["timeout"] = timeout
        return "https://youtu.be/dQw4w9WgXcQ"

    monkeypatch.setattr(KM, "search_youtube_link", fake_search)

    KM.spotify_track_to_youtube_id("Daft Punk", "One More Time")

    from kodibot.config import CFG
    assert seen["timeout"] == CFG.spotify_yt_timeout
    assert CFG.spotify_yt_timeout > CFG.yt_search_timeout


# ── Trefferprüfung: Schreibweisen von Künstlernamen ──────────────────


def test_match_check_treats_ampersand_and_and_alike():
    """Spotify writes 'and', YouTube writes '&' — same artist either way."""
    assert KM.youtube_result_matches_radio_track(
        "Seals and Crofts - Summer Breeze",
        "Seals & Crofts - Summer Breeze (Official Audio)",
    )
    assert KM.youtube_result_matches_radio_track(
        "Seals & Crofts - Summer Breeze",
        "Seals and Crofts - Summer Breeze",
    )


def test_match_check_ignores_artist_order():
    """Collaborations are credited in either order; the track is the same."""
    assert KM.youtube_result_matches_radio_track(
        "Mychael Danna & Jeff Danna - The Blood Of Cu Chulainn",
        "The Blood of Cu Chulainn (Official Music Video) | Jeff Danna & Mychael Danna",
    )


def test_match_check_still_rejects_a_different_artist():
    """Looser artist matching must not let a cover or tribute act through."""
    assert not KM.youtube_result_matches_radio_track(
        "Avicii Cover Band - Levels",
        "SYNTHONY - Avicii 'Levels' (Live at The Auckland Domain)",
    )


def test_match_check_still_rejects_a_different_title():
    assert not KM.youtube_result_matches_radio_track(
        "DJ Collins - DJ Collins Rock Mixology Classic Covers 1",
        "DJ Collins Pop Giants Covers 1",
    )
