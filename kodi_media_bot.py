import os, re, threading, time, requests, asyncio, subprocess, html, json
from urllib.parse import unquote
from pytube import Playlist, YouTube
from telegram.ext import Application, MessageHandler, filters, CallbackQueryHandler, CommandHandler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter, TimedOut
import websockets

TOKEN = os.environ["TG_TOKEN"]
KODI_HOST = os.environ["KODI_HOST"]
KODI_PORT = os.environ["KODI_PORT"]
KODI_WS_PORT = os.environ["KODI_WS_PORT"]
KODI_URL = f"http://{KODI_HOST}:{KODI_PORT}/jsonrpc"
AUTH = (os.environ["KODI_USER"], os.environ["KODI_PASS"])
STARTUP_CHAT_ID = -1003641420817
CEC_HOST = os.environ.get("CEC_HOST") or os.environ.get("HOST_IP", "172.17.0.1")
CEC_CMD_VOL_UP = "0x41"
CEC_CMD_VOL_DOWN = "0x42"

YT = re.compile(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})")
PL = re.compile(r"(?:[?&]list=)([A-Za-z0-9_-]+)")
SC = re.compile(r"https?://(www\.)?soundcloud\.com/[^/]+/[^/?#]+")
SC_SHORT = re.compile(r"https?://on\.soundcloud\.com/[A-Za-z0-9]+")

pending = {}

HELP_TEXT = (
    "Commands:\n"
    "/info – this help\n"
    "/list – current queue (numbered)\n"
    "/play <n> – plays item n (queue stays)\n"
    "/stop – stop playback\n"
    "/skip – next track\n"
    "/back – previous track\n"
    "/delete <n> – remove item n\n"
    "/deleteall – clear the queue\n"
    "/repeat – toggle: off → one → all\n"
    "\n"
    "Post links:\n"
    "- YouTube video link → appended to the end\n"
    "- YouTube link with ?list=… → bot asks: 1 (video only) or L (full list)\n"
)

LAST_BOT_ID = {}
PREV_BOT_ID = {}
LAST_SEEN_ID = {}
LAST_CLEANUP_ID = {}
FIRST_BOT_ID = {}
STARTUP_POSTED = {}
LIST_MSG_ID = {}
PANEL_MSG_ID = {}
LIST_DIRTY = False
HIFI_STATUS_CACHE = "⚪ Hifi: Unknown"
HIFI_STATUS_TS = 0.0
TG_RATE_LOCK = asyncio.Lock()
TG_LAST_TS = 0.0
TG_MIN_INTERVAL = 1.1
TG_MAX_RETRIES = 3
LAST_PROGRESS_TS = 0.0
LAST_PROGRESS_TIME = None
LAST_PROGRESS_TOTAL = None
LAST_PROGRESS_INDEX = None
RESUME_ATTEMPTS = {}
RESUME_MAX_ATTEMPTS = 8
RESUME_MIN_REMAINING_SEC = 10
RESUME_SEEK_WAIT_SEC = 20
EXTERNAL_PLAYBACK = False
BOT_EXPECTING_WS = 0
WS_CONNECTED = False
WS_PLAYING = False
WS_LAST_EVENT_TS = 0.0
WS_STATE = "unknown"
KODI_WS_URL = None
APP_INSTANCE = None
MAIN_LOOP = None
AUTOPLAY_THREAD_STARTED = False
AUTOPLAY_THREAD = None

# Serialize Telegram API calls to avoid send/edit/delete collisions.
async def telegram_request(call, *args, **kwargs):
    global TG_LAST_TS
    for _ in range(TG_MAX_RETRIES):
        async with TG_RATE_LOCK:
            now = time.time()
            wait = TG_MIN_INTERVAL - (now - TG_LAST_TS)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                res = await call(*args, **kwargs)
                TG_LAST_TS = time.time()
                return res
            except RetryAfter as e:
                TG_LAST_TS = time.time()
                await asyncio.sleep(e.retry_after)
            except TimedOut:
                TG_LAST_TS = time.time()
                await asyncio.sleep(1.5)
            except Exception:
                TG_LAST_TS = time.time()
                raise
    async with TG_RATE_LOCK:
        now = time.time()
        wait = TG_MIN_INTERVAL - (now - TG_LAST_TS)
        if wait > 0:
            await asyncio.sleep(wait)
        res = await call(*args, **kwargs)
        TG_LAST_TS = time.time()
        return res

# Mark the playlist display as needing refresh.
def mark_list_dirty():
    global LIST_DIRTY
    LIST_DIRTY = True

# Clear bot playback state without stopping Kodi playback.
def clear_bot_playback_state():
    global AUTOPLAY_ENABLED, CURRENT_INDEX, DISPLAY_INDEX, EXTERNAL_PLAYBACK
    with LOCK:
        AUTOPLAY_ENABLED = False
        CURRENT_INDEX = None
        DISPLAY_INDEX = None
        EXTERNAL_PLAYBACK = True
        RESUME_ATTEMPTS.clear()
    mark_list_dirty()

# Refresh now-playing panel from non-async contexts.
def schedule_now_playing_refresh():
    if APP_INSTANCE is None or MAIN_LOOP is None:
        return
    asyncio.run_coroutine_threadsafe(
        update_now_playing_message(APP_INSTANCE, STARTUP_CHAT_ID),
        MAIN_LOOP,
    )

# Refresh list + now-playing after playback state changes.
def schedule_playback_refresh():
    mark_list_dirty()
    schedule_now_playing_refresh()

# Build the inline keyboard control panel markup.
def control_panel():
    play_label = "⏸" if WS_STATE == "playing" else "▶"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▶ No.", callback_data="play:ask"),
            InlineKeyboardButton("⏮", callback_data="back"),
            InlineKeyboardButton(play_label, callback_data="playpause"),
            InlineKeyboardButton("⏭", callback_data="skip"),
            InlineKeyboardButton("⏹ Stop", callback_data="stop"),
        ],
        [
            InlineKeyboardButton("🔁 Repeat", callback_data="repeat"),
            InlineKeyboardButton("🗑 Delete No.", callback_data="delete:ask"),
            InlineKeyboardButton("🗑 Delete all", callback_data="deleteall"),
        ],
        [
            InlineKeyboardButton("🔊 +5", callback_data="vol:up5"),
            InlineKeyboardButton("🔊 +10", callback_data="vol:up10"),
        ],
        [
            InlineKeyboardButton("🔉 -5", callback_data="vol:down5"),
            InlineKeyboardButton("🔉 -10", callback_data="vol:down10"),
        ],
        [
            InlineKeyboardButton("🔌 Hifi On", callback_data="hifi:on"),
            InlineKeyboardButton("🔌 Hifi Off", callback_data="hifi:off"),
        ],
    ])

# Send a JSON-RPC request to Kodi and return the response JSON.
def kodi_call(method: str, params: dict | None = None):
    payload = {"jsonrpc": "2.0", "method": method, "id": 1}
    if params:
        payload["params"] = params
    return requests.post(KODI_URL, auth=AUTH, json=payload, timeout=5).json()


# Return the first active Kodi player, if any.
def get_active_player():
    players = get_active_players()
    return players[0] if players else None

# Return the active player id, if any.
def get_active_playerid():
    p = get_active_player()
    return p["playerid"] if p else None

# Fetch the list of active Kodi players.
def get_active_players():
    return kodi_call("Player.GetActivePlayers").get("result", [])

# Send repeated CEC volume commands over SSH.
def run_cec_volume(times: int, cmd_hex: str) -> bool:
    cmd = (
        f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@{CEC_HOST} seq {times} | "
        f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@{CEC_HOST} "
        f"xargs -Iz cec-ctl --user-control-pressed ui-cmd={cmd_hex} -t5"
    )
    try:
        res = subprocess.run(cmd, shell=True, check=False, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"CEC FAIL rc={res.returncode} stderr={res.stderr.strip()}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"CEC ERROR err={e}", flush=True)
        return False

# Turn the audio system on or off via CEC over SSH.
def run_cec_power(on: bool) -> bool:
    if on:
        cmd = (
            f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@{CEC_HOST} "
            f"cec-ctl --user-control-pressed ui-cmd=power-on-function -t0 && "
            f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@{CEC_HOST} "
            f"cec-ctl --user-control-pressed ui-cmd=power-on-function -t5"
        )
    else:
        cmd = (
            f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@{CEC_HOST} "
            f"cec-ctl --standby -t0 && "
            f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@{CEC_HOST} "
            f"cec-ctl --standby -t5"
        )
    try:
        res = subprocess.run(cmd, shell=True, check=False, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"CEC FAIL rc={res.returncode} stderr={res.stderr.strip()}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"CEC ERROR err={e}", flush=True)
        return False

# Query the audio system power state via CEC.
def get_hifi_power_status():
    cmd = (
        f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@{CEC_HOST} "
        f"cec-ctl --show-topology | awk '/Audio System/{{f=1}} f && /Power Status/{{print $NF; exit}}'"
    )
    try:
        res = subprocess.run(cmd, shell=True, check=False, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"CEC FAIL rc={res.returncode} stderr={res.stderr.strip()}", flush=True)
            return None
        val = (res.stdout or "").strip()
        if val in ("On", "Standby"):
            return val
        return None
    except Exception as e:
        print(f"CEC ERROR err={e}", flush=True)
        return None

# Send a Telegram message and track its message id.
async def send_and_track(ctx, chat_id, text, **kwargs):
    if "disable_web_page_preview" not in kwargs:
        kwargs["disable_web_page_preview"] = True
    msg = await telegram_request(ctx.bot.send_message, chat_id=chat_id, text=text, **kwargs)
    if chat_id not in FIRST_BOT_ID:
        FIRST_BOT_ID[chat_id] = msg.message_id
    PREV_BOT_ID[chat_id] = LAST_BOT_ID.get(chat_id)
    LAST_BOT_ID[chat_id] = msg.message_id
    print(f"BOT MSG chat_id={chat_id} message_id={msg.message_id}", flush=True)
    return msg

# Send the queue list and control panel messages.
async def send_info_list_panel(ctx, chat_id):
    with LOCK:
        if not QUEUE:
            out = "Queue empty."
        else:
            lines = [format_item_line(i, it) for i, it in enumerate(QUEUE)]
            out = "\n".join(lines)
    list_msg = await send_and_track(ctx, chat_id, out, parse_mode="HTML")
    LIST_MSG_ID[chat_id] = list_msg.message_id
    panel_msg = await send_and_track(ctx, chat_id, "🎛 Kodi Remote - Current track:", reply_markup=control_panel())
    PANEL_MSG_ID[chat_id] = panel_msg.message_id

# Format a single queue item as a display line.
def format_item_line(i, it):
    mark = "▶ " if i == DISPLAY_INDEX else ""
    title = html.escape(it.get("title", ""), quote=False)
    link = it.get("link")
    if link:
        safe_link = html.escape(link, quote=True)
        return f"{mark}{i+1}. <a href=\"{safe_link}\">{title}</a>"
    return f"{mark}{i+1}. {title}"

# Build the full queue list text for display.
def build_list_text():
    with LOCK:
        if not QUEUE:
            return "Queue empty."
        lines = [format_item_line(i, it) for i, it in enumerate(QUEUE)]
        return "🎵 Playlist:\n\n" + "\n".join(lines)

# Update or create the queue list message.
async def update_list_message(ctx, chat_id):
    msg_id = LIST_MSG_ID.get(chat_id)
    if not msg_id:
        # Create list message if missing
        list_msg = await send_and_track(ctx, chat_id, build_list_text(), parse_mode="HTML")
        LIST_MSG_ID[chat_id] = list_msg.message_id
        # If the panel exists, only create the list message; otherwise recreate both.
        if PANEL_MSG_ID.get(chat_id):
            list_msg = await send_and_track(ctx, chat_id, build_list_text(), parse_mode="HTML")
            LIST_MSG_ID[chat_id] = list_msg.message_id
        else:
            await send_info_list_panel(ctx, chat_id)
        return
    try:
        await telegram_request(
            ctx.bot.edit_message_text,
            chat_id=chat_id,
            message_id=msg_id,
            text=build_list_text(),
            parse_mode="HTML",
            disable_web_page_preview=True
        )
    except Exception:
        pass
    else:
        global LIST_DIRTY
        LIST_DIRTY = False

# Format Kodi time dict into a mm:ss or h:mm:ss string.
def format_kodi_time(t):
    if not t:
        return "00:00"
    h = t.get("hours", 0)
    m = t.get("minutes", 0)
    s = t.get("seconds", 0)
    total = h * 3600 + m * 60 + s
    if total >= 3600:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

# Convert Kodi time dict into total seconds.
def kodi_time_seconds(t):
    if not t:
        return None
    return t.get("hours", 0) * 3600 + t.get("minutes", 0) * 60 + t.get("seconds", 0)

# Normalize a title for loose comparison.
def normalize_title(s):
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip().casefold()

# Assemble the now-playing display text.
def get_now_playing_text():
    global LAST_PROGRESS_TS, LAST_PROGRESS_TIME, LAST_PROGRESS_TOTAL, LAST_PROGRESS_INDEX, EXTERNAL_PLAYBACK
    global AUTOPLAY_ENABLED, CURRENT_INDEX, DISPLAY_INDEX, WS_PLAYING
    name = None
    link = None
    with LOCK:
        if not EXTERNAL_PLAYBACK and DISPLAY_INDEX is not None and 0 <= DISPLAY_INDEX < len(QUEUE):
            it = QUEUE[DISPLAY_INDEX]
            name = it.get("title") or None
            link = it.get("link")

    players = get_active_players()
    if not players:
        if WS_PLAYING and name:
            safe_name = html.escape(name, quote=False)
            if link:
                safe_link = html.escape(link, quote=True)
                return f"▶ <a href=\"{safe_link}\">{safe_name}</a>"
            return f"▶ {safe_name}"
        if WS_PLAYING and not name:
            return "▶ Playing..."
        EXTERNAL_PLAYBACK = False
        if name:
            safe_name = html.escape(name, quote=False)
            if link:
                safe_link = html.escape(link, quote=True)
                return f"▶ <a href=\"{safe_link}\">{safe_name}</a>"
            return f"▶ {safe_name}"
        return "⏸ Nothing playing"

    player = players[0]
    pid = player.get("playerid")
    if pid is None:
        EXTERNAL_PLAYBACK = False
        return "⏸ Nothing playing"

    # External playback is now detected via Kodi WebSocket events.

    props = kodi_call(
        "Player.GetProperties",
        {"playerid": pid, "properties": ["time", "totaltime"]}
    ).get("result", {})

    if not name:
        item = kodi_call(
            "Player.GetItem",
            {"playerid": pid, "properties": ["title", "artist", "label"]}
        ).get("result", {}).get("item", {})
        artists = item.get("artist") or []
        title = item.get("title") or ""
        label = item.get("label") or ""
        if artists and title:
            name = f"{', '.join(artists)} - {title}"
        else:
            name = label or title or "Unknown"

    cur = format_kodi_time(props.get("time"))
    total = format_kodi_time(props.get("totaltime"))
    LAST_PROGRESS_TS = time.time()
    LAST_PROGRESS_TIME = props.get("time")
    LAST_PROGRESS_TOTAL = props.get("totaltime")
    LAST_PROGRESS_INDEX = DISPLAY_INDEX
    safe_name = html.escape(name, quote=False)
    if link:
        safe_link = html.escape(link, quote=True)
        return f"▶ <a href=\"{safe_link}\">{safe_name}</a> | {cur} / {total}"
    return f"▶ {safe_name} | {cur} / {total}"

# Update or create the now-playing panel message.
async def update_now_playing_message(ctx, chat_id):
    msg_id = PANEL_MSG_ID.get(chat_id)
    text = get_now_playing_text()
    hifi_text = HIFI_STATUS_CACHE
    if not msg_id:
        panel_msg = await send_and_track(
            ctx,
            chat_id,
            f"🎛 Kodi Remote - Current track:\n{text}\n{hifi_text}",
            reply_markup=control_panel(),
            parse_mode="HTML",
        )
        PANEL_MSG_ID[chat_id] = panel_msg.message_id
        return
    try:
        await telegram_request(
            ctx.bot.edit_message_text,
            chat_id=chat_id,
            message_id=msg_id,
            text=f"🎛 Kodi Remote - Current track:\n{text}\n{hifi_text}",
            parse_mode="HTML",
            reply_markup=control_panel(),
        )
    except Exception:
        pass

# Refresh cached hifi power status with throttling.
async def refresh_hifi_status_cache(force=False):
    global HIFI_STATUS_CACHE, HIFI_STATUS_TS
    now = time.time()
    if not force and now - HIFI_STATUS_TS < 300:
        return
    status = await asyncio.to_thread(get_hifi_power_status)
    if status == "On":
        HIFI_STATUS_CACHE = "🟢 Hifi: On"
    elif status == "Standby":
        HIFI_STATUS_CACHE = "🔴 Hifi: Standby"
    # If unknown/None, keep previous value but still advance timestamp
    HIFI_STATUS_TS = now

# Listen for Kodi playback events via WebSocket.
async def kodi_ws_listener():
    global KODI_WS_URL, WS_PLAYING, WS_LAST_EVENT_TS, BOT_EXPECTING_WS, WS_CONNECTED, WS_STATE
    if KODI_WS_URL is None:
        KODI_WS_URL = f"ws://{KODI_HOST}:{KODI_WS_PORT}/jsonrpc"
    while True:
        try:
            async with websockets.connect(KODI_WS_URL, ping_interval=20, ping_timeout=20) as ws:
                WS_CONNECTED = True
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    method = msg.get("method")
                    if method in ("Player.OnPlay", "Player.OnAVStart"):
                        now = time.time()
                        WS_PLAYING = True
                        WS_STATE = "playing"
                        WS_LAST_EVENT_TS = now
                        if BOT_EXPECTING_WS > 0:
                            BOT_EXPECTING_WS -= 1
                        else:
                            clear_bot_playback_state()
                            schedule_now_playing_refresh()
                    elif method == "Player.OnPause":
                        WS_PLAYING = False
                        WS_STATE = "paused"
                        WS_LAST_EVENT_TS = time.time()
                        schedule_now_playing_refresh()
                    elif method == "Player.OnResume":
                        WS_PLAYING = True
                        WS_STATE = "playing"
                        WS_LAST_EVENT_TS = time.time()
                        schedule_now_playing_refresh()
                    elif method == "Player.OnStop":
                        WS_PLAYING = False
                        WS_STATE = "stopped"
                        WS_LAST_EVENT_TS = time.time()
                        schedule_now_playing_refresh()
        except Exception:
            WS_CONNECTED = False
            WS_STATE = "unknown"
            await asyncio.sleep(3)

# Background task to refresh list and now-playing messages.
async def list_refresher(ctx):
    last_np = 0.0
    last_hifi = 0.0
    while True:
        if LIST_DIRTY:
            await update_list_message(ctx, STARTUP_CHAT_ID)
        now = time.time()
        if now - last_np >= 5:
            await update_now_playing_message(ctx, STARTUP_CHAT_ID)
            last_np = now
        if now - last_hifi >= 300:
            await refresh_hifi_status_cache(force=True)
            await update_now_playing_message(ctx, STARTUP_CHAT_ID)
            last_hifi = now
        await asyncio.sleep(2)

# Ensure the startup panel is posted once.
async def ensure_startup_panel(ctx, chat_id):
    if STARTUP_POSTED.get(chat_id):
        return
    STARTUP_POSTED[chat_id] = True
    await send_info_list_panel(ctx, chat_id)

# Record the last seen user message id per chat.
def record_last_seen(ctx, update):
    msg = update.effective_message
    if msg:
        LAST_SEEN_ID[update.effective_chat.id] = msg.message_id
        print(f"SEEN chat_id={update.effective_chat.id} message_id={msg.message_id}", flush=True)

# Schedule deletion of recent messages after a delay.
def schedule_cleanup(ctx, chat_id, prev_id):
    last_seen = LAST_SEEN_ID.get(chat_id)
    last_bot = LAST_BOT_ID.get(chat_id)
    if last_bot is None:
        return
    # Prefer previous bot msg; fall back to last seen user msg
    start_inclusive = False
    if PREV_BOT_ID.get(chat_id) is not None:
        prev_id = PREV_BOT_ID.get(chat_id)
    elif last_seen is not None:
        prev_id = last_seen
        start_inclusive = True
    elif LAST_CLEANUP_ID.get(chat_id) is not None:
        prev_id = LAST_CLEANUP_ID.get(chat_id)
    else:
        prev_id = FIRST_BOT_ID.get(chat_id)
    end_id = max(x for x in [last_seen, last_bot] if x is not None)
    print(f"SCHEDULE CLEANUP chat_id={chat_id} prev_id={prev_id} end_id={end_id} inclusive={start_inclusive} last_cleanup={LAST_CLEANUP_ID.get(chat_id)}", flush=True)
    if hasattr(ctx, "application"):
        ctx.application.create_task(_cleanup_after_delay(ctx, chat_id, prev_id, end_id, start_inclusive))
    else:
        asyncio.create_task(_cleanup_after_delay(ctx, chat_id, prev_id, end_id, start_inclusive))

# Delete a range of messages after a delay.
async def _cleanup_after_delay(ctx, chat_id, start_id, end_id, start_inclusive):
    await asyncio.sleep(7)
    print(f"RUN CLEANUP chat_id={chat_id} start_id={start_id} end_id={end_id} inclusive={start_inclusive}", flush=True)
    if start_id is not None:
        begin = start_id if start_inclusive else start_id + 1
        for mid in range(begin, end_id + 1):
            try:
                if mid == LIST_MSG_ID.get(chat_id):
                    continue
                if mid == PANEL_MSG_ID.get(chat_id):
                    continue
                await telegram_request(ctx.bot.delete_message, chat_id=chat_id, message_id=mid)
            except Exception as e:
                print(f"DELETE FAIL chat_id={chat_id} message_id={mid} err={e}", flush=True)
    LAST_CLEANUP_ID[chat_id] = end_id

# Warn about off-topic chat and remove both messages.
async def warn_and_cleanup_chat(ctx, chat_id, user_msg_id, delay=5):
    warn = await send_and_track(
        ctx,
        chat_id,
        "This group is not meant for conversations."
    )
    await asyncio.sleep(delay)
    try:
        await telegram_request(ctx.bot.delete_message, chat_id=chat_id, message_id=warn.message_id)
    except Exception as e:
        print(f"DELETE FAIL chat_id={chat_id} message_id={warn.message_id} err={e}", flush=True)
    try:
        await telegram_request(ctx.bot.delete_message, chat_id=chat_id, message_id=user_msg_id)
    except Exception as e:
        print(f"DELETE FAIL chat_id={chat_id} message_id={user_msg_id} err={e}", flush=True)

# Try to seek to a time once a player is available.
def seek_when_player_ready(t, context=""):
    def _seek():
        end = time.time() + RESUME_SEEK_WAIT_SEC
        start_ts = time.time()
        last_log_ts = 0.0
        while time.time() < end:
            players = get_active_players()
            pid = players[0]["playerid"] if players else None
            if pid is not None:
                try:
                    props = kodi_call(
                        "Player.GetProperties",
                        {"playerid": pid, "properties": ["totaltime", "canseek"]}
                    ).get("result", {})
                    if not props.get("canseek"):
                        print(f"RESUME SEEK skip canseek=false playerid={pid} ctx={context}", flush=True)
                        return
                    target_sec = kodi_time_seconds(t)
                    if target_sec is None:
                        print(
                            f"RESUME SEEK skip invalid times playerid={pid} ctx={context} "
                            f"target={t}",
                            flush=True
                        )
                        return
                    print(
                        f"RESUME SEEK playerid={pid} ctx={context} target_sec={target_sec}",
                        flush=True
                    )
                    kodi_call(
                        "Player.Seek",
                        {"playerid": pid, "value": {"time": t}}
                    )
                except Exception:
                    pass
                return
            now = time.time()
            if now - last_log_ts >= 1.0:
                elapsed = now - start_ts
                print(
                    f"RESUME SEEK waiting for playerid ctx={context} elapsed={elapsed:.1f}s players={players}",
                    flush=True
                )
                last_log_ts = now
            time.sleep(0.3)
        print(f"RESUME SEEK gave up: no playerid ctx={context}", flush=True)
    threading.Thread(target=_seek, daemon=True).start()

# Start playback of a queue item via Kodi.
def play_item(item: dict, resume_time=None):
    # Stop + clear Kodi state, but leave bot state unchanged.
    global BOT_EXPECTING_WS
    stop_all_players()
    kodi_clear_all_playlists()
    BOT_EXPECTING_WS = 2
    print(
        f"PLAY_ITEM start kind={item.get('kind')} title={item.get('title')} url={item.get('url')}",
        flush=True,
    )

    # Explicitly use audio (0) vs video (1) playlists.
    kind = item.get("kind", "video")
    if kind == "audio":
        # Start SoundCloud via the audio playlist, then switch to the real stream.
        playlistid = 0
        kodi_add_to_playlist(item["url"], playlistid)
        res = kodi_call("Player.Open", {"item": {"playlistid": playlistid, "position": 0}})
        print(f"PLAY_ITEM open audio res={res}", flush=True)
        schedule_audio_resolve_and_open(playlistid, resume_time=resume_time)
    else:
        playlistid = 1
        kodi_add_to_playlist(item["url"], playlistid)
        res = kodi_call("Player.Open", {"item": {"playlistid": playlistid}})
        print(f"PLAY_ITEM open video res={res}", flush=True)
        schedule_playback_refresh()
        if resume_time is not None:
            seek_when_player_ready(resume_time, context="video")
    players = get_active_players()
    print(f"PLAY_ITEM active_players={players}", flush=True)

# Start playback and then seek to a saved timestamp.
def resume_item_at_time(item: dict, t):
    if not t:
        play_item(item)
        return
    play_item(item, resume_time=t)

# Stop all active Kodi players.
def stop_all_players():
    for p in get_active_players():
        pid = p.get("playerid")
        if pid is not None:
            kodi_call("Player.Stop", {"playerid": pid})


# Stop playback and clear Kodi playlists.
def stop_player_and_clear_playlists():
    stop_all_players()
    kodi_clear_all_playlists()

# Stop playback and reset bot playback state.
def hard_stop_and_clear():
    global AUTOPLAY_ENABLED, CURRENT_INDEX, DISPLAY_INDEX, NEXT_INDEX, LAST_PROGRESS_TS, LAST_PROGRESS_TIME, LAST_PROGRESS_TOTAL, LAST_PROGRESS_INDEX, EXTERNAL_PLAYBACK, BOT_EXPECTING_WS
    AUTOPLAY_ENABLED = False
    stop_all_players()
    kodi_clear_all_playlists()
    CURRENT_INDEX = None
    DISPLAY_INDEX = None
    NEXT_INDEX = 0
    LAST_PROGRESS_TS = 0.0
    LAST_PROGRESS_TIME = None
    LAST_PROGRESS_TOTAL = None
    LAST_PROGRESS_INDEX = None
    EXTERNAL_PLAYBACK = False
    BOT_EXPECTING_WS = 0
    RESUME_ATTEMPTS.clear()

# Clear both audio and video Kodi playlists.
def kodi_clear_all_playlists():
    # Audio
    kodi_call("Playlist.Clear", {"playlistid": 0})
    # Video
    kodi_call("Playlist.Clear", {"playlistid": 1})

# Advance to the next queue item and start playback.
def skip_queue():
    global CURRENT_INDEX, DISPLAY_INDEX, NEXT_INDEX, AUTOPLAY_ENABLED

    with LOCK:
        if DISPLAY_INDEX is None:
            i = 0
        else:
            i = DISPLAY_INDEX + 1

        if i >= len(QUEUE):
            AUTOPLAY_ENABLED = False
            CURRENT_INDEX = None
            DISPLAY_INDEX = None
            NEXT_INDEX = 0
            stop_player_and_clear_playlists()
            return False

    play_index(i)
    return True

QUEUE = []
CURRENT_INDEX = None
DISPLAY_INDEX = None
NEXT_INDEX = 0
LOCK = threading.Lock()
AUTOPLAY_ENABLED = True
REPEAT_MODE = "off"

# Create a queue item dict.
def make_item(title, url, kind, link=None):
    return {"title": title, "url": url, "kind": kind, "link": link}

# Fetch a YouTube title and author for display.
def fetch_youtube_title(vid):
    url = f"https://youtu.be/{vid}"
    try:
        yt = YouTube(url)
        author = yt.author or ""
        title = yt.title or ""
        if author and title:
            return f"{author} - {title}"
        if title:
            return title
    except Exception:
        pass
    try:
        oembed = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=6,
        )
        if oembed.ok:
            data = oembed.json()
            author = data.get("author_name", "")
            title = data.get("title", "")
            if author and title:
                return f"{author} - {title}"
            if title:
                return title
    except Exception:
        pass
    return url

# Create a YouTube queue item with Kodi plugin URL.
def make_youtube(vid, title=None):
    link = f"https://youtu.be/{vid}"
    return make_item(
        title or link,
        f"plugin://plugin.video.youtube/play/?video_id={vid}",
        "video",
        link=link
    )

# Derive a display title from a SoundCloud URL.
def soundcloud_display_title(clean_url):
    m = re.match(r"^https?://(www\.)?soundcloud\.com/([^/]+)/([^/?#]+)", clean_url)
    if not m:
        return clean_url
    artist = unquote(m.group(2)).replace("-", " ")
    track = unquote(m.group(3)).replace("-", " ")
    return f"{artist} - {track}".strip()

# Create a SoundCloud queue item with Kodi plugin URL.
def make_soundcloud(url):
    clean = re.sub(r"\?.*$", "", url)
    return make_item(
        soundcloud_display_title(clean),
        f"plugin://plugin.audio.soundcloud/play/?url={clean}",
        "audio",
        link=clean
    )

# Validate that a SoundCloud URL is a track link.
def is_sc_track_url(url):
    # Accept only artist/track links; reject discover/sets and other non-track paths
    return bool(re.match(r"^https?://(www\.)?soundcloud\.com/[^/]+/[^/?#]+", url)) and "discover/sets" not in url

# Resolve a SoundCloud short link to a full track URL.
def resolve_sc_short(url):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        }
        r = requests.get(url, allow_redirects=True, timeout=8, headers=headers)
        print(f"SC_SHORT RESOLVE start={url} final={r.url} history={[h.url for h in r.history]}", flush=True)
        # Prefer a real soundcloud.com target (avoid /discover/sets fallback)
        candidates = [h.url for h in r.history] + [r.url]
        for u in candidates:
            if re.match(r"^https?://(www\.)?soundcloud\.com/", u) and "discover/sets" not in u:
                print(f"SC_SHORT RESOLVE pick={u}", flush=True)
                return u
        # Try to extract canonical/og:url from HTML
        m = re.search(r'https?://soundcloud\.com/[^\s"\'<>]+', r.text)
        if m:
            print(f"SC_SHORT RESOLVE html={m.group(0)}", flush=True)
            return m.group(0)
        print("SC_SHORT RESOLVE failed", flush=True)
        return None
    except Exception as e:
        print(f"SC_SHORT RESOLVE error={e}", flush=True)
        return None

# Add a file URL to a Kodi playlist.
def kodi_add_to_playlist(url, playlistid):
    kodi_call(
        "Playlist.Add",
        {"playlistid": playlistid, "item": {"file": url}}
    )

# Start playback of a Kodi playlist.
def kodi_play_playlist(playlistid):
    kodi_call(
        "Player.Open",
        {"item": {"playlistid": playlistid}}
    )

# Poll the Kodi playlist for the resolved SoundCloud stream URL.
def resolve_soundcloud_media_url(playlistid, timeout_s=6.0, interval_s=0.5):
    # Wait until the addon creates the real stream entry.
    end = time.time() + timeout_s
    while time.time() < end:
        res = kodi_call(
            "Playlist.GetItems",
            {"playlistid": playlistid, "properties": ["file", "title"]}
        )
        items = res.get("result", {}).get("items", [])
        for it in items:
            f = it.get("file", "")
            if "media_url=" in f:
                return f
        time.sleep(interval_s)
    return None

# Resolve SoundCloud stream URL asynchronously and open it.
def schedule_audio_resolve_and_open(playlistid, resume_time=None):
    # As soon as the real stream is available, open it and clear the playlist.
    # Worker to resolve and open the real SoundCloud stream.
    def _run():
        url = resolve_soundcloud_media_url(playlistid)
        if not url:
            return
        kodi_call("Player.Open", {"item": {"file": url}})
        kodi_call("Playlist.Clear", {"playlistid": playlistid})
        schedule_playback_refresh()
        if resume_time is not None:
            print("PLAY_ITEM audio stream opened; seeking...", flush=True)
            seek_when_player_ready(resume_time, context="audio")
    threading.Thread(target=_run, daemon=True).start()


# Append an item to the queue and mark list dirty.
def queue_item(item):
    with LOCK:
        QUEUE.append(item)
    mark_list_dirty()

# Expand a YouTube playlist into video ids.
def expand_playlist(pid):
    pl = Playlist(f"https://www.youtube.com/playlist?list={pid}")
    return [YT.search(v).group(1) for v in pl.video_urls if YT.search(v)]

# Append a YouTube video to the queue.
def queue_video(vid, title=None):
    with LOCK:
        QUEUE.append(make_youtube(vid, title=title))
    mark_list_dirty()

# Fetch YouTube title asynchronously and queue the video.
async def queue_video_async(vid):
    try:
        title = await asyncio.to_thread(fetch_youtube_title, vid)
    except Exception:
        title = None
    queue_video(vid, title=title)


# Queue all items from a YouTube playlist.
def queue_playlist(pid):
    for vid in expand_playlist(pid):
        queue_video(vid)
    mark_list_dirty()

# Asynchronously queue all items from a YouTube playlist.
async def queue_playlist_async(pid):
    try:
        vids = await asyncio.to_thread(expand_playlist, pid)
    except Exception:
        vids = []
    for vid in vids:
        await queue_video_async(vid)
    mark_list_dirty()
    return len(vids)

# Clear the queue and reset indices.
def clear_queue():
    global CURRENT_INDEX, NEXT_INDEX, LAST_PROGRESS_TS, LAST_PROGRESS_TIME, LAST_PROGRESS_TOTAL, LAST_PROGRESS_INDEX, EXTERNAL_PLAYBACK, BOT_EXPECTING_WS
    with LOCK:
        QUEUE.clear()
        CURRENT_INDEX = None
        NEXT_INDEX = 0
        LAST_PROGRESS_TS = 0.0
        LAST_PROGRESS_TIME = None
        LAST_PROGRESS_TOTAL = None
        LAST_PROGRESS_INDEX = None
        EXTERNAL_PLAYBACK = False
        BOT_EXPECTING_WS = 0
        RESUME_ATTEMPTS.clear()
    mark_list_dirty()

# Remove a queue item by index with safety checks.
def delete_index(i):
    global CURRENT_INDEX, NEXT_INDEX, DISPLAY_INDEX

    with LOCK:
        # Invalid index.
        if i < 0 or i >= len(QUEUE):
            return False, "Invalid index."

        # If this title is currently shown/playing, do not delete it.
        if DISPLAY_INDEX is not None and i == DISPLAY_INDEX:
            return False, "You cannot delete the currently playing title. Use /skip or /stop first."

        # Remove the item.
        QUEUE.pop(i)

        # Adjust indices after removal.
        if DISPLAY_INDEX is not None and i < DISPLAY_INDEX:
            DISPLAY_INDEX -= 1

        if CURRENT_INDEX is not None and i < CURRENT_INDEX:
            CURRENT_INDEX -= 1

        if i < NEXT_INDEX:
            NEXT_INDEX -= 1

        mark_list_dirty()
        return True, None

# Play a specific queue index and update state.
def play_index(i):
    global CURRENT_INDEX, DISPLAY_INDEX, NEXT_INDEX, AUTOPLAY_ENABLED, EXTERNAL_PLAYBACK
    with LOCK:
        if i < 0 or i >= len(QUEUE):
            return
        CURRENT_INDEX = i
        DISPLAY_INDEX = i
        NEXT_INDEX = i + 1
        AUTOPLAY_ENABLED = True
        EXTERNAL_PLAYBACK = False
        item = QUEUE[i]
        RESUME_ATTEMPTS.clear()
    mark_list_dirty()
    play_item(item)

# Check if the requested index is already playing or starting.
def is_requested_track_already_playing(i):
    with LOCK:
        if DISPLAY_INDEX is None or i != DISPLAY_INDEX:
            return False
    if BOT_EXPECTING_WS > 0:
        return True
    return WS_PLAYING

# Go back to the previous queue item.
def back_queue():
    global CURRENT_INDEX, DISPLAY_INDEX, NEXT_INDEX, AUTOPLAY_ENABLED

    with LOCK:
        if DISPLAY_INDEX is None:
            return False
        i = max(DISPLAY_INDEX - 1, 0)

    play_index(i)
    return True


# Background loop that advances playback automatically.
def autoplay_loop():
    global CURRENT_INDEX, NEXT_INDEX, AUTOPLAY_ENABLED, DISPLAY_INDEX
    global LAST_PROGRESS_INDEX, LAST_PROGRESS_TIME, LAST_PROGRESS_TOTAL, WS_PLAYING, WS_LAST_EVENT_TS, WS_CONNECTED, WS_STATE, BOT_EXPECTING_WS

    while True:
        try:
            now = time.time()

            if not WS_CONNECTED:
                time.sleep(0.5)
                continue

            if not AUTOPLAY_ENABLED:
                time.sleep(0.5)
                continue

            # Wait for the bot-initiated WS events before acting.
            if BOT_EXPECTING_WS > 0:
                time.sleep(0.2)
                continue

            if WS_STATE == "playing":
                time.sleep(0.5)
                continue

            if WS_STATE == "paused":
                time.sleep(0.5)
                continue

            resume_pending = False
            if WS_STATE == "stopped" and DISPLAY_INDEX is not None and LAST_PROGRESS_INDEX == DISPLAY_INDEX and LAST_PROGRESS_TIME:
                remaining = None
                if LAST_PROGRESS_TOTAL:
                    cur_sec = kodi_time_seconds(LAST_PROGRESS_TIME)
                    total_sec = kodi_time_seconds(LAST_PROGRESS_TOTAL)
                    if cur_sec is not None and total_sec is not None:
                        remaining = max(total_sec - cur_sec, 0)
                if remaining is None or remaining > RESUME_MIN_REMAINING_SEC:
                    attempts = RESUME_ATTEMPTS.get(DISPLAY_INDEX, 0)
                    resume_pending = attempts < RESUME_MAX_ATTEMPTS
                    if resume_pending:
                        print(
                            f"RESUME PENDING idx={DISPLAY_INDEX} attempts={attempts} remaining={remaining}",
                            flush=True,
                        )

            # If a track marker is still present but progress stopped updating, try to resume.
            if WS_STATE == "stopped" and DISPLAY_INDEX is not None:
                if LAST_PROGRESS_INDEX == DISPLAY_INDEX and LAST_PROGRESS_TIME:
                    remaining = None
                    if LAST_PROGRESS_TOTAL:
                        cur_sec = kodi_time_seconds(LAST_PROGRESS_TIME)
                        total_sec = kodi_time_seconds(LAST_PROGRESS_TOTAL)
                        if cur_sec is not None and total_sec is not None:
                            remaining = max(total_sec - cur_sec, 0)
                    if remaining is not None and remaining <= RESUME_MIN_REMAINING_SEC:
                        # Track effectively ended; advance to next item.
                        if REPEAT_MODE == "one":
                            NEXT_INDEX = CURRENT_INDEX
                        CURRENT_INDEX = None
                        DISPLAY_INDEX = None
                        LAST_PROGRESS_TIME = None
                        LAST_PROGRESS_INDEX = None
                        LAST_PROGRESS_TOTAL = None
                        mark_list_dirty()
                        continue
                    attempts = RESUME_ATTEMPTS.get(DISPLAY_INDEX, 0)
                    if attempts < RESUME_MAX_ATTEMPTS:
                        RESUME_ATTEMPTS[DISPLAY_INDEX] = attempts + 1
                        print(
                            f"RESUME ATTEMPT idx={DISPLAY_INDEX} attempt={RESUME_ATTEMPTS[DISPLAY_INDEX]} "
                            f"remaining={remaining}",
                            flush=True,
                        )
                        with LOCK:
                            if DISPLAY_INDEX is not None and DISPLAY_INDEX < len(QUEUE):
                                item = QUEUE[DISPLAY_INDEX]
                            else:
                                item = None
                        if item:
                            resume_item_at_time(item, LAST_PROGRESS_TIME)
                            time.sleep(0.3)
                            continue
                    else:
                        # Resume attempts exhausted; treat as failed so autoplay can advance.
                        CURRENT_INDEX = None
                        DISPLAY_INDEX = None
                        mark_list_dirty()

            if resume_pending:
                time.sleep(0.3)
                continue

            if WS_STATE == "stopped":
                if CURRENT_INDEX is not None:
                    if REPEAT_MODE == "one":
                        NEXT_INDEX = CURRENT_INDEX
                    CURRENT_INDEX = None
                    time.sleep(0.3)
                    continue

                with LOCK:
                    if NEXT_INDEX < len(QUEUE):
                        CURRENT_INDEX = NEXT_INDEX
                        DISPLAY_INDEX = CURRENT_INDEX
                        item = QUEUE[CURRENT_INDEX]
                        NEXT_INDEX += 1
                        mark_list_dirty()
                    else:
                        if REPEAT_MODE == "all":
                            NEXT_INDEX = 0
                            CURRENT_INDEX = None
                            DISPLAY_INDEX = None
                        else:
                            AUTOPLAY_ENABLED = False
                            CURRENT_INDEX = None
                            DISPLAY_INDEX = None
                        item = None

                if item:
                    play_item(item)

        except Exception as e:
            print("AUTOPLAY ERROR:", e)

        time.sleep(1)


def start_autoplay_thread():
    global AUTOPLAY_THREAD_STARTED, AUTOPLAY_THREAD
    if AUTOPLAY_THREAD_STARTED and AUTOPLAY_THREAD and AUTOPLAY_THREAD.is_alive():
        return
    AUTOPLAY_THREAD_STARTED = True
    AUTOPLAY_THREAD = threading.Thread(target=autoplay_loop, daemon=True)
    AUTOPLAY_THREAD.start()

# Handle /panel command and post the control panel.
async def panel(update, ctx):
    record_last_seen(ctx, update)
    chat_id = update.effective_chat.id
    prev_id = LAST_BOT_ID.get(chat_id)
    await send_and_track(ctx, chat_id, "🎛 Kodi Remote - Current track:", reply_markup=control_panel())
    schedule_cleanup(ctx, chat_id, prev_id)
    await update_list_message(ctx, chat_id)

# Handle inline keyboard button callbacks.
async def on_button(update, ctx):
    q = update.callback_query
    await q.answer()
    cmd = q.data
    if q.message:
        LAST_SEEN_ID[update.effective_chat.id] = q.message.message_id
        print(f"SEEN chat_id={update.effective_chat.id} message_id={q.message.message_id}", flush=True)
    chat_id = update.effective_chat.id
    prev_id = LAST_BOT_ID.get(chat_id)
    sent = False

    if cmd == "skip":
        if skip_queue():
            await send_and_track(ctx, chat_id, "⏭ Next")
            sent = True
        else:
            await send_and_track(ctx, chat_id, "⏹ End of queue.")
            sent = True

    elif cmd == "back":
        if back_queue():
            await send_and_track(ctx, chat_id, "⏮ Back")
            sent = True

    elif cmd == "playpause":
        if DISPLAY_INDEX is None:
            with LOCK:
                has_queue = len(QUEUE) > 0
            if has_queue:
                play_index(0)
                await send_and_track(ctx, chat_id, "▶ Play")
            else:
                await send_and_track(ctx, chat_id, "⏹ Queue empty.")
            sent = True
        else:
            pid = get_active_playerid()
            if pid is not None:
                kodi_call("Player.PlayPause", {"playerid": pid})
                await send_and_track(ctx, chat_id, "⏯")
                sent = True
            else:
                play_index(DISPLAY_INDEX)
                await send_and_track(ctx, chat_id, "▶ Play")
                sent = True

    elif cmd == "stop":
        hard_stop_and_clear()
        await send_and_track(ctx, chat_id, "⏹ Stop")
        sent = True

    elif cmd == "repeat":
        global REPEAT_MODE
        REPEAT_MODE = {"off":"one","one":"all","all":"off"}[REPEAT_MODE]
        await send_and_track(ctx, chat_id, f"🔁 Repeat: {REPEAT_MODE}")
        sent = True

    elif cmd == "deleteall":
        clear_queue()
        await send_and_track(ctx, chat_id, "🗑 Queue cleared")
        sent = True

    elif cmd == "play:ask":
        await send_and_track(ctx, chat_id, "▶ Which number should be played? (e.g. 3)")
        ctx.user_data["await_play_index"] = True
        sent = True
    elif cmd == "delete:ask":
        await send_and_track(ctx, chat_id, "🗑 Which number should be deleted? (e.g. 3)")
        ctx.user_data["await_delete_index"] = True
        sent = True
    elif cmd == "vol:up5":
        ok = await asyncio.to_thread(run_cec_volume, 9, CEC_CMD_VOL_UP)
        await send_and_track(ctx, chat_id, "🔊 +5" if ok else "⚠ Volume +5 failed")
        sent = True
    elif cmd == "vol:up10":
        ok = await asyncio.to_thread(run_cec_volume, 18, CEC_CMD_VOL_UP)
        await send_and_track(ctx, chat_id, "🔊 +10" if ok else "⚠ Volume +10 failed")
        sent = True
    elif cmd == "vol:down5":
        ok = await asyncio.to_thread(run_cec_volume, 9, CEC_CMD_VOL_DOWN)
        await send_and_track(ctx, chat_id, "🔉 -5" if ok else "⚠ Volume -5 failed")
        sent = True
    elif cmd == "vol:down10":
        ok = await asyncio.to_thread(run_cec_volume, 18, CEC_CMD_VOL_DOWN)
        await send_and_track(ctx, chat_id, "🔉 -10" if ok else "⚠ Volume -10 failed")
        sent = True
    elif cmd == "hifi:on":
        ok = await asyncio.to_thread(run_cec_power, True)
        await send_and_track(ctx, chat_id, "🔌 Hifi On" if ok else "⚠ Hifi On failed")
        await asyncio.sleep(10)
        await refresh_hifi_status_cache(force=True)
        await update_now_playing_message(ctx, chat_id)
        sent = True
    elif cmd == "hifi:off":
        ok = await asyncio.to_thread(run_cec_power, False)
        await send_and_track(ctx, chat_id, "🔌 Hifi Off" if ok else "⚠ Hifi Off failed")
        await asyncio.sleep(10)
        await refresh_hifi_status_cache(force=True)
        await update_now_playing_message(ctx, chat_id)
        sent = True

    if sent:
        schedule_cleanup(ctx, chat_id, prev_id)
        await update_list_message(ctx, chat_id)




# Handle slash commands from chat.
async def handle_command(update, ctx):
    if not update.message or not update.message.text:
        return  # <<< VERY IMPORTANT

    record_last_seen(ctx, update)
    chat_id = update.effective_chat.id
    prev_id = LAST_BOT_ID.get(chat_id)
    sent = False
    txt = update.message.text.split()
    cmd = txt[0].split("@", 1)[0].lower()


    if cmd == "/info":
        await send_and_track(ctx, chat_id, HELP_TEXT)
        sent = True

    elif cmd == "/skip":
        if skip_queue():
            await send_and_track(ctx, chat_id, "⏭ Next")
            sent = True
        else:
            await send_and_track(ctx, chat_id, "⏹ End of queue.")
            sent = True

    elif cmd == "/back":
        if back_queue():
            await send_and_track(ctx, chat_id, "⏮ Previous track.")
            sent = True
        else:
            await send_and_track(ctx, chat_id, "No previous track.")
            sent = True

    elif cmd == "/stop":
        hard_stop_and_clear()
        await send_and_track(ctx, chat_id, "⏹ Stop")
        sent = True

    elif cmd == "/play" and len(txt) > 1 and txt[1].isdigit():
        i = int(txt[1]) - 1
        if is_requested_track_already_playing(i):
            await send_and_track(ctx, chat_id, "▶ This track is already playing.")
        else:
            play_index(i)
            await send_and_track(ctx, chat_id, f"▶ Playing track {txt[1]}.")
        sent = True

    elif cmd == "/list":
        await update_list_message(ctx, chat_id)
        sent = True

    elif cmd == "/repeat":
        global REPEAT_MODE
        REPEAT_MODE = {"off":"one","one":"all","all":"off"}[REPEAT_MODE]
        await send_and_track(ctx, chat_id, f"🔁 Repeat-Modus: {REPEAT_MODE}")
        sent = True

    elif cmd == "/delete" and len(txt) > 1 and txt[1].isdigit():
        ok, msg = delete_index(int(txt[1]) - 1)
        if ok:
            await send_and_track(ctx, chat_id, "🗑 Track deleted.")
        else:
            await send_and_track(ctx, chat_id, msg)
        sent = True

    elif cmd == "/deleteall":
        clear_queue()
        await send_and_track(ctx, chat_id, "🗑 Queue cleared.")
        sent = True

    if sent:
        schedule_cleanup(ctx, chat_id, prev_id)
        await update_list_message(ctx, chat_id)

# Handle text messages and URL inputs.
async def handle_text(update, ctx):
    record_last_seen(ctx, update)
    chat_id = update.effective_chat.id
    prev_id = LAST_BOT_ID.get(chat_id)
    sent = False
    msg_id = update.message.message_id
    txt = update.message.text.strip()

    if ctx.user_data.get("await_play_index"):
        ctx.user_data["await_play_index"] = False
        if txt.isdigit():
            i = int(txt) - 1
            if is_requested_track_already_playing(i):
                await send_and_track(ctx, chat_id, "▶ Dieser Track läuft bereits.")
            else:
                play_index(i)
                await send_and_track(ctx, chat_id, f"▶ Playing track {txt}.")
        else:
            await send_and_track(ctx, chat_id, "Please enter a number only.")
        sent = True
        if sent:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
        return
    if ctx.user_data.get("await_delete_index"):
        ctx.user_data["await_delete_index"] = False
        if txt.isdigit():
            ok, msg = delete_index(int(txt) - 1)
            if ok:
                await send_and_track(ctx, chat_id, "🗑 Track deleted.")
            else:
                await send_and_track(ctx, chat_id, msg)
        else:
            await send_and_track(ctx, chat_id, "Please enter a number only.")
        sent = True
        if sent:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
        return


    uid = update.effective_user.id
    txt = update.message.text.strip()

    if uid in pending:
        if txt.lower() == "1":
            await queue_video_async(pending[uid]["video"])
            await send_and_track(ctx, chat_id, "✔ Song added to the queue.")
            pending.pop(uid)
        elif txt.lower() == "l":
            count = await queue_playlist_async(pending[uid]["list"])
            await send_and_track(ctx, chat_id, f"✔ Playlist with {count} tracks added.")
            pending.pop(uid)
        sent = True
        if sent:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
        return

    # ---- Check SoundCloud first ----
    sc = SC.search(txt)
    if not sc:
        scs = SC_SHORT.search(txt)
        if scs:
            try:
                resolved = await asyncio.to_thread(resolve_sc_short, scs.group(0))
            except Exception:
                resolved = None
            if resolved and is_sc_track_url(resolved):
                txt = resolved
                sc = SC.search(txt)
            if not sc:
                await send_and_track(
                    ctx,
                    chat_id,
                    "❌ SoundCloud link could not be added.\n"
                    "The link points to Discover/Playlist or personal content.\n"
                    "Please send the full track link in this format:\n"
                    "https://soundcloud.com/ARTIST/TRACK"
                )
                sent = True
                if sent:
                    schedule_cleanup(ctx, chat_id, prev_id)
                    await update_list_message(ctx, chat_id)
                return
    if sc:
        try:
            item = make_soundcloud(sc.group(0))
            queue_item(item)
            await send_and_track(ctx, chat_id, "✔ SoundCloud track added to the queue.")
        except Exception as e:
            await send_and_track(ctx, chat_id, "⚠ This SoundCloud link is not playable.")
        sent = True
        if sent:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
        return

    # ---- Then YouTube ----
    vid = YT.search(txt)
    pl = PL.search(txt)

    if vid and pl:
        pending[uid] = {"video": vid.group(1), "list": pl.group(1)}
        await send_and_track(ctx, chat_id, "1 = Song, L = Playlist")
        sent = True
    elif vid:
        await queue_video_async(vid.group(1))
        await send_and_track(ctx, chat_id, "✔ Song added to the queue.")
        sent = True
    elif pl:
        count = await queue_playlist_async(pl.group(1))
        await send_and_track(ctx, chat_id, f"✔ Playlist with {count} tracks added.")
        sent = True

    if sent:
        schedule_cleanup(ctx, chat_id, prev_id)
        await update_list_message(ctx, chat_id)
        return

    # Everything else is treated as chat noise.
    await warn_and_cleanup_chat(ctx, chat_id, msg_id)


# Initialize the bot, handlers, and start polling.
def main():
    app = Application.builder().token(TOKEN).build()

    start_autoplay_thread()

    app.add_handler(CommandHandler("panel", panel))
    app.add_handler(CallbackQueryHandler(on_button))

    # IMPORTANT – re-enable your existing handlers
    app.add_handler(
        MessageHandler(filters.COMMAND & filters.TEXT, handle_command)
    )
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))

    # Post startup messages and start background refresher.
    async def _post_init(app):
        try:
            global APP_INSTANCE, MAIN_LOOP
            APP_INSTANCE = app
            MAIN_LOOP = asyncio.get_running_loop()
            STARTUP_POSTED[STARTUP_CHAT_ID] = True
            await send_info_list_panel(app, STARTUP_CHAT_ID)
            await refresh_hifi_status_cache(force=True)
        except Exception as e:
            print(f"STARTUP POST FAIL chat_id={STARTUP_CHAT_ID} err={e}", flush=True)
        asyncio.get_running_loop().create_task(list_refresher(app))
        asyncio.get_running_loop().create_task(kodi_ws_listener())
    app.post_init = _post_init

    app.run_polling()


if __name__ == "__main__":
    main()
