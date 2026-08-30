"""Spotify track lists, read from the public embed page.

Resolves public Spotify playlists, albums and tracks into ``(artist, title)``
pairs.  Deliberately free of any Kodi or Telegram knowledge: callers feed the
pairs into the YouTube search and the queue themselves.

Spotify's Web API is not used: since February 2026 registering a developer app
requires a Premium subscription.  The embed page — the one Spotify serves for
embedding players into third-party sites — needs no account and no credentials,
and it also returns playlists curated by Spotify itself, which the Web API
refuses.  Its one limit is EMBED_TRACK_LIMIT tracks per link.

The page ships its data as JSON inside a ``__NEXT_DATA__`` script tag.  That
structure is undocumented, so every read is defensive: anything unexpected
raises SpotifyUnavailable rather than silently yielding an empty playlist.
"""

import json
import logging
import re

import requests

from kodibot.config import CFG

log = logging.getLogger(__name__)

SPOTIFY_URL_RE = re.compile(
    r"https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?(playlist|album|track)/([A-Za-z0-9]+)"
)

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)

EMBED_BASE = "https://open.spotify.com/embed"

# The embed page stops after this many tracks, however long the playlist is.
EMBED_TRACK_LIMIT = 100

# Spotify serves a different page to clients that do not look like a browser.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

SUPPORTED_KINDS = ("playlist", "album", "track")

HTTP = requests.Session()


class SpotifyUnavailable(Exception):
    """The link could not be read: private, deleted, or the page changed."""


# Extract the kind and id from a Spotify link, or None when there is none.
def parse_spotify_url(text):
    if not text:
        return None
    m = SPOTIFY_URL_RE.search(text)
    if not m:
        return None
    return m.group(1), m.group(2)


# Download one embed page and return the entity object it carries.
def _fetch_entity(kind, sid):
    url = f"{EMBED_BASE}/{kind}/{sid}"
    try:
        res = HTTP.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=CFG.spotify_timeout,
        )
    except Exception as e:
        raise SpotifyUnavailable(f"request failed: {e}") from e
    if res.status_code != 200:
        raise SpotifyUnavailable(f"status {res.status_code} for {url}")
    m = NEXT_DATA_RE.search(res.text or "")
    if not m:
        raise SpotifyUnavailable("embed page carries no __NEXT_DATA__")
    try:
        payload = json.loads(m.group(1))
        entity = payload["props"]["pageProps"]["state"]["data"]["entity"]
    except Exception as e:
        raise SpotifyUnavailable(f"unexpected embed structure: {e}") from e
    if not isinstance(entity, dict):
        raise SpotifyUnavailable("embed entity is not an object")
    return entity


# Join an artists array into one display string.
def _artist_names(artists):
    if not isinstance(artists, list):
        return ""
    names = [
        (a.get("name") or "").strip()
        for a in artists
        if isinstance(a, dict)
    ]
    return ", ".join(n for n in names if n)


# Turn one trackList entry into an (artist, title) pair, or None if unusable.
def _list_entry_pair(entry):
    if not isinstance(entry, dict):
        return None
    title = (entry.get("title") or "").strip()
    artist = (entry.get("subtitle") or "").strip()
    if not title or not artist:
        return None
    return artist, title


# Resolve a Spotify link into (artist, title) pairs in playlist order.
def fetch_tracks(kind, sid):
    if kind not in SUPPORTED_KINDS:
        raise SpotifyUnavailable(f"unsupported link kind {kind!r}")
    entity = _fetch_entity(kind, sid)

    # A track embed has no trackList; artist and title sit on the entity.
    if kind == "track":
        title = (entity.get("title") or "").strip()
        artist = _artist_names(entity.get("artists"))
        return [(artist, title)] if title and artist else []

    tracks = []
    for entry in entity.get("trackList") or []:
        pair = _list_entry_pair(entry)
        if pair:
            tracks.append(pair)
        if len(tracks) >= CFG.spotify_max_tracks:
            break
    return tracks
