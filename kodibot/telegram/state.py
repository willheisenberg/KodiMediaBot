"""Shared state and constants for the Telegram UI layer.

All mutable globals that are accessed across telegram_rate, telegram_panel,
and telegram_handlers live here so that circular imports are avoided.
"""

import asyncio
import threading

# ── Message tracking ─────────────────────────────────────────────────
pending = {}
IMAGE_GROUPS = {}
IMAGE_GROUP_TASKS = {}
IMAGE_GROUP_DELAY_SECONDS = 2.0

LAST_BOT_ID = {}
PREV_BOT_ID = {}
LAST_SEEN_ID = {}
LAST_CLEANUP_ID = {}
FIRST_BOT_ID = {}
STARTUP_POSTED = {}
LIST_MSG_ID = {}
PANEL_MSG_ID = {}
PANEL_MENU_MODE = {}
HA_MENU_MSG_ID = {}

# ── Status caches ────────────────────────────────────────────────────
HIFI_STATUS_CACHE = "⚪ Hifi: Unknown"
HIFI_STATUS_TS = 0.0
AIRPLAY_STATUS_CACHE = "AirPlay: Unknown"
AIRPLAY_STATUS_TS = 0.0
DENON_VOLUME_CACHE = "🔊 --"
DENON_VOLUME_TS = 0.0

# ── Rate limiting ────────────────────────────────────────────────────
TG_RATE_LOCK = asyncio.Lock()
TG_DELETE_RATE_LOCK = asyncio.Lock()
TG_LAST_TS = 0.0
TG_DELETE_LAST_TS = 0.0
TG_MIN_INTERVAL = 0.6
TG_DELETE_MIN_INTERVAL = 1.0
TG_MAX_RETRIES = 3
TG_DYNAMIC_DELAY = 0.0
TG_DYNAMIC_UNTIL = 0.0

# ── Concurrency ──────────────────────────────────────────────────────
PLAYBACK_TASK_LOCK = asyncio.Lock()
NP_REFRESH_LOCK = threading.Lock()
NP_REFRESH_FUTURE = None
NP_REFRESH_LAST_TS = 0.0
NP_REFRESH_MIN_INTERVAL = 0.5
RESET_PANEL_LOCK = asyncio.Lock()
RESETTING_CHATS = set()

# ── App references ───────────────────────────────────────────────────
APP_INSTANCE = None
MAIN_LOOP = None

# ── Render caches ────────────────────────────────────────────────────
LIST_RENDER_CACHE = {}
PANEL_RENDER_CACHE = {}
LIST_REFRESH_TASK = None
WS_LISTENER_TASK = None

# ── Prompt state ─────────────────────────────────────────────────────
PROMPT_TIMEOUT_SECONDS = 300
PROMPT_TIMEOUT_TASKS = {}
PENDING_TIMEOUT_TASKS = {}
HA_MENU_TIMEOUT_SECONDS = 300
HA_MENU_TIMEOUT_TASKS = {}
