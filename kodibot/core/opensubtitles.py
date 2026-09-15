"""OpenSubtitles REST client.

Fetches subtitles for the running Kodi item when a wanted language has no
track yet.  Deliberately free of Telegram imports so it stays testable on
its own.
"""

import codecs
import logging
import threading
import time

import requests

from kodibot.config import CFG

log = logging.getLogger(__name__)

# Kodi reports ISO 639-2 for embedded tracks and 639-1 for external files.
_LANG_ALIASES = {
    "ger": "de",
    "deu": "de",
    "de": "de",
    "eng": "en",
    "en": "en",
}

API_BASE = "https://api.opensubtitles.com/api/v1"
USER_AGENT = "KodiMediaBot v1.0"
TIMEOUT = 15.0
# The API issues 24h tokens; renew a little early.
TOKEN_TTL = 23 * 3600
# A downloaded subtitle becomes a permanent file next to the movie — refuse
# anything that does not look like a real payload.
MAX_SUBTITLE_BYTES = 2 * 1024 * 1024

_SESSION_LOCK = threading.Lock()
_TOKEN = None
_TOKEN_TS = 0.0
_BASE_URL = API_BASE


class OpenSubtitlesError(Exception):
    """The API could not serve a request.

    ``message`` is a short machine-readable reason; ``reset_time`` carries the
    quota reset timestamp when the daily download limit was hit.
    """

    def __init__(self, message, reset_time=None):
        super().__init__(message)
        self.message = message
        self.reset_time = reset_time


def _headers(with_token=False):
    headers = {
        "Api-Key": CFG.opensubtitles_api_key,
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if with_token and _TOKEN:
        headers["Authorization"] = f"Bearer {_TOKEN}"
    return headers


def reset_session():
    """Drop the cached token, forcing the next call to log in again.

    Must never be called while the caller already holds ``_SESSION_LOCK``
    (e.g. from within ``_login()``) — that would deadlock.
    """
    global _TOKEN, _TOKEN_TS, _BASE_URL
    with _SESSION_LOCK:
        _TOKEN = None
        _TOKEN_TS = 0.0
        _BASE_URL = API_BASE


def _login():
    global _TOKEN, _TOKEN_TS, _BASE_URL
    try:
        res = requests.post(
            f"{API_BASE}/login",
            json={
                "username": CFG.opensubtitles_user,
                "password": CFG.opensubtitles_pass,
            },
            headers=_headers(),
            timeout=TIMEOUT,
        )
    except Exception as e:
        log.warning("OpenSubtitles login error: %s", e)
        return False
    if res.status_code != 200:
        log.warning("OpenSubtitles login failed: status=%s", res.status_code)
        return False
    data = res.json() or {}
    token = data.get("token")
    if not token:
        log.warning("OpenSubtitles login returned no token")
        return False
    _TOKEN = token
    _TOKEN_TS = time.time()
    # VIP accounts are pointed at a different host by the login response.
    base = (data.get("base_url") or "").strip()
    _BASE_URL = f"https://{base}/api/v1" if base else API_BASE
    return True


def ensure_token():
    """Log in unless a valid token is already cached."""
    with _SESSION_LOCK:
        if _TOKEN and (time.time() - _TOKEN_TS) < TOKEN_TTL:
            return True
        return _login()


def normalize_language(value):
    """Reduce a Kodi or OpenSubtitles language tag to a two-letter code."""
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    return _LANG_ALIASES.get(raw, raw[:2])


def is_forced_track(stream):
    """True when a Kodi subtitle stream only covers foreign-language parts.

    Kodi 20 and up report ``isforced``; older versions leave us with the
    track label, which carries "Forced" by convention.
    """
    flag = (stream or {}).get("isforced")
    if flag is not None:
        return bool(flag)
    return "forced" in ((stream or {}).get("name") or "").lower()


def subtitle_suffix(language, forced=False):
    """Filename part for one track: ``de``, or ``de.forced`` for forced ones.

    Kodi reads the ``.forced`` marker out of external subtitle filenames, and
    it keeps the two variants of a language from overwriting each other.
    """
    return f"{language}.forced" if forced else language


def missing_tracks(av_state, wanted=None):
    """Return the (language, forced) pairs that have no subtitle track yet.

    Both variants are wanted per language: a full track already on the item
    says nothing about the forced one, and vice versa.
    """
    targets = list(wanted if wanted is not None else CFG.opensubtitles_language_list)
    present = set()
    for stream in (av_state or {}).get("subtitles") or []:
        present.add((normalize_language(stream.get("language")), is_forced_track(stream)))
    return [
        (lang, forced)
        for lang in targets
        for forced in (False, True)
        if (normalize_language(lang), forced) not in present
    ]


def strip_tt(imdb_id):
    """OpenSubtitles wants the numeric id — Kodi delivers ``tt1375666``."""
    raw = str(imdb_id or "").strip().lower()
    if raw.startswith("tt"):
        raw = raw[2:]
    return raw.lstrip("0")


def search(imdb_id, language, season=None, episode=None, parent_imdb_id=None, forced=False):
    """Return the most-downloaded file_id for one language, or None.

    ``forced`` picks the variant: forced tracks only translate foreign-language
    passages.  Both directions are stated explicitly -- the API default mixes
    the two, which would let a forced track land on disk as the full subtitle.
    """
    params = {
        "languages": language,
        "order_by": "download_count",
        "order_direction": "desc",
        "foreign_parts_only": "only" if forced else "exclude",
    }
    own = strip_tt(imdb_id)
    if season is not None and episode is not None:
        params["type"] = "episode"
        params["season_number"] = int(season)
        params["episode_number"] = int(episode)
        parent = strip_tt(parent_imdb_id)
        if own:
            params["imdb_id"] = own
        elif parent:
            params["parent_imdb_id"] = parent
        else:
            return None
    else:
        if not own:
            return None
        params["type"] = "movie"
        params["imdb_id"] = own

    # Makes sure _BASE_URL reflects a VIP host before the first request of a
    # session; harmless (and cheap) once a token is already cached.
    ensure_token()
    try:
        res = requests.get(
            f"{_BASE_URL}/subtitles",
            params=params,
            headers=_headers(),
            timeout=TIMEOUT,
        )
    except Exception as e:
        log.warning("OpenSubtitles search error: %s", e)
        return None
    if res.status_code != 200:
        log.warning(
            "OpenSubtitles search failed: status=%s lang=%s", res.status_code, language
        )
        return None
    for entry in (res.json() or {}).get("data") or []:
        for entry_file in ((entry.get("attributes") or {}).get("files") or []):
            file_id = entry_file.get("file_id")
            if file_id:
                return file_id
    return None


def _post_download(file_id):
    try:
        return requests.post(
            f"{_BASE_URL}/download",
            json={"file_id": file_id},
            headers=_headers(with_token=True),
            timeout=TIMEOUT,
        )
    except Exception as e:
        log.warning("OpenSubtitles download request error: %s", e)
        raise OpenSubtitlesError("request_failed") from e


def _looks_like_subtitle(content):
    """Cheap sanity check: does this start like SRT or WebVTT?"""
    # A leading UTF-8 BOM is common in OpenSubtitles downloads and explicitly
    # allowed by the WebVTT spec — strip it before sniffing the content so we
    # don't reject valid subtitles and burn the daily download quota on them.
    stripped = (content or b"").lstrip().lstrip(codecs.BOM_UTF8).lstrip()
    if stripped.startswith(b"WEBVTT"):
        return True
    first_line = stripped.split(b"\n", 1)[0].strip()
    # SRT starts with a bare cue sequence number.
    return first_line.isdigit()


def download(file_id):
    """Resolve a file_id to subtitle bytes.

    Raises OpenSubtitlesError when the API refuses — the caller turns that
    into a chat message and shows the regular list anyway.
    """
    if not ensure_token():
        raise OpenSubtitlesError("login_failed")
    res = _post_download(file_id)
    if res.status_code == 401:
        # The token expired earlier than its nominal lifetime.
        reset_session()
        if not ensure_token():
            raise OpenSubtitlesError("login_failed")
        res = _post_download(file_id)
    if res.status_code == 406:
        data = res.json() or {}
        raise OpenSubtitlesError("quota_exceeded", reset_time=data.get("reset_time"))
    if res.status_code != 200:
        raise OpenSubtitlesError(f"download_failed_{res.status_code}")
    data = res.json() or {}
    link = data.get("link")
    if not link:
        raise OpenSubtitlesError("no_link")
    log.info(
        "OpenSubtitles download file_id=%s remaining=%s",
        file_id,
        data.get("remaining"),
    )
    try:
        file_res = requests.get(link, timeout=TIMEOUT)
    except Exception as e:
        log.warning("OpenSubtitles fetch error: %s", e)
        raise OpenSubtitlesError("request_failed") from e
    if file_res.status_code != 200:
        raise OpenSubtitlesError(f"fetch_failed_{file_res.status_code}")
    content = file_res.content
    if len(content) > MAX_SUBTITLE_BYTES:
        raise OpenSubtitlesError("content_too_large")
    if not _looks_like_subtitle(content):
        raise OpenSubtitlesError("invalid_content")
    return content


def fetch_for(imdb_id, language, season=None, episode=None, parent_imdb_id=None, forced=False):
    """Search and download one track. Returns bytes, or None without a hit."""
    if not CFG.opensubtitles_enabled:
        return None
    file_id = search(imdb_id, language, season, episode, parent_imdb_id, forced=forced)
    if not file_id:
        return None
    return download(file_id)
