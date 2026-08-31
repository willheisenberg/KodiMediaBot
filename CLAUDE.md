# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```
.venv/bin/python -m pytest -q                        # full suite
.venv/bin/python -m pytest tests/test_telegram_ui.py -q   # single file
docker build -t partyqueue .                         # build image
.venv/bin/ruff check .                               # lint (config in pyproject.toml)
```

The local venv runs Python 3.14, the container image is `python:3.12-alpine`.
Code must stay 3.12-compatible — 3.13+ syntax passes locally and breaks in production.

## Deployment

`./deploy_libreelec_partyqueue.sh` is run by the maintainer, never by Claude.
Do not scp files, ssh to the LibreELEC host, or restart containers autonomously.

## Configuration

`kodibot/config.py` reads every environment variable **at import time**
(`CFG = Config.from_env()` at module level), and required vars such as
`KODI_HOST` / `TG_TOKEN` use `os.environ[...]`. Importing anything from
`kodibot` without those set raises `KeyError`.

- Read config only via `from kodibot.config import CFG`. `os.environ` is used
  in `config.py` and nowhere else.
- A new env var has to be added in four places: the `Config` dataclass, the
  `Config.from_env()` factory, `docker-compose.local-bot-api.yml`, and
  `.env.local-bot-api.example` — plus the README's *Explanations* section when
  it is user-facing.

## Tests

There is no `conftest.py`. Each test file sets its own env defaults *before*
importing `kodibot`; copy the preamble from an existing test:

```python
os.environ.setdefault("KODI_HOST", "127.0.0.1")
# ... KODI_PORT, KODI_WS_PORT, KODI_USER, KODI_PASS, TG_TOKEN
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from kodibot.telegram import ui
```

Any `kodibot` import placed above that preamble breaks the file.

The suite must pass before work counts as done, and a bugfix is not finished
until a test in `tests/` covers it.

## Code conventions

- Mutable state shared across the telegram layer lives in
  `kodibot/telegram/state.py`. That is what keeps `ui.py`, `panel.py`,
  `rate.py` and `ui_callbacks.py` free of circular imports — put new shared
  globals there, not in whichever module happens to use them.
- Telegram API calls go through the wrappers in `kodibot/telegram/rate.py`
  (`telegram_request`, `telegram_request_delete`, `send_and_track`,
  `delete_message_if_present`), which carry the rate limiting and retry logic.
  Pass `ctx.bot.<method>` into a wrapper; never await it directly.

## Dependencies

There is no `requirements.txt` — runtime dependencies are the `pip install`
line in the `Dockerfile`. Ask before adding one; the Alpine image is
deliberately small.

## Git

Conventional Commits (`fix:`, `feat:`, `docs:`). Never commit or push unless
explicitly asked.
