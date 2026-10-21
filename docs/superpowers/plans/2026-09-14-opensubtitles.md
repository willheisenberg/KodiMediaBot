# Untertitel aus OpenSubtitles nachladen — Implementierungsplan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fehlen dem laufenden Film deutsche oder englische Untertitel, lädt der Bot sie von OpenSubtitles, legt die `.srt` neben die Videodatei und hängt sie an die laufende Wiedergabe — ohne eine Spur zu aktivieren.

**Architecture:** Ein neues, telegram-freies Modul `kodibot/core/opensubtitles.py` kapselt den REST-Client (Login-Token, Suche, Download). Das Schreiben auf den LibreELEC-Host läuft über SSH nach dem Muster von `get_ctimes_via_ssh` in `kodibot/core/kodi_library.py`. `kodibot/telegram/ui_callbacks.py` verdrahtet beides im bestehenden Zweig `action == "subtitles"` als vorgelagerten, jederzeit überspringbaren Schritt.

**Tech Stack:** Python 3.12-kompatibel, `requests` (bereits Dependency), `subprocess` + SSH, Kodi JSON-RPC, pytest mit `unittest.mock`.

**Spec:** `docs/superpowers/specs/2026-09-14-opensubtitles-design.md`

## Global Constraints

- **Nicht committen.** Weder am Ende eines Tasks noch am Ende des Plans. Änderungen bleiben im Arbeitsverzeichnis liegen; der Stand wird berichtet. Das gilt ausdrücklich auch für Subagenten. (Anweisung des Maintainers, überstimmt die Commit-Schritte der Skill-Vorlage.)
- **Nicht deployen.** Kein `scp`, kein `ssh` zum LibreELEC-Host, kein Container-Neustart, kein Aufruf von `deploy_libreelec_partyqueue.sh`. Der Maintainer deployt selbst.
- **Python 3.12-kompatibel.** Das lokale venv fährt 3.14, das Image ist `python:3.12-alpine`. Syntax ab 3.13 bricht in Produktion.
- **Keine neuen Dependencies.** `requests` ist über die `pip install`-Zeile im `Dockerfile` vorhanden und reicht aus.
- **Config nur über `CFG`.** `os.environ` ausschließlich in `kodibot/config.py`.
- **Telegram-Aufrufe nur über die Wrapper** aus `kodibot/telegram/rate.py` (`telegram_request`, `send_and_track`, `delete_message_if_present`). Nie `ctx.bot.<method>` direkt awaiten.
- **Jede Testdatei setzt ihr eigenes Env-Preamble** vor dem ersten `kodibot`-Import. Ein `kodibot`-Import oberhalb des Preambles bricht die Datei.
- **Neue Env-Vars an vier Stellen** plus README: `Config`-Dataclass, `Config.from_env()`, `docker-compose.local-bot-api.yml`, `.env.local-bot-api.example`.
- **Kein Modul in `kodibot/core/` importiert aus `kodibot/telegram/`.**
- Die volle Suite (`.venv/bin/python -m pytest -q`) muss am Ende grün sein.

---

### Task 1: Konfiguration

**Files:**
- Modify: `kodibot/config.py` (Dataclass-Felder, Properties, `from_env()`)
- Modify: `docker-compose.local-bot-api.yml:26-73` (environment-Block des Dienstes `kodi-media-bot`)
- Modify: `.env.local-bot-api.example`
- Modify: `README.md` (Abschnitt *Explanations*)
- Test: `tests/test_opensubtitles.py` (neu)

**Interfaces:**
- Consumes: nichts
- Produces: `CFG.opensubtitles_api_key`, `CFG.opensubtitles_user`, `CFG.opensubtitles_pass`, `CFG.opensubtitles_languages` (alle `str`), `CFG.opensubtitles_enabled` (`bool`, Property), `CFG.opensubtitles_language_list` (`list[str]`, Property)

- [ ] **Step 1: Write the failing test**

Neue Datei `tests/test_opensubtitles.py`. Das Env-Preamble steht zwingend vor jedem `kodibot`-Import:

```python
import os

os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "1")

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from kodibot.config import Config


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_opensubtitles.py -v`
Expected: FAIL mit `AttributeError: 'Config' object has no attribute 'opensubtitles_enabled'`

- [ ] **Step 3: Write minimal implementation**

In `kodibot/config.py`, in der `Config`-Dataclass hinter dem Block `# ── IPTV / TV configuration ──`:

```python
    # ── OpenSubtitles ─────────────────────────────────────────────────
    opensubtitles_api_key: str
    opensubtitles_user: str
    opensubtitles_pass: str
    opensubtitles_languages: str
```

Bei den `# ── Derived helpers ──`-Properties ergänzen:

```python
    @property
    def opensubtitles_enabled(self) -> bool:
        return bool(
            self.opensubtitles_api_key
            and self.opensubtitles_user
            and self.opensubtitles_pass
        )

    @property
    def opensubtitles_language_list(self) -> list[str]:
        return [
            part.strip().lower()
            for part in self.opensubtitles_languages.split(",")
            if part.strip()
        ]
```

In `Config.from_env()` hinter dem `# IPTV`-Block, vor der schließenden Klammer:

```python
            # OpenSubtitles
            opensubtitles_api_key=(os.environ.get("OPENSUBTITLES_API_KEY") or "").strip(),
            opensubtitles_user=(os.environ.get("OPENSUBTITLES_USER") or "").strip(),
            opensubtitles_pass=(os.environ.get("OPENSUBTITLES_PASS") or "").strip(),
            opensubtitles_languages=(os.environ.get("OPENSUBTITLES_LANGUAGES") or "de,en").strip(),
```

In `docker-compose.local-bot-api.yml` in den `environment`-Block von `kodi-media-bot`, hinter `RADIO_API_URL`:

```yaml
      OPENSUBTITLES_API_KEY: "${OPENSUBTITLES_API_KEY}"
      OPENSUBTITLES_USER: "${OPENSUBTITLES_USER}"
      OPENSUBTITLES_PASS: "${OPENSUBTITLES_PASS}"
      OPENSUBTITLES_LANGUAGES: "${OPENSUBTITLES_LANGUAGES:-de,en}"
```

In `.env.local-bot-api.example` ans Ende:

```
# OpenSubtitles (optional) — ohne diese Werte ist das Nachladen von
# Untertiteln still deaktiviert. API-Key aus dem Profil auf opensubtitles.com.
OPENSUBTITLES_API_KEY=
OPENSUBTITLES_USER=
OPENSUBTITLES_PASS=
OPENSUBTITLES_LANGUAGES=de,en
```

In `README.md` im Abschnitt *Explanations* denselben Text in der dort üblichen Form ergänzen: Alle vier Variablen sind optional; fehlt einer der drei Zugangswerte, verhält sich der Bot exakt wie bisher.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_opensubtitles.py -v`
Expected: 3 passed

- [ ] **Step 5: Lint und Stand berichten**

Run: `.venv/bin/ruff check .`
Expected: keine neuen Befunde.

**Nicht committen.** Änderungen liegen lassen und den Stand berichten.

---

### Task 2: Sprachabgleich

**Files:**
- Create: `kodibot/core/opensubtitles.py`
- Test: `tests/test_opensubtitles.py` (erweitern)

**Interfaces:**
- Consumes: `CFG.opensubtitles_language_list` aus Task 1
- Produces: `normalize_language(value: str) -> str`, `missing_languages(av_state: dict, wanted: list[str] | None = None) -> list[str]`

- [ ] **Step 1: Write the failing test**

An `tests/test_opensubtitles.py` anhängen (der Import gehört zu den übrigen Imports oben in der Datei):

```python
from kodibot.core import opensubtitles


def test_normalize_language_maps_iso_639_2():
    assert opensubtitles.normalize_language("ger") == "de"
    assert opensubtitles.normalize_language("deu") == "de"
    assert opensubtitles.normalize_language("DE") == "de"
    assert opensubtitles.normalize_language("eng") == "en"
    assert opensubtitles.normalize_language("") == ""


def test_missing_languages_reports_nothing_when_both_present():
    av_state = {"subtitles": [{"language": "ger"}, {"language": "eng"}]}
    assert opensubtitles.missing_languages(av_state, ["de", "en"]) == []


def test_missing_languages_reports_only_the_missing_one():
    av_state = {"subtitles": [{"language": "deu"}]}
    assert opensubtitles.missing_languages(av_state, ["de", "en"]) == ["en"]


def test_missing_languages_ignores_unrelated_tracks():
    av_state = {"subtitles": [{"language": "fre"}, {"language": "spa"}]}
    assert opensubtitles.missing_languages(av_state, ["de", "en"]) == ["de", "en"]


def test_missing_languages_handles_empty_state():
    assert opensubtitles.missing_languages({}, ["de", "en"]) == ["de", "en"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_opensubtitles.py -v`
Expected: FAIL mit `ModuleNotFoundError: No module named 'kodibot.core.opensubtitles'`

- [ ] **Step 3: Write minimal implementation**

Neue Datei `kodibot/core/opensubtitles.py`:

```python
"""OpenSubtitles REST client.

Fetches subtitles for the running Kodi item when a wanted language has no
track yet.  Deliberately free of Telegram imports so it stays testable on
its own.
"""

import logging

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


def normalize_language(value):
    """Reduce a Kodi or OpenSubtitles language tag to a two-letter code."""
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    return _LANG_ALIASES.get(raw, raw[:2])


def missing_languages(av_state, wanted=None):
    """Return the wanted languages that have no subtitle track yet."""
    targets = list(wanted if wanted is not None else CFG.opensubtitles_language_list)
    present = {
        normalize_language(stream.get("language"))
        for stream in ((av_state or {}).get("subtitles") or [])
    }
    return [lang for lang in targets if normalize_language(lang) not in present]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_opensubtitles.py -v`
Expected: 8 passed

- [ ] **Step 5: Lint und Stand berichten**

Run: `.venv/bin/ruff check kodibot/core/opensubtitles.py tests/test_opensubtitles.py`

**Nicht committen.**

---

### Task 3: Login und Token-Verwaltung

**Files:**
- Modify: `kodibot/core/opensubtitles.py`
- Test: `tests/test_opensubtitles.py` (erweitern)

**Interfaces:**
- Consumes: `CFG.opensubtitles_api_key`, `CFG.opensubtitles_user`, `CFG.opensubtitles_pass` aus Task 1
- Produces: `OpenSubtitlesError(message, reset_time=None)` mit den Attributen `message` und `reset_time`, `ensure_token() -> bool`, `reset_session() -> None`, `_headers(with_token: bool = False) -> dict`, Modul-Konstanten `API_BASE`, `USER_AGENT`, `TIMEOUT`

- [ ] **Step 1: Write the failing test**

An `tests/test_opensubtitles.py` anhängen. `unittest.mock` wird oben zu den Imports ergänzt (`from unittest.mock import MagicMock, patch`):

```python
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
```

**Wichtig für alle folgenden Tests:** `Config` ist mit `frozen=True` deklariert —
`monkeypatch.setattr(CFG, ...)` wirft `FrozenInstanceError`. Konfiguration wird in
Tests deshalb ausschließlich über `patch("<modul>.CFG")` mit einem `MagicMock`
ersetzt, nie durch Setzen von Attributen auf der echten Instanz.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_opensubtitles.py -k token -v`
Expected: FAIL mit `AttributeError: module 'kodibot.core.opensubtitles' has no attribute 'reset_session'`

- [ ] **Step 3: Write minimal implementation**

In `kodibot/core/opensubtitles.py` die Imports ergänzen und den Abschnitt hinter `_LANG_ALIASES` einfügen:

```python
import threading
import time

import requests
```

```python
API_BASE = "https://api.opensubtitles.com/api/v1"
USER_AGENT = "KodiMediaBot v1.0"
TIMEOUT = 15.0
# The API issues 24h tokens; renew a little early.
TOKEN_TTL = 23 * 3600

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
    """Drop the cached token, forcing the next call to log in again."""
    global _TOKEN, _TOKEN_TS, _BASE_URL
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_opensubtitles.py -v`
Expected: 11 passed

- [ ] **Step 5: Lint und Stand berichten**

Run: `.venv/bin/ruff check kodibot/core/opensubtitles.py tests/test_opensubtitles.py`

**Nicht committen.**

---

### Task 4: Suche und Download

**Files:**
- Modify: `kodibot/core/opensubtitles.py`
- Test: `tests/test_opensubtitles.py` (erweitern)

**Interfaces:**
- Consumes: `ensure_token`, `reset_session`, `_headers`, `OpenSubtitlesError`, `TIMEOUT` aus Task 3
- Produces:
  - `strip_tt(imdb_id) -> str`
  - `search(imdb_id, language, season=None, episode=None, parent_imdb_id=None) -> int | None`
  - `download(file_id) -> bytes` (wirft `OpenSubtitlesError`)
  - `fetch_for(imdb_id, language, season=None, episode=None, parent_imdb_id=None) -> bytes | None`

- [ ] **Step 1: Write the failing test**

An `tests/test_opensubtitles.py` anhängen:

```python
def test_strip_tt_removes_prefix_and_leading_zeros():
    assert opensubtitles.strip_tt("tt1375666") == "1375666"
    assert opensubtitles.strip_tt("tt0133093") == "133093"
    assert opensubtitles.strip_tt("") == ""


def test_search_uses_imdb_id_for_movies():
    payload = {"data": [{"attributes": {"files": [{"file_id": 998877}]}}]}
    with patch("kodibot.core.opensubtitles.requests.get") as get:
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
    with patch("kodibot.core.opensubtitles.requests.get") as get:
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
    with patch("kodibot.core.opensubtitles.requests.get") as get:
        assert opensubtitles.search("", "de") is None
        get.assert_not_called()


def test_search_returns_none_without_results():
    with patch("kodibot.core.opensubtitles.requests.get") as get:
        get.return_value = _response(payload={"data": []})
        assert opensubtitles.search("tt1375666", "de") is None


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
        get.return_value = _response(content=b"ok")
        assert opensubtitles.download(1) == b"ok"
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_opensubtitles.py -k "strip_tt or search or download" -v`
Expected: FAIL mit `AttributeError: module 'kodibot.core.opensubtitles' has no attribute 'strip_tt'`

- [ ] **Step 3: Write minimal implementation**

An `kodibot/core/opensubtitles.py` anhängen:

```python
def strip_tt(imdb_id):
    """OpenSubtitles wants the numeric id — Kodi delivers ``tt1375666``."""
    raw = str(imdb_id or "").strip().lower()
    if raw.startswith("tt"):
        raw = raw[2:]
    return raw.lstrip("0")


def search(imdb_id, language, season=None, episode=None, parent_imdb_id=None):
    """Return the most-downloaded file_id for one language, or None."""
    params = {
        "languages": language,
        "order_by": "download_count",
        "order_direction": "desc",
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
    return requests.post(
        f"{_BASE_URL}/download",
        json={"file_id": file_id},
        headers=_headers(with_token=True),
        timeout=TIMEOUT,
    )


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
    file_res = requests.get(link, timeout=TIMEOUT)
    if file_res.status_code != 200:
        raise OpenSubtitlesError(f"fetch_failed_{file_res.status_code}")
    return file_res.content


def fetch_for(imdb_id, language, season=None, episode=None, parent_imdb_id=None):
    """Search and download one language. Returns bytes, or None without a hit."""
    if not CFG.opensubtitles_enabled:
        return None
    file_id = search(imdb_id, language, season, episode, parent_imdb_id)
    if not file_id:
        return None
    return download(file_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_opensubtitles.py -v`
Expected: 19 passed

- [ ] **Step 5: Lint und Stand berichten**

Run: `.venv/bin/ruff check kodibot/core/opensubtitles.py tests/test_opensubtitles.py`

**Nicht committen.**

---

### Task 5: Untertitel per SSH neben den Film schreiben

**Files:**
- Modify: `kodibot/core/kodi_library.py` (hinter `get_ctimes_via_ssh`, aktuell Zeile 15-58)
- Test: `tests/test_kodi_library.py` (erweitern)

**Interfaces:**
- Consumes: `KA.CFG.cec_host`, `KA.log` — beide schon in `kodi_library.py` in Gebrauch
- Produces:
  - `subtitle_path_for(video_path: str, language: str) -> str`
  - `subtitle_exists_via_ssh(video_path: str, language: str) -> bool`
  - `write_subtitle_via_ssh(video_path: str, language: str, content: bytes) -> str | None`
  - `now_playing_media_info() -> dict | None` mit den Schlüsseln `file`, `imdb_id`, `parent_imdb_id`, `season`, `episode`

- [ ] **Step 1: Write the failing test**

An `tests/test_kodi_library.py` anhängen:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_kodi_library.py -v`
Expected: FAIL mit `AttributeError: module 'kodibot.core.kodi_library' has no attribute 'subtitle_path_for'`

- [ ] **Step 3: Write minimal implementation**

An `kodibot/core/kodi_library.py` anhängen (hinter `get_ctimes_via_ssh`):

```python
def _ssh_prefix():
    host = shlex.quote(KA.CFG.cec_host)
    return f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@{host}"


def subtitle_path_for(video_path, language):
    """Kodi's naming scheme: ``Movie.mkv`` plus ``de`` becomes ``Movie.de.srt``."""
    if not video_path:
        return ""
    tail = video_path.rsplit("/", 1)[-1]
    base = video_path.rsplit(".", 1)[0] if "." in tail else video_path
    return f"{base}.{language}.srt"


def subtitle_exists_via_ssh(video_path, language):
    """True when the subtitle already sits next to the video on the Kodi host."""
    target = subtitle_path_for(video_path, language)
    if not target or not KA.CFG.cec_host or "://" in video_path:
        return False
    script = (
        "import os, sys\n"
        "print('yes' if os.path.exists(sys.argv[1]) else 'no')\n"
    )
    remote_cmd = f"python3 -c {shlex.quote(script)} {shlex.quote(target)}"
    cmd = f"{_ssh_prefix()} {shlex.quote(remote_cmd)}"
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True)
    except Exception as e:
        KA.log.warning(f"SSH subtitle probe error: {e}")
        return False
    return (res.stdout or b"").decode("utf-8", "replace").strip() == "yes"


def write_subtitle_via_ssh(video_path, language, content):
    """Write a subtitle next to the video file on the Kodi host.

    Returns the written path, or None when the host is unreachable or the
    source is not on its local filesystem (smb://, nfs://).  An existing file
    is never overwritten and counts as success.
    """
    target = subtitle_path_for(video_path, language)
    if not target or not KA.CFG.cec_host:
        return None
    if "://" in video_path:
        # Network sources do not live on the Kodi host's filesystem.
        return None
    script = (
        "import os, sys\n"
        "target = sys.argv[1]\n"
        "data = sys.stdin.buffer.read()\n"
        "if os.path.exists(target):\n"
        "    print('exists')\n"
        "    sys.exit(0)\n"
        "folder = os.path.dirname(target)\n"
        "if not os.path.isdir(folder):\n"
        "    print('nodir')\n"
        "    sys.exit(1)\n"
        "with open(target, 'wb') as fh:\n"
        "    fh.write(data)\n"
        "print('ok')\n"
    )
    remote_cmd = f"python3 -c {shlex.quote(script)} {shlex.quote(target)}"
    cmd = f"{_ssh_prefix()} {shlex.quote(remote_cmd)}"
    try:
        res = subprocess.run(cmd, shell=True, input=content, capture_output=True)
    except Exception as e:
        KA.log.warning(f"SSH subtitle write error: {e}")
        return None
    out = (res.stdout or b"").decode("utf-8", "replace").strip()
    if out in ("ok", "exists"):
        return target
    KA.log.warning(f"SSH subtitle write failed: rc={res.returncode} out={out}")
    return None


def now_playing_media_info():
    """Path and IMDb ids of the running item, for subtitle lookups.

    Returns None when nothing plays or the item has no file path.
    """
    pid = KA.get_active_playerid()
    if pid is None:
        return None
    res = KA.kodi_call(
        "Player.GetItem",
        {
            "playerid": pid,
            "properties": ["file", "uniqueid", "season", "episode", "showtitle"],
        },
    )
    item = ((res.get("result", {}) or {}).get("item") or {})
    file_path = item.get("file") or ""
    if not file_path:
        return None
    info = {
        "file": file_path,
        "imdb_id": (item.get("uniqueid") or {}).get("imdb") or "",
        "parent_imdb_id": "",
        "season": None,
        "episode": None,
    }
    if (item.get("type") or "") == "episode":
        info["season"] = item.get("season")
        info["episode"] = item.get("episode")
        tvshowid = item.get("tvshowid")
        if tvshowid:
            show = KA.kodi_call(
                "VideoLibrary.GetTVShowDetails",
                {"tvshowid": tvshowid, "properties": ["uniqueid", "imdbnumber"]},
            )
            details = ((show.get("result", {}) or {}).get("tvshowdetails") or {})
            info["parent_imdb_id"] = (
                (details.get("uniqueid") or {}).get("imdb")
                or details.get("imdbnumber")
                or ""
            )
    return info
```

Hinweis: `Player.GetItem` liefert `tvshowid` nur, wenn Kodi es zur Episode kennt. Fehlt es, bleibt `parent_imdb_id` leer und die Suche fällt auf die Episoden-ID zurück — behandelt in `search()` aus Task 4.

Der Test `test_now_playing_media_info_resolves_parent_show` verlangt, dass `tvshowid` in der Item-Antwort steht. Damit Kodi es liefert, muss `"tvshowid"` in der `properties`-Liste stehen — die Liste im Code oben deshalb auf
`["file", "uniqueid", "season", "episode", "showtitle", "tvshowid"]` erweitern.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_kodi_library.py -v`
Expected: alle grün, davon 11 neue

- [ ] **Step 5: Lint und Stand berichten**

Run: `.venv/bin/ruff check kodibot/core/kodi_library.py tests/test_kodi_library.py`

**Nicht committen.**

---

### Task 6: Untertitel in den laufenden Player laden

**Files:**
- Modify: `kodibot/core/kodi_api.py` (hinter `disable_subtitles`, aktuell Zeile 367-374)
- Test: `tests/test_kodi_api.py` (erweitern)

**Interfaces:**
- Consumes: `get_active_playerid`, `kodi_call` — beide in `kodi_api.py` vorhanden
- Produces: `add_subtitle_file(path: str) -> bool`

- [ ] **Step 1: Write the failing test**

An `tests/test_kodi_api.py` anhängen (Import-Stil der Datei übernehmen):

```python
@patch("kodibot.core.kodi_api.get_active_playerid")
@patch("kodibot.core.kodi_api.kodi_call")
def test_add_subtitle_file_sends_player_add_subtitle(mock_call, mock_pid):
    mock_pid.return_value = 1
    mock_call.return_value = {"result": "OK"}
    assert kodi_api.add_subtitle_file("/storage/videos/x.de.srt") is True
    method, params = mock_call.call_args.args
    assert method == "Player.AddSubtitle"
    assert params == {"playerid": 1, "subtitle": "/storage/videos/x.de.srt"}


@patch("kodibot.core.kodi_api.get_active_playerid")
def test_add_subtitle_file_without_player(mock_pid):
    mock_pid.return_value = None
    assert kodi_api.add_subtitle_file("/storage/videos/x.de.srt") is False


@patch("kodibot.core.kodi_api.get_active_playerid")
@patch("kodibot.core.kodi_api.kodi_call")
def test_add_subtitle_file_reports_kodi_error(mock_call, mock_pid):
    mock_pid.return_value = 1
    mock_call.return_value = {"error": {"code": -32100}}
    assert kodi_api.add_subtitle_file("/storage/videos/x.de.srt") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_kodi_api.py -k add_subtitle -v`
Expected: FAIL mit `AttributeError: module 'kodibot.core.kodi_api' has no attribute 'add_subtitle_file'`

- [ ] **Step 3: Write minimal implementation**

In `kodibot/core/kodi_api.py` hinter `disable_subtitles`:

```python
def add_subtitle_file(path):
    """Load an external subtitle file into the running player.

    Player.AddSubtitle takes no ``enable`` flag and Kodi turns the added track
    on right away, so callers that want it silent follow up with
    ``disable_subtitles()``.
    """
    playerid = get_active_playerid()
    if playerid is None or not path:
        return False
    res = kodi_call("Player.AddSubtitle", {"playerid": playerid, "subtitle": path})
    return "error" not in res
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_kodi_api.py -v`
Expected: alle grün, davon 3 neue

- [ ] **Step 5: Lint und Stand berichten**

Run: `.venv/bin/ruff check kodibot/core/kodi_api.py tests/test_kodi_api.py`

**Nicht committen.**

---

### Task 7: Verdrahtung im Untertitel-Menü

**Files:**
- Modify: `kodibot/telegram/i18n.py` (EN-Block bei Zeile 180, DE-Block bei Zeile 502 — jeweils hinter `subtitle_change_failed`)
- Modify: `kodibot/telegram/ui_callbacks.py:1-7` (Imports), neue Helfer vor `_forget_prompt`, Zweig `action == "subtitles"` ab Zeile 1143
- Test: `tests/test_opensubtitles.py` (erweitern)

**Interfaces:**
- Consumes: `opensubtitles.missing_languages`, `opensubtitles.fetch_for`, `opensubtitles.OpenSubtitlesError` (Tasks 2-4); `kodi_library.write_subtitle_via_ssh`, `kodi_library.subtitle_exists_via_ssh`, `kodi_library.subtitle_path_for`, `kodi_library.now_playing_media_info` (Task 5); `kodi_api.add_subtitle_file` (Task 6); `CFG.opensubtitles_enabled` (Task 1)
- Produces: `_subtitle_language_name(code: str) -> str`, `_write_subtitle_fallback(video_path: str, language: str, content: bytes) -> str | None`, `_fetch_missing_subtitles(ctx, chat_id, av_state) -> dict`

- [ ] **Step 1: Write the failing test**

An `tests/test_opensubtitles.py` anhängen. `import asyncio` gehört zu den Imports oben in der Datei:

```python
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
    av_state = {"subtitles": [{"language": "ger"}, {"language": "eng"}]}
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_opensubtitles.py -k "fallback or fetch_missing or language_name" -v`
Expected: FAIL mit `AttributeError: module 'kodibot.telegram.ui_callbacks' has no attribute '_write_subtitle_fallback'`

- [ ] **Step 3: Write minimal implementation**

**3a — i18n-Schlüssel.** In `kodibot/telegram/i18n.py` im englischen Block hinter `"subtitle_change_failed"` (Zeile 180):

```python
        "subtitle_search_running": "💬 Searching for subtitles …",
        "subtitle_search_none": "💬 No {language} subtitles found.",
        "subtitle_quota_exceeded": "⚠ OpenSubtitles download limit reached. Resets at {reset}.",
        "subtitle_added_temporary": "💬 Subtitles loaded for this playback only.",
```

Im deutschen Block hinter `"subtitle_change_failed"` (Zeile 502):

```python
        "subtitle_search_running": "💬 Suche Untertitel …",
        "subtitle_search_none": "💬 Keine Untertitel auf {language} gefunden.",
        "subtitle_quota_exceeded": "⚠ OpenSubtitles-Downloadlimit erreicht. Zurückgesetzt am {reset}.",
        "subtitle_added_temporary": "💬 Untertitel nur für diese Wiedergabe geladen.",
```

**3b — Imports in `kodibot/telegram/ui_callbacks.py`.** Der Kopf der Datei lautet danach:

```python
import asyncio
import logging
import os
import time

from kodibot.telegram import ui as UI
from kodibot.core import kodi_library, opensubtitles, partyvideo, radio_browser
from kodibot.telegram.i18n import repeat_mode_label, state_label, store_message, t
from kodibot.telegram.languages import LANG_MAP

log = logging.getLogger(__name__)
```

**3c — Helfer**, direkt vor `_forget_prompt` einfügen:

```python
def _subtitle_language_name(code):
    """Readable name for a language code, for chat messages."""
    entry = LANG_MAP.get((code or "").strip().lower())
    return entry[1] if entry else (code or "").upper()


def _write_subtitle_fallback(video_path, language, content):
    """Store a subtitle in the shared upload dir when SSH writing failed.

    Kodi reads it through KODI_UPLOAD_DIR, so the track works for the running
    playback but is not kept next to the movie.
    """
    tail = os.path.basename(video_path) or "subtitle"
    base = tail.rsplit(".", 1)[0] if "." in tail else tail
    name = f"{base}.{language}.srt"
    local_dir = os.path.join(UI.CFG.upload_dir, "subs")
    try:
        os.makedirs(local_dir, exist_ok=True)
        with open(os.path.join(local_dir, name), "wb") as fh:
            fh.write(content)
    except OSError as e:
        log.warning("Subtitle fallback write failed: %s", e)
        return None
    return os.path.join(UI.CFG.kodi_upload_dir, "subs", name)


async def _store_subtitle(info, language, content):
    """Put one downloaded subtitle where Kodi can read it.

    Returns ``(path, temporary)``. ``temporary`` is True when the file only
    landed in the upload dir instead of next to the movie.
    """
    path = await asyncio.to_thread(
        kodi_library.write_subtitle_via_ssh, info["file"], language, content
    )
    if path:
        return path, False
    path = await asyncio.to_thread(
        _write_subtitle_fallback, info["file"], language, content
    )
    return path, bool(path)


async def _fetch_missing_subtitles(ctx, chat_id, av_state):
    """Pull missing subtitles from OpenSubtitles for the running item.

    Returns a refreshed av_state when tracks were added, otherwise the one
    passed in.  Never raises — the caller's selection list has to show up
    either way.
    """
    if not UI.CFG.opensubtitles_enabled:
        return av_state
    missing = opensubtitles.missing_languages(av_state)
    if not missing:
        return av_state
    info = await asyncio.to_thread(kodi_library.now_playing_media_info)
    if not info or not (info.get("imdb_id") or info.get("parent_imdb_id")):
        return av_state

    status = await UI.send_and_track(ctx, chat_id, t("subtitle_search_running"))
    added = 0
    temporary = False
    try:
        for language in missing:
            path = None
            if await asyncio.to_thread(
                kodi_library.subtitle_exists_via_ssh, info["file"], language
            ):
                # Already on disk from an earlier run — no download needed.
                path = kodi_library.subtitle_path_for(info["file"], language)
            else:
                try:
                    content = await asyncio.to_thread(
                        opensubtitles.fetch_for,
                        info.get("imdb_id"),
                        language,
                        info.get("season"),
                        info.get("episode"),
                        info.get("parent_imdb_id"),
                    )
                except opensubtitles.OpenSubtitlesError as err:
                    if err.message == "quota_exceeded":
                        await UI.send_toast_message(
                            ctx,
                            chat_id,
                            t("subtitle_quota_exceeded", reset=err.reset_time or "?"),
                            delay=6,
                        )
                        break
                    log.warning("OpenSubtitles failed lang=%s err=%s", language, err.message)
                    continue
                except Exception as e:
                    log.warning("OpenSubtitles error lang=%s err=%s", language, e)
                    continue
                if not content:
                    await UI.send_toast_message(
                        ctx,
                        chat_id,
                        t(
                            "subtitle_search_none",
                            language=_subtitle_language_name(language),
                        ),
                        delay=4,
                    )
                    continue
                path, is_temp = await _store_subtitle(info, language, content)
                temporary = temporary or is_temp
            if path and await asyncio.to_thread(UI.kodi_api.add_subtitle_file, path):
                added += 1
    finally:
        await UI.delete_message_if_present(ctx, chat_id, status.message_id)

    if not added:
        return av_state
    # AddSubtitle turns the track on; the user asked for nothing to be active.
    await asyncio.to_thread(UI.kodi_api.disable_subtitles)
    if temporary:
        await UI.send_toast_message(ctx, chat_id, t("subtitle_added_temporary"), delay=4)
    # Give Kodi a moment to register the new streams before re-reading indexes.
    await asyncio.sleep(0.5)
    return await asyncio.to_thread(UI.kodi_api.get_av_settings)
```

**3d — Aufruf im Zweig `action == "subtitles"`.** In `kodibot/telegram/ui_callbacks.py` die erste Zeile des Zweigs (aktuell Zeile 1144, `subtitles = av_state.get("subtitles") or []`) durch diese beiden ersetzen:

```python
            av_state = await _fetch_missing_subtitles(ctx, chat_id, av_state)
            subtitles = av_state.get("subtitles") or []
```

Der Rest des Zweigs bleibt unverändert: Er liest `currentsubtitle`, `subtitleenabled` und die Indizes aus `av_state` — jetzt aus dem aufgefrischten Zustand.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_opensubtitles.py -v`
Expected: 23 passed

- [ ] **Step 5: Lint und Stand berichten**

Run: `.venv/bin/ruff check kodibot/telegram/ui_callbacks.py kodibot/telegram/i18n.py tests/test_opensubtitles.py`

**Nicht committen.**

---

### Task 8: Gesamtabnahme

**Files:**
- keine Änderungen, sofern die Suite grün ist

- [ ] **Step 1: Volle Suite laufen lassen**

Run: `.venv/bin/python -m pytest -q`
Expected: alle Tests grün. Schlägt etwas fehl, wird es in diesem Task behoben — ein roter Lauf beendet den Plan nicht als „fertig".

- [ ] **Step 2: Lint über das ganze Projekt**

Run: `.venv/bin/ruff check .`
Expected: keine neuen Befunde.

- [ ] **Step 3: Python-3.12-Kompatibilität prüfen**

Run: `.venv/bin/python -c "import ast,sys; [ast.parse(open(f).read(), feature_version=(3,12)) for f in ['kodibot/core/opensubtitles.py','kodibot/core/kodi_library.py','kodibot/core/kodi_api.py','kodibot/telegram/ui_callbacks.py','kodibot/config.py']]; print('3.12 ok')"`
Expected: `3.12 ok`

- [ ] **Step 4: Stand berichten**

Auflisten, welche Dateien geändert wurden und was der Maintainer noch tun muss:
1. `OPENSUBTITLES_API_KEY`, `OPENSUBTITLES_USER`, `OPENSUBTITLES_PASS` in die eigene `.env` eintragen
2. `./deploy_libreelec_partyqueue.sh` selbst ausführen

**Nicht committen, nicht deployen.**

---

## Manueller Abnahmetest (durch den Maintainer)

1. Film mit deutschen, aber ohne englische Untertitel starten → **Sprache → Untertitel wechseln** → die Liste zeigt zusätzlich eine englische Spur, am TV erscheint nichts.
2. Auf dem LibreELEC-Host prüfen, dass `<Filmname>.en.srt` neben der Videodatei liegt.
3. Film beenden, neu starten, Untertitel-Liste öffnen → die englische Spur ist jetzt ohne Zutun des Bots da, es wird kein Download ausgelöst.
4. Film mit deutschen **und** englischen Untertiteln starten → die Liste erscheint sofort, ohne Statusmeldung.
5. Einen YouTube-Stream abspielen → die Liste verhält sich wie bisher.
