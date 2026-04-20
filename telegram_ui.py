import asyncio
import html
import json
import os
import re
import threading
import time
import traceback

from telegram.ext import Application, MessageHandler, filters, CallbackQueryHandler, CommandHandler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter, TimedOut, NetworkError, BadRequest

import kodi_api
import playlist_store
import queue_state
import telegram_media

STARTUP_CHAT_ID = -1003641420817
PLAYLIST_DIR = os.environ.get("PLAYLIST_DIR", "/data/playlists")
UI_STATE_FILE = os.environ.get("UI_STATE_FILE", "/data/telegram_ui_state.json")

pending = {}
IMAGE_GROUPS = {}
IMAGE_GROUP_TASKS = {}
IMAGE_GROUP_DELAY_SECONDS = 1.2

LAST_BOT_ID = {}
PREV_BOT_ID = {}
LAST_SEEN_ID = {}
LAST_CLEANUP_ID = {}
FIRST_BOT_ID = {}
STARTUP_POSTED = {}
LIST_MSG_ID = {}
PANEL_MSG_ID = {}

HIFI_STATUS_CACHE = "⚪ Hifi: Unknown"
HIFI_STATUS_TS = 0.0
AIRPLAY_STATUS_CACHE = "AirPlay: Unknown"
AIRPLAY_STATUS_TS = 0.0
DENON_VOLUME_CACHE = "🔊 --"
DENON_VOLUME_TS = 0.0

TG_RATE_LOCK = asyncio.Lock()
TG_DELETE_RATE_LOCK = asyncio.Lock()
TG_LAST_TS = 0.0
TG_DELETE_LAST_TS = 0.0
TG_MIN_INTERVAL = 0.6
TG_DELETE_MIN_INTERVAL = 1.0
TG_MAX_RETRIES = 3
TG_DYNAMIC_DELAY = 0.0
TG_DYNAMIC_UNTIL = 0.0
PLAYBACK_TASK_LOCK = asyncio.Lock()
NP_REFRESH_LOCK = threading.Lock()
NP_REFRESH_FUTURE = None
NP_REFRESH_LAST_TS = 0.0
NP_REFRESH_MIN_INTERVAL = 0.5
RESET_PANEL_LOCK = asyncio.Lock()
RESETTING_CHATS = set()

APP_INSTANCE = None
MAIN_LOOP = None
LIST_RENDER_CACHE = {}
PANEL_RENDER_CACHE = {}
LIST_REFRESH_TASK = None
WS_LISTENER_TASK = None
PROMPT_TIMEOUT_SECONDS = 120
PROMPT_TIMEOUT_TASKS = {}
PENDING_TIMEOUT_TASKS = {}


def save_ui_state():
    payload = {"chats": {}}
    chat_ids = set(LIST_MSG_ID.keys()) | set(PANEL_MSG_ID.keys())
    for chat_id in chat_ids:
        payload["chats"][str(chat_id)] = {
            "list_msg_id": LIST_MSG_ID.get(chat_id),
            "panel_msg_id": PANEL_MSG_ID.get(chat_id),
        }
    try:
        tmp = f"{UI_STATE_FILE}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=True)
        os.replace(tmp, UI_STATE_FILE)
    except Exception as e:
        print(f"UI STATE SAVE FAIL file={UI_STATE_FILE} err={e}", flush=True)


def load_ui_state():
    try:
        with open(UI_STATE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        print(f"UI STATE LOAD FAIL file={UI_STATE_FILE} err={e}", flush=True)
        return
    chats = payload.get("chats", {})
    for chat_id_str, ids in chats.items():
        try:
            chat_id = int(chat_id_str)
        except Exception:
            continue
        list_id = ids.get("list_msg_id")
        panel_id = ids.get("panel_msg_id")
        if isinstance(list_id, int):
            LIST_MSG_ID[chat_id] = list_id
        if isinstance(panel_id, int):
            PANEL_MSG_ID[chat_id] = panel_id
    print(f"UI STATE LOADED file={UI_STATE_FILE} chats={len(chats)}", flush=True)


def remember_last_seen(chat_id, message_id):
    prev = LAST_SEEN_ID.get(chat_id)
    if prev is None or message_id > prev:
        LAST_SEEN_ID[chat_id] = message_id
    return LAST_SEEN_ID.get(chat_id)


def should_recreate_after_edit_error(err):
    if not isinstance(err, BadRequest):
        return False
    txt = str(err).lower()
    if "message is not modified" in txt:
        return False
    return ("message to edit not found" in txt) or ("message_id_invalid" in txt)


def is_not_modified_error(err):
    return isinstance(err, BadRequest) and ("message is not modified" in str(err).lower())


def resolve_airplay_status_text(status):
    if status == "On":
        return "AirPlay: On"
    if status == "Off":
        return "AirPlay: Off"
    if HIFI_STATUS_CACHE == "🔴 Hifi: Standby":
        return "AirPlay: Off"
    return "AirPlay: Unknown"


# Serialize Telegram API calls to avoid send/edit/delete collisions.
async def telegram_request(call, *args, **kwargs):
    global TG_LAST_TS, TG_DYNAMIC_DELAY, TG_DYNAMIC_UNTIL
    for _ in range(TG_MAX_RETRIES):
        async with TG_RATE_LOCK:
            now = time.time()
            extra = TG_DYNAMIC_DELAY if now < TG_DYNAMIC_UNTIL else 0.0
            wait = TG_MIN_INTERVAL + extra - (now - TG_LAST_TS)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                res = await call(*args, **kwargs)
                TG_LAST_TS = time.time()
                return res
            except RetryAfter as e:
                TG_LAST_TS = time.time()
                TG_DYNAMIC_DELAY = max(TG_DYNAMIC_DELAY, min(e.retry_after, 2.0))
                TG_DYNAMIC_UNTIL = time.time() + 60.0
                await asyncio.sleep(e.retry_after)
            except TimedOut:
                TG_LAST_TS = time.time()
                await asyncio.sleep(1.5)
            except Exception:
                TG_LAST_TS = time.time()
                raise
    async with TG_RATE_LOCK:
        now = time.time()
        extra = TG_DYNAMIC_DELAY if now < TG_DYNAMIC_UNTIL else 0.0
        wait = TG_MIN_INTERVAL + extra - (now - TG_LAST_TS)
        if wait > 0:
            await asyncio.sleep(wait)
        res = await call(*args, **kwargs)
        TG_LAST_TS = time.time()
        return res


async def telegram_request_delete(call, *args, **kwargs):
    global TG_DELETE_LAST_TS, TG_DYNAMIC_DELAY, TG_DYNAMIC_UNTIL
    for _ in range(TG_MAX_RETRIES):
        async with TG_DELETE_RATE_LOCK:
            now = time.time()
            extra = TG_DYNAMIC_DELAY if now < TG_DYNAMIC_UNTIL else 0.0
            wait = TG_DELETE_MIN_INTERVAL + extra - (now - TG_DELETE_LAST_TS)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                res = await call(*args, **kwargs)
                TG_DELETE_LAST_TS = time.time()
                return res
            except RetryAfter as e:
                TG_DELETE_LAST_TS = time.time()
                TG_DYNAMIC_DELAY = max(TG_DYNAMIC_DELAY, min(e.retry_after, 2.0))
                TG_DYNAMIC_UNTIL = time.time() + 60.0
                await asyncio.sleep(e.retry_after)
            except TimedOut:
                TG_DELETE_LAST_TS = time.time()
                await asyncio.sleep(1.5)
            except Exception:
                TG_DELETE_LAST_TS = time.time()
                raise
    async with TG_DELETE_RATE_LOCK:
        now = time.time()
        extra = TG_DYNAMIC_DELAY if now < TG_DYNAMIC_UNTIL else 0.0
        wait = TG_DELETE_MIN_INTERVAL + extra - (now - TG_DELETE_LAST_TS)
        if wait > 0:
            await asyncio.sleep(wait)
        res = await call(*args, **kwargs)
        TG_DELETE_LAST_TS = time.time()
        return res


def schedule_playback_action(ctx, chat_id, action, *args):
    async def _run():
        async with PLAYBACK_TASK_LOCK:
            try:
                await asyncio.to_thread(action, *args)
            except Exception as e:
                print(f"PLAYBACK ACTION ERROR action={getattr(action, '__name__', action)} err={e}", flush=True)

    asyncio.get_running_loop().create_task(_run())


# Refresh now-playing panel from non-async contexts.
def schedule_now_playing_refresh():
    if APP_INSTANCE is None or MAIN_LOOP is None:
        return
    global NP_REFRESH_FUTURE, NP_REFRESH_LAST_TS
    now = time.time()
    with NP_REFRESH_LOCK:
        if NP_REFRESH_FUTURE is not None and not NP_REFRESH_FUTURE.done():
            if now - NP_REFRESH_LAST_TS < NP_REFRESH_MIN_INTERVAL:
                return
        NP_REFRESH_LAST_TS = now
        NP_REFRESH_FUTURE = asyncio.run_coroutine_threadsafe(
            update_now_playing_message(APP_INSTANCE, STARTUP_CHAT_ID),
            MAIN_LOOP,
        )


# Build the inline keyboard control panel markup.
def control_panel():
    play_label = "⏸" if kodi_api.WS_STATE == "playing" else "▶"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏮", callback_data="back"),
            InlineKeyboardButton(play_label, callback_data="playpause"),
            InlineKeyboardButton("⏭", callback_data="skip"),
        ],
        [
            InlineKeyboardButton("▶ №", callback_data="play:ask"),
            InlineKeyboardButton("⏹", callback_data="stop"),
        ],
        [
            InlineKeyboardButton("-10s", callback_data="seek:-10s"),
            InlineKeyboardButton("-30s", callback_data="seek:-30s"),
            InlineKeyboardButton("+10s", callback_data="seek:+10s"),
            InlineKeyboardButton("+30s", callback_data="seek:+30s"),
        ],
        [
            InlineKeyboardButton("-1m", callback_data="seek:-1m"),
            InlineKeyboardButton("-5m", callback_data="seek:-5m"),
            InlineKeyboardButton("-10m", callback_data="seek:-10m"),
            InlineKeyboardButton("+1m", callback_data="seek:+1m"),
            InlineKeyboardButton("+5m", callback_data="seek:+5m"),
            InlineKeyboardButton("+10m", callback_data="seek:+10m"),
        ],
        [
            InlineKeyboardButton("⏱ % Seek", callback_data="seek:percent"),
            InlineKeyboardButton("🔁 Repeat", callback_data="repeat"),
        ],
        [
            InlineKeyboardButton("🗑 №", callback_data="delete:ask"),
            InlineKeyboardButton("🗑 First", callback_data="delete:first"),
            InlineKeyboardButton("🗑 Last", callback_data="delete:last"),
            InlineKeyboardButton("🗑 All", callback_data="deleteall"),
        ],
        [
            InlineKeyboardButton("🔉 -5", callback_data="vol:down5"),
            InlineKeyboardButton("🔊 +5", callback_data="vol:up5"),
            InlineKeyboardButton("🔉 -10", callback_data="vol:down10"),
            InlineKeyboardButton("🔊 +10", callback_data="vol:up10"),
        ],
        [
            InlineKeyboardButton("⭐", callback_data="fav:ask"),
            InlineKeyboardButton("🎬", callback_data="media:ask"),
            InlineKeyboardButton("🗣", callback_data="av:ask"),
        ],
        [
            InlineKeyboardButton("💾 Save", callback_data="plist:save"),
            InlineKeyboardButton("🎵 Delete", callback_data="plist:delete"),
            InlineKeyboardButton("📂 Load", callback_data="plist:load"),
        ],
        [
            InlineKeyboardButton("🔌 Hifi On", callback_data="hifi:on"),
            InlineKeyboardButton("🔌 Hifi Off", callback_data="hifi:off"),
        ],
        [
            InlineKeyboardButton("☠️ AirPlay Kill", callback_data="airplay:kill"),
        ],
    ])


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
    with queue_state.LOCK:
        if not queue_state.QUEUE:
            out = "Queue empty."
        else:
            lines = [format_item_line(i, it) for i, it in enumerate(queue_state.QUEUE)]
            out = "\n".join(lines)
    list_msg = await send_and_track(ctx, chat_id, out, parse_mode="HTML")
    LIST_MSG_ID[chat_id] = list_msg.message_id
    panel_msg = await send_and_track(ctx, chat_id, "🎛 Kodi Remote - Current track:", reply_markup=control_panel())
    PANEL_MSG_ID[chat_id] = panel_msg.message_id
    save_ui_state()


# Format a single queue item as a display line.
def format_item_line(i, it):
    mark = "▶ " if i == queue_state.DISPLAY_INDEX else ""
    title = html.escape(it.get("title", ""), quote=False)
    link = it.get("link")
    if link:
        safe_link = html.escape(link, quote=True)
        return f"{mark}{i+1}. <a href=\"{safe_link}\">{title}</a>"
    return f"{mark}{i+1}. {title}"


# Build the full queue list text for display.
def build_list_text():
    with queue_state.LOCK:
        if not queue_state.QUEUE:
            return "Queue empty."
        lines = [format_item_line(i, it) for i, it in enumerate(queue_state.QUEUE)]
        return "🎵 Playlist:\n\n" + "\n".join(lines)


async def delete_message_if_present(ctx, chat_id, message_id):
    if not message_id:
        return
    if isinstance(message_id, (list, tuple, set)):
        for mid in message_id:
            await delete_message_if_present(ctx, chat_id, mid)
        return
    try:
        await telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


def _prompt_timeout_key(chat_id, user_id, state_key):
    return (chat_id, user_id, state_key)


def cancel_prompt_timeout(chat_id, user_id, state_key):
    task = PROMPT_TIMEOUT_TASKS.pop(_prompt_timeout_key(chat_id, user_id, state_key), None)
    if task is not None and not task.done():
        task.cancel()


async def _expire_prompt_timeout(ctx, chat_id, user_id, state_key, msg_key, extra_keys, expected_message_id):
    try:
        await asyncio.sleep(PROMPT_TIMEOUT_SECONDS)
        user_data = ctx.application.user_data.get(user_id)
        if not user_data or not user_data.get(state_key):
            return
        if user_data.get(msg_key) != expected_message_id:
            return
        user_data.pop(state_key, None)
        prompt_id = user_data.pop(msg_key, None)
        for key in extra_keys or ():
            user_data.pop(key, None)
        await delete_message_if_present(ctx, chat_id, prompt_id)
    except asyncio.CancelledError:
        return
    finally:
        PROMPT_TIMEOUT_TASKS.pop(_prompt_timeout_key(chat_id, user_id, state_key), None)


def activate_prompt(ctx, chat_id, user_id, state_key, msg_key, message_id, extra_keys=None):
    ctx.user_data[state_key] = True
    ctx.user_data[msg_key] = message_id
    cancel_prompt_timeout(chat_id, user_id, state_key)
    task = ctx.application.create_task(
        _expire_prompt_timeout(ctx, chat_id, user_id, state_key, msg_key, extra_keys or (), message_id)
    )
    PROMPT_TIMEOUT_TASKS[_prompt_timeout_key(chat_id, user_id, state_key)] = task


def media_prompt_active(user_data):
    return any(
        user_data.get(key)
        for key in (
            "await_media_type",
            "await_movie_index",
            "await_show_index",
            "await_episode_index",
        )
    )


def av_prompt_active(user_data):
    return any(
        user_data.get(key)
        for key in (
            "await_av_action",
            "await_audio_index",
            "await_subtitle_index",
        )
    )


def cancel_pending_timeout(user_id):
    task = PENDING_TIMEOUT_TASKS.pop(user_id, None)
    if task is not None and not task.done():
        task.cancel()


async def _expire_pending_timeout(ctx, chat_id, user_id, expected_prompt_id):
    try:
        await asyncio.sleep(PROMPT_TIMEOUT_SECONDS)
        entry = pending.get(user_id)
        if not entry or entry.get("prompt_id") != expected_prompt_id:
            return
        pending.pop(user_id, None)
        await delete_message_if_present(ctx, chat_id, expected_prompt_id)
    except asyncio.CancelledError:
        return
    finally:
        PENDING_TIMEOUT_TASKS.pop(user_id, None)


def activate_pending_choice(ctx, chat_id, user_id, prompt_id, video_id, list_id):
    pending[user_id] = {
        "video": video_id,
        "list": list_id,
        "chat_id": chat_id,
        "prompt_id": prompt_id,
    }
    cancel_pending_timeout(user_id)
    PENDING_TIMEOUT_TASKS[user_id] = ctx.application.create_task(
        _expire_pending_timeout(ctx, chat_id, user_id, prompt_id)
    )


def format_link_line(i, title, link):
    safe_title = html.escape(title, quote=False)
    if not link:
        return f"{i}. {safe_title}"
    safe_link = html.escape(link, quote=True)
    return f"{i}. <a href=\"{safe_link}\">{safe_title}</a>"


def chunk_selection_text(header, lines, footer=None, max_len=3800):
    chunks = []
    current = header
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > max_len and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if footer:
        footer_text = f"\n{footer}" if current else footer
        if current and len(current) + len(footer_text) > max_len:
            chunks.append(current)
            current = footer
        else:
            current = f"{current}{footer_text}" if current else footer
    if current:
        chunks.append(current)
    return chunks


async def send_chunked_selection(ctx, chat_id, header, lines, footer=None):
    message_ids = []
    for chunk in chunk_selection_text(header, lines, footer=footer):
        msg = await send_and_track(ctx, chat_id, chunk, parse_mode="HTML")
        message_ids.append(msg.message_id)
    return message_ids


def movie_list_lines(movies):
    lines = []
    for i, movie in enumerate(movies, start=1):
        title = movie.get("title") or "Unknown"
        year = movie.get("year")
        if year:
            title = f"{title} ({year})"
        lines.append(format_link_line(i, title, kodi_api.build_imdb_link(movie)))
    return lines


def show_list_lines(shows):
    lines = []
    for i, show in enumerate(shows, start=1):
        title = show.get("title") or "Unknown"
        year = show.get("year")
        if year:
            title = f"{title} ({year})"
        lines.append(format_link_line(i, title, kodi_api.build_imdb_link(show)))
    return lines


def episode_list_lines(episodes):
    lines = []
    for i, episode in enumerate(episodes, start=1):
        season = episode.get("season")
        number = episode.get("episode")
        prefix = ""
        if isinstance(season, int) and isinstance(number, int):
            prefix = f"S{season:02d}E{number:02d} "
        title = f"{prefix}{episode.get('title') or 'Unknown'}".strip()
        lines.append(format_link_line(i, title, kodi_api.build_imdb_link(episode)))
    return lines


def av_stream_label(stream):
    if not isinstance(stream, dict):
        return "Unknown"
    parts = []
    language = stream.get("language")
    name = stream.get("name")
    if language:
        parts.append(str(language))
    if name and str(name) not in parts:
        parts.append(str(name))
    if stream.get("isdefault"):
        parts.append("Default")
    if not parts:
        index = stream.get("index")
        if index is not None:
            return f"Track {index}"
        return "Unknown"
    return " | ".join(parts)


def current_subtitle_label(av_state):
    if not av_state.get("subtitleenabled"):
        return "Off"
    current = av_state.get("currentsubtitle") or {}
    label = av_stream_label(current)
    return label if label else "Ein"


# Update or create the queue list message.
async def update_list_message(ctx, chat_id):
    msg_id = LIST_MSG_ID.get(chat_id)
    text = build_list_text()
    if LIST_RENDER_CACHE.get(chat_id) == text and msg_id:
        queue_state.LIST_DIRTY = False
        return
    if not msg_id:
        if PANEL_MSG_ID.get(chat_id):
            list_msg = await send_and_track(ctx, chat_id, text, parse_mode="HTML")
            LIST_MSG_ID[chat_id] = list_msg.message_id
            LIST_RENDER_CACHE[chat_id] = text
            save_ui_state()
        else:
            await send_info_list_panel(ctx, chat_id)
        return
    try:
        await telegram_request(
            ctx.bot.edit_message_text,
            chat_id=chat_id,
            message_id=msg_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
    except Exception as e:
        if is_not_modified_error(e):
            LIST_RENDER_CACHE[chat_id] = text
            queue_state.LIST_DIRTY = False
            return
        if not should_recreate_after_edit_error(e):
            print(f"LIST EDIT FAIL chat_id={chat_id} message_id={msg_id} err={e}", flush=True)
            return
        list_msg = await send_and_track(ctx, chat_id, text, parse_mode="HTML")
        LIST_MSG_ID[chat_id] = list_msg.message_id
        LIST_RENDER_CACHE[chat_id] = text
        save_ui_state()
    else:
        LIST_RENDER_CACHE[chat_id] = text
        queue_state.LIST_DIRTY = False


# Assemble the now-playing display text.
async def get_now_playing_text():
    name = None
    link = None
    with queue_state.LOCK:
        if not queue_state.EXTERNAL_PLAYBACK and queue_state.DISPLAY_INDEX is not None and 0 <= queue_state.DISPLAY_INDEX < len(queue_state.QUEUE):
            it = queue_state.QUEUE[queue_state.DISPLAY_INDEX]
            name = it.get("title") or None
            link = it.get("link")

    players = await kodi_api.kodi_call_async("Player.GetActivePlayers")
    players = (players or {}).get("result", [])
    if not players:
        if kodi_api.WS_PLAYING and name:
            safe_name = html.escape(name, quote=False)
            if link:
                safe_link = html.escape(link, quote=True)
                return f"▶ <a href=\"{safe_link}\">{safe_name}</a>", None
            return f"▶ {safe_name}", None
        if kodi_api.WS_PLAYING and not name:
            return "▶ Playing...", None
        queue_state.EXTERNAL_PLAYBACK = False
        if name:
            safe_name = html.escape(name, quote=False)
            if link:
                safe_link = html.escape(link, quote=True)
                return f"▶ <a href=\"{safe_link}\">{safe_name}</a>", None
            return f"▶ {safe_name}", None
        return "⏸ Nothing playing", None

    pid = None
    if kodi_api.LAST_WS_PLAYERID is not None:
        for p in players:
            if p.get("playerid") == kodi_api.LAST_WS_PLAYERID:
                pid = kodi_api.LAST_WS_PLAYERID
                break
    if pid is None:
        pid = kodi_api.pick_playerid(players)
    if pid is None:
        queue_state.EXTERNAL_PLAYBACK = False
        return "⏸ Nothing playing", None

    props = (await kodi_api.kodi_call_async(
        "Player.GetProperties",
        {"playerid": pid, "properties": ["time", "totaltime"]}
    )).get("result", {})

    if not name:
        item = (await kodi_api.kodi_call_async(
            "Player.GetItem",
            {
                "playerid": pid,
                "properties": [
                    "title",
                    "artist",
                    "file",
                    "showtitle",
                    "season",
                    "episode",
                    "album",
                    "channel",
                    "imdbnumber",
                    "uniqueid",
                    "year",
                    "originaltitle",
                ],
            }
        )).get("result", {}).get("item", {})
        kodi_api.maybe_cache_soundcloud_url(item.get("file"))

        ws_id = kodi_api.LAST_WS_ITEM.get("id")
        ws_type = kodi_api.LAST_WS_ITEM.get("type")
        ws_title = kodi_api.LAST_WS_ITEM.get("title")
        if kodi_api.DEBUG_WS:
            print(f"EXT ITEM fallback ws_id={ws_id} ws_type={ws_type} ws_title={ws_title}", flush=True)
        if ws_id is not None and ws_type:
            lib_item = kodi_api.fetch_library_item(ws_type, ws_id)
            if lib_item:
                lib_item["type"] = ws_type
                if item:
                    item = {**item, **lib_item}
                else:
                    item = lib_item
        if not item and ws_title:
            item = {"type": ws_type, "title": ws_title}

        with queue_state.LOCK:
            if queue_state.DISPLAY_INDEX is not None and 0 <= queue_state.DISPLAY_INDEX < len(queue_state.QUEUE):
                qitem = queue_state.QUEUE[queue_state.DISPLAY_INDEX]
            else:
                qitem = None
        if qitem and kodi_api.kodi_item_matches_queue(item, qitem):
            queue_state.EXTERNAL_PLAYBACK = False
            name = qitem.get("title") or None
            link = qitem.get("link")
        else:
            name, link = kodi_api.external_item_display(item)
        if not name:
            if kodi_api.DEBUG_WS:
                print(f"EXT ITEM unknown item={item}", flush=True)
            name = "Unknown"

    cur = kodi_api.format_kodi_time(props.get("time"))
    total = kodi_api.format_kodi_time(props.get("totaltime"))
    queue_state.LAST_PROGRESS_TS = time.time()
    queue_state.LAST_PROGRESS_TIME = props.get("time")
    queue_state.LAST_PROGRESS_TOTAL = props.get("totaltime")
    queue_state.LAST_PROGRESS_INDEX = queue_state.DISPLAY_INDEX
    safe_name = html.escape(name, quote=False)
    progress_text = f"{cur} / {total}"
    if link:
        safe_link = html.escape(link, quote=True)
        return f"▶ <a href=\"{safe_link}\">{safe_name}</a>", progress_text
    return f"▶ {safe_name}", progress_text


# Update or create the now-playing panel message.
async def update_now_playing_message(ctx, chat_id):
    msg_id = PANEL_MSG_ID.get(chat_id)
    text, progress_text = await get_now_playing_text()
    hifi_text = HIFI_STATUS_CACHE
    airplay_text = AIRPLAY_STATUS_CACHE
    repeat_text = f"🔁 Repeat: {queue_state.REPEAT_MODE}"
    status_parts = [hifi_text, airplay_text, repeat_text]
    if hifi_text != "🔴 Hifi: Standby":
        status_parts.append(DENON_VOLUME_CACHE)
    if progress_text:
        status_parts.append(f"⏱ {progress_text}")
    full_text = f"🎛 Kodi Remote - Current track:\n{text}\n{' | '.join(status_parts)}"
    panel_markup = control_panel()
    render_sig = (
        full_text,
        tuple(
            tuple((btn.text, btn.callback_data) for btn in row)
            for row in panel_markup.inline_keyboard
        ),
    )
    if PANEL_RENDER_CACHE.get(chat_id) == render_sig and msg_id:
        return
    if not msg_id:
        panel_msg = await send_and_track(
            ctx,
            chat_id,
            full_text,
            reply_markup=panel_markup,
            parse_mode="HTML",
        )
        PANEL_MSG_ID[chat_id] = panel_msg.message_id
        PANEL_RENDER_CACHE[chat_id] = render_sig
        save_ui_state()
        return
    try:
        await telegram_request(
            ctx.bot.edit_message_text,
            chat_id=chat_id,
            message_id=msg_id,
            text=full_text,
            parse_mode="HTML",
            reply_markup=panel_markup,
        )
    except Exception as e:
        if is_not_modified_error(e):
            PANEL_RENDER_CACHE[chat_id] = render_sig
            return
        if not should_recreate_after_edit_error(e):
            print(f"PANEL EDIT FAIL chat_id={chat_id} message_id={msg_id} err={e}", flush=True)
            return
        panel_msg = await send_and_track(
            ctx,
            chat_id,
            full_text,
            reply_markup=panel_markup,
            parse_mode="HTML",
        )
        PANEL_MSG_ID[chat_id] = panel_msg.message_id
        PANEL_RENDER_CACHE[chat_id] = render_sig
        save_ui_state()
    else:
        PANEL_RENDER_CACHE[chat_id] = render_sig


# Refresh cached hifi power status with throttling.
async def refresh_hifi_status_cache(force=False):
    global HIFI_STATUS_CACHE, HIFI_STATUS_TS, AIRPLAY_STATUS_CACHE
    now = time.time()
    if not force and now - HIFI_STATUS_TS < 300:
        return
    status = await asyncio.to_thread(kodi_api.get_hifi_power_status)
    if status == "On":
        HIFI_STATUS_CACHE = "🟢 Hifi: On"
    elif status == "Standby" or (status is None and bool(kodi_api.DENON_HOST)):
        HIFI_STATUS_CACHE = "🔴 Hifi: Standby"
        AIRPLAY_STATUS_CACHE = "AirPlay: Off"
    HIFI_STATUS_TS = now


async def refresh_airplay_status_cache(force=False):
    global AIRPLAY_STATUS_CACHE, AIRPLAY_STATUS_TS
    now = time.time()
    if not force and now - AIRPLAY_STATUS_TS < 15:
        return
    if HIFI_STATUS_CACHE == "🔴 Hifi: Standby":
        AIRPLAY_STATUS_CACHE = "AirPlay: Off"
        AIRPLAY_STATUS_TS = now
        return
    status = await asyncio.to_thread(kodi_api.get_airplay_status)
    AIRPLAY_STATUS_CACHE = resolve_airplay_status_text(status)
    AIRPLAY_STATUS_TS = now


async def refresh_denon_volume_cache(force=False):
    global DENON_VOLUME_CACHE, DENON_VOLUME_TS
    now = time.time()
    if not force and now - DENON_VOLUME_TS < 60:
        return
    if HIFI_STATUS_CACHE == "🔴 Hifi: Standby":
        DENON_VOLUME_CACHE = "🔊 --"
        DENON_VOLUME_TS = now
        return
    vol = await asyncio.to_thread(kodi_api.get_denon_mainzone_volume)
    if vol is None:
        DENON_VOLUME_CACHE = "🔊 --"
    else:
        try:
            rel = float(vol)
            abs_vol = max(0.0, min(98.0, rel + 80.0))
            if abs(abs_vol - round(abs_vol)) < 1e-6:
                DENON_VOLUME_CACHE = f"🔊 {int(round(abs_vol))}"
            else:
                DENON_VOLUME_CACHE = f"🔊 {abs_vol:.1f}"
        except Exception:
            DENON_VOLUME_CACHE = f"🔊 {vol}"
    DENON_VOLUME_TS = now


# Background task to refresh list and now-playing messages.
async def list_refresher(ctx):
    last_np = 0.0
    last_hifi = 0.0
    last_airplay = 0.0
    last_volume = 0.0
    try:
        while True:
            try:
                if STARTUP_CHAT_ID in RESETTING_CHATS:
                    await asyncio.sleep(1)
                    continue
                if queue_state.LIST_DIRTY:
                    await update_list_message(ctx, STARTUP_CHAT_ID)
                now = time.time()
                refresh_np = False
                if now - last_np >= 5:
                    refresh_np = True
                    last_np = now
                if now - last_hifi >= 300:
                    await refresh_hifi_status_cache(force=True)
                    refresh_np = True
                    last_hifi = now
                if now - last_airplay >= 60:
                    await refresh_airplay_status_cache(force=True)
                    refresh_np = True
                    last_airplay = now
                if now - last_volume >= 60:
                    await refresh_denon_volume_cache(force=True)
                    refresh_np = True
                    last_volume = now
                if refresh_np:
                    await update_now_playing_message(ctx, STARTUP_CHAT_ID)
                await asyncio.sleep(2)
            except Exception:
                print("LIST REFRESHER LOOP ERROR", flush=True)
                print(traceback.format_exc(), flush=True)
                await asyncio.sleep(2)
    except asyncio.CancelledError:
        return


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
        seen_id = remember_last_seen(update.effective_chat.id, msg.message_id)
        print(f"SEEN chat_id={update.effective_chat.id} message_id={msg.message_id} stored={seen_id}", flush=True)


# Schedule deletion of recent messages after a delay.
def schedule_cleanup(ctx, chat_id, prev_id):
    last_seen = LAST_SEEN_ID.get(chat_id)
    last_bot = LAST_BOT_ID.get(chat_id)
    if last_bot is None:
        return
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
    elif MAIN_LOOP is not None:
        asyncio.run_coroutine_threadsafe(
            _cleanup_after_delay(ctx, chat_id, prev_id, end_id, start_inclusive),
            MAIN_LOOP,
        )
    else:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_cleanup_after_delay(ctx, chat_id, prev_id, end_id, start_inclusive))
        except RuntimeError:
            print("SCHEDULE CLEANUP skipped: no running event loop", flush=True)


# Delete a range of messages after a delay.
async def _cleanup_after_delay(ctx, chat_id, start_id, end_id, start_inclusive):
    await asyncio.sleep(4)
    print(f"RUN CLEANUP chat_id={chat_id} start_id={start_id} end_id={end_id} inclusive={start_inclusive}", flush=True)
    if start_id is not None:
        begin = start_id if start_inclusive else start_id + 1
        for mid in range(begin, end_id + 1):
            try:
                if mid == LIST_MSG_ID.get(chat_id):
                    continue
                if mid == PANEL_MSG_ID.get(chat_id):
                    continue
                await telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=mid)
            except Exception as e:
                print(f"DELETE FAIL chat_id={chat_id} message_id={mid} err={e}", flush=True)
    prev_cleanup = LAST_CLEANUP_ID.get(chat_id)
    if prev_cleanup is None or end_id > prev_cleanup:
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


async def play_image_items(ctx, chat_id, message_ids, items):
    created_session = False
    try:
        session = await asyncio.to_thread(telegram_media.get_image_session)
        picture_active = await asyncio.to_thread(kodi_api.is_picture_player_active)
        if session and not picture_active:
            await asyncio.to_thread(telegram_media.cleanup_active_image_session)
            session = None
        if session is None:
            await asyncio.to_thread(queue_state.clear_bot_playback_state)
            session = await asyncio.to_thread(telegram_media.start_image_session, items[0])
            created_session = True
            start_index = 1
        else:
            start_index = 0
        for item in items[start_index:]:
            session = await asyncio.to_thread(telegram_media.add_image_to_session, item)
        if session["count"] > 1:
            ok = await asyncio.to_thread(kodi_api.play_picture_slideshow, session["kodi_dir"])
        else:
            kodi_image_path = telegram_media.resolve_kodi_media_path(session["image_paths"][0])
            ok = await asyncio.to_thread(kodi_api.play_picture, kodi_image_path)
        if not ok:
            ok = await asyncio.to_thread(kodi_api.wait_for_picture_player_active)
        if not ok:
            raise RuntimeError("Kodi rejected picture playback.")
    except Exception as e:
        print(
            f"IMAGE PLAY FAIL chat_id={chat_id} message_ids={message_ids} count={len(items)} err={e}",
            flush=True,
        )
        if created_session:
            telegram_media.cleanup_active_image_session()
        else:
            for item in items:
                try:
                    path = item.get("path")
                    if path and os.path.exists(path):
                        os.remove(path)
                except Exception:
                    pass
        await send_and_track(ctx, chat_id, "⚠ Image upload could not be displayed.")
        schedule_cleanup(ctx, chat_id, LAST_BOT_ID.get(chat_id))
        return
    await delete_message_if_present(ctx, chat_id, message_ids)
    await update_now_playing_message(ctx, chat_id)


async def _flush_image_group(ctx, chat_id, group_key):
    try:
        await asyncio.sleep(IMAGE_GROUP_DELAY_SECONDS)
        bucket = IMAGE_GROUPS.pop(group_key, None)
        if not bucket:
            return
        await play_image_items(ctx, chat_id, bucket["message_ids"], bucket["items"])
    finally:
        current = asyncio.current_task()
        if IMAGE_GROUP_TASKS.get(group_key) is current:
            IMAGE_GROUP_TASKS.pop(group_key, None)


# Handle inline keyboard button callbacks.
async def on_button(update, ctx):
    global AIRPLAY_STATUS_CACHE, AIRPLAY_STATUS_TS
    q = update.callback_query
    await q.answer()
    cmd = q.data
    if q.message:
        seen_id = remember_last_seen(update.effective_chat.id, q.message.message_id)
        print(f"SEEN chat_id={update.effective_chat.id} message_id={q.message.message_id} stored={seen_id}", flush=True)
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    prev_id = LAST_BOT_ID.get(chat_id)
    sent = False
    skip_cleanup = False

    if cmd == "skip":
        with queue_state.LOCK:
            has_queue = len(queue_state.QUEUE) > 0
        if not has_queue:
            await send_and_track(ctx, chat_id, "⏹ End of queue.")
            sent = True
        else:
            schedule_playback_action(ctx, chat_id, queue_state.skip_queue)
            await send_and_track(ctx, chat_id, "⏭ Next")
            sent = True

    elif cmd == "back":
        with queue_state.LOCK:
            has_queue = len(queue_state.QUEUE) > 0
        if not has_queue:
            await send_and_track(ctx, chat_id, "⏹ End of queue.")
            sent = True
        else:
            schedule_playback_action(ctx, chat_id, queue_state.back_queue)
            await send_and_track(ctx, chat_id, "⏮ Back")
            sent = True

    elif cmd == "playpause":
        pid = kodi_api.get_active_playerid()
        if pid is not None:
            schedule_playback_action(ctx, chat_id, kodi_api.kodi_call, "Player.PlayPause", {"playerid": pid})
            await send_and_track(ctx, chat_id, "⏯")
            sent = True
        else:
            with queue_state.LOCK:
                display_index = queue_state.DISPLAY_INDEX
                has_queue = len(queue_state.QUEUE) > 0
            if display_index is not None:
                schedule_playback_action(ctx, chat_id, queue_state.play_index, display_index)
                await send_and_track(ctx, chat_id, "▶ Play")
                sent = True
            elif has_queue:
                schedule_playback_action(ctx, chat_id, queue_state.play_index, 0)
                await send_and_track(ctx, chat_id, "▶ Play")
                sent = True
            else:
                await send_and_track(ctx, chat_id, "⏹ Queue empty.")
                sent = True

    elif cmd == "stop":
        schedule_playback_action(ctx, chat_id, queue_state.hard_stop_and_clear)
        await send_and_track(ctx, chat_id, "⏹ Stop")
        sent = True

    elif cmd.startswith("seek:"):
        if cmd == "seek:percent":
            if ctx.user_data.get("await_seek_percent"):
                return
            msg = await send_and_track(ctx, chat_id, "⏱ Percent? (0-100, q = cancel)")
            activate_prompt(ctx, chat_id, user_id, "await_seek_percent", "await_seek_percent_msg_id", msg.message_id)
            sent = True
            skip_cleanup = True
        else:
            delta_map = {
                "seek:-10s": -10,
                "seek:-30s": -30,
                "seek:+10s": 10,
                "seek:+30s": 30,
                "seek:-1m": -60,
                "seek:-5m": -300,
                "seek:-10m": -600,
                "seek:+1m": 60,
                "seek:+5m": 300,
                "seek:+10m": 600,
            }
            delta = delta_map.get(cmd)
            if delta is None:
                await send_and_track(ctx, chat_id, "⚠ Unknown seek.")
                sent = True
            else:
                ok = queue_state.seek_relative_seconds(delta)
                await send_and_track(ctx, chat_id, "⏩ Seeked." if ok else "⚠ Seek failed.")
                sent = True

    elif cmd == "repeat":
        queue_state.REPEAT_MODE = {"off":"one","one":"all","all":"off"}[queue_state.REPEAT_MODE]
        await send_and_track(ctx, chat_id, f"🔁 Repeat: {queue_state.REPEAT_MODE}")
        sent = True

    elif cmd == "deleteall":
        queue_state.clear_queue()
        await send_and_track(ctx, chat_id, "🗑 Queue cleared")
        sent = True

    elif cmd == "delete:first":
        ok, msg = queue_state.delete_index(0)
        if ok:
            await send_and_track(ctx, chat_id, "🗑 First track deleted.")
        else:
            await send_and_track(ctx, chat_id, msg)
        sent = True

    elif cmd == "delete:last":
        with queue_state.LOCK:
            last_idx = len(queue_state.QUEUE) - 1
        ok, msg = queue_state.delete_index(last_idx)
        if ok:
            await send_and_track(ctx, chat_id, "🗑 Last track deleted.")
        else:
            await send_and_track(ctx, chat_id, msg)
        sent = True

    elif cmd == "play:ask":
        if ctx.user_data.get("await_play_index"):
            return
        msg = await send_and_track(ctx, chat_id, "▶ Which number should be played? (e.g. 3, q = cancel)")
        activate_prompt(ctx, chat_id, user_id, "await_play_index", "await_play_msg_id", msg.message_id)
        sent = True
        skip_cleanup = True
    elif cmd == "fav:ask":
        if ctx.user_data.get("await_favourite_index"):
            return
        favourites = await asyncio.to_thread(kodi_api.get_playable_favourites)
        if not favourites:
            await send_and_track(ctx, chat_id, "⭐ No playable Kodi favourites found.")
            sent = True
        else:
            lines = [f"{i+1}. {fav['title']}" for i, fav in enumerate(favourites)]
            msg = await send_and_track(
                ctx,
                chat_id,
                "⭐ Select a Kodi favourite (q = cancel):\n" + "\n".join(lines),
            )
            ctx.user_data["favourites"] = favourites
            activate_prompt(
                ctx,
                chat_id,
                user_id,
                "await_favourite_index",
                "await_favourite_msg_id",
                msg.message_id,
                extra_keys=("favourites",),
            )
            sent = True
            skip_cleanup = True
    elif cmd == "media:ask":
        if media_prompt_active(ctx.user_data):
            return
        msg = await send_and_track(
            ctx,
            chat_id,
            "🎬 Media browser\n1. Movies\n2. Series\nq = cancel",
        )
        activate_prompt(ctx, chat_id, user_id, "await_media_type", "await_media_type_msg_id", msg.message_id)
        sent = True
        skip_cleanup = True
    elif cmd == "av:ask":
        if av_prompt_active(ctx.user_data):
            return
        av_state = await asyncio.to_thread(kodi_api.get_av_settings)
        if av_state.get("playerid") is None:
            await send_and_track(ctx, chat_id, "⚠ Nothing is currently playing.")
            sent = True
        elif av_state.get("error"):
            await send_and_track(ctx, chat_id, "⚠ Audio/subtitle information could not be loaded.")
            sent = True
        else:
            current_audio = av_stream_label(av_state.get("currentaudiostream") or {})
            current_sub = current_subtitle_label(av_state)
            msg = await send_and_track(
                ctx,
                chat_id,
                "🗣 Audio / Subtitles\n"
                "1. Change audio\n"
                "2. Change subtitles\n"
                f"Current audio: {current_audio}\n"
                f"Current subtitles: {current_sub}\n"
                "q = cancel",
            )
            activate_prompt(ctx, chat_id, user_id, "await_av_action", "await_av_action_msg_id", msg.message_id)
            sent = True
            skip_cleanup = True
    elif cmd == "delete:ask":
        if ctx.user_data.get("await_delete_index"):
            return
        msg = await send_and_track(ctx, chat_id, "🗑 Which number should be deleted? (e.g. 3, q = cancel)")
        activate_prompt(ctx, chat_id, user_id, "await_delete_index", "await_delete_msg_id", msg.message_id)
        sent = True
        skip_cleanup = True
    elif cmd == "plist:save":
        if ctx.user_data.get("await_playlist_save_name"):
            return
        with queue_state.LOCK:
            has_queue = len(queue_state.QUEUE) > 0
        if not has_queue:
            await send_and_track(ctx, chat_id, "🗒 Queue is empty.")
            sent = True
        else:
            msg = await send_and_track(ctx, chat_id, "💾 Playlist name? (q = cancel)")
            activate_prompt(ctx, chat_id, user_id, "await_playlist_save_name", "await_playlist_save_msg_id", msg.message_id)
            sent = True
            skip_cleanup = True
    elif cmd == "plist:load":
        if ctx.user_data.get("await_playlist_load_index"):
            return
        files = playlist_store.list_playlist_files(PLAYLIST_DIR)
        if not files:
            await send_and_track(ctx, chat_id, "📂 No saved playlists found.")
            sent = True
        else:
            lines = [f"{i+1}. {os.path.splitext(f)[0]}" for i, f in enumerate(files)]
            msg = await send_and_track(
                ctx,
                chat_id,
                "📂 Select a playlist (q = cancel):\n" + "\n".join(lines),
            )
            ctx.user_data["playlist_load_files"] = files
            activate_prompt(
                ctx,
                chat_id,
                user_id,
                "await_playlist_load_index",
                "await_playlist_load_msg_id",
                msg.message_id,
                extra_keys=("playlist_load_files",),
            )
            sent = True
            skip_cleanup = True
    elif cmd == "plist:delete":
        if ctx.user_data.get("await_playlist_delete_index"):
            return
        files = playlist_store.list_playlist_files(PLAYLIST_DIR)
        if not files:
            await send_and_track(ctx, chat_id, "🗑 No saved playlists found.")
            sent = True
        else:
            lines = [f"{i+1}. {os.path.splitext(f)[0]}" for i, f in enumerate(files)]
            msg = await send_and_track(
                ctx,
                chat_id,
                "🗑 Delete which playlist? (q = cancel)\n" + "\n".join(lines),
            )
            ctx.user_data["playlist_delete_files"] = files
            activate_prompt(
                ctx,
                chat_id,
                user_id,
                "await_playlist_delete_index",
                "await_playlist_delete_msg_id",
                msg.message_id,
                extra_keys=("playlist_delete_files",),
            )
            sent = True
            skip_cleanup = True
    elif cmd == "vol:up5":
        ok = await asyncio.to_thread(kodi_api.run_volume_delta, 5)
        await send_and_track(ctx, chat_id, "🔊 +5" if ok else "⚠ Volume +5 failed")
        await asyncio.sleep(0.35)
        await refresh_denon_volume_cache(force=True)
        await update_now_playing_message(ctx, chat_id)
        sent = True
    elif cmd == "vol:up10":
        ok = await asyncio.to_thread(kodi_api.run_volume_delta, 10)
        await send_and_track(ctx, chat_id, "🔊 +10" if ok else "⚠ Volume +10 failed")
        await asyncio.sleep(0.35)
        await refresh_denon_volume_cache(force=True)
        await update_now_playing_message(ctx, chat_id)
        sent = True
    elif cmd == "vol:down5":
        ok = await asyncio.to_thread(kodi_api.run_volume_delta, -5)
        await send_and_track(ctx, chat_id, "🔉 -5" if ok else "⚠ Volume -5 failed")
        await asyncio.sleep(0.35)
        await refresh_denon_volume_cache(force=True)
        await update_now_playing_message(ctx, chat_id)
        sent = True
    elif cmd == "vol:down10":
        ok = await asyncio.to_thread(kodi_api.run_volume_delta, -10)
        await send_and_track(ctx, chat_id, "🔉 -10" if ok else "⚠ Volume -10 failed")
        await asyncio.sleep(0.35)
        await refresh_denon_volume_cache(force=True)
        await update_now_playing_message(ctx, chat_id)
        sent = True
    elif cmd == "hifi:on":
        ok = await asyncio.to_thread(kodi_api.run_cec_power, True)
        await send_and_track(ctx, chat_id, "🔌 Hifi On" if ok else "⚠ Hifi On failed")
        if kodi_api.DENON_HOST:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
            skip_cleanup = True
        else:
            await asyncio.sleep(10)
        await refresh_hifi_status_cache(force=True)
        await update_now_playing_message(ctx, chat_id)
        await refresh_denon_volume_cache(force=True)
        sent = True
    elif cmd == "hifi:off":
        ok = await asyncio.to_thread(kodi_api.run_cec_power, False)
        await send_and_track(ctx, chat_id, "🔌 Hifi Off" if ok else "⚠ Hifi Off failed")
        if kodi_api.DENON_HOST:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
            skip_cleanup = True
        else:
            await asyncio.sleep(10)
        await refresh_hifi_status_cache(force=True)
        await update_now_playing_message(ctx, chat_id)
        sent = True
    elif cmd == "airplay:kill":
        ok = await asyncio.to_thread(kodi_api.run_airplay_kill)
        status = await asyncio.to_thread(kodi_api.get_airplay_status)
        AIRPLAY_STATUS_CACHE = resolve_airplay_status_text(status)
        AIRPLAY_STATUS_TS = time.time()
        status_text = AIRPLAY_STATUS_CACHE
        if ok:
            await send_and_track(ctx, chat_id, f"☠️ AirPlay Kill | {status_text}")
        else:
            await send_and_track(ctx, chat_id, f"⚠ AirPlay Kill failed | {status_text}")
        await update_now_playing_message(ctx, chat_id)
        sent = True

    if sent and not skip_cleanup:
        schedule_cleanup(ctx, chat_id, prev_id)
        await update_list_message(ctx, chat_id)


# Handle text messages and URL inputs.
async def handle_text(update, ctx):
    record_last_seen(ctx, update)
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    prev_id = LAST_BOT_ID.get(chat_id)
    sent = False
    skip_cleanup = False
    msg_id = update.message.message_id
    txt = update.message.text.strip()
    txt_lower = txt.lower()

    if ctx.user_data.get("await_media_type"):
        cancel_prompt_timeout(chat_id, user_id, "await_media_type")
        ctx.user_data["await_media_type"] = False
        prompt_id = ctx.user_data.pop("await_media_type_msg_id", None)
        await delete_message_if_present(ctx, chat_id, msg_id)
        if txt_lower == "q":
            await send_and_track(ctx, chat_id, "Cancelled.")
            sent = True
        elif txt == "1":
            movies = await asyncio.to_thread(kodi_api.list_movies)
            if not movies:
                await send_and_track(ctx, chat_id, "🎬 No movies found in Kodi.")
                sent = True
            else:
                msg_ids = await send_chunked_selection(
                    ctx,
                    chat_id,
                    "🎬 Select movie:",
                    movie_list_lines(movies),
                    footer="q = cancel",
                )
                ctx.user_data["media_movies"] = movies
                activate_prompt(
                    ctx,
                    chat_id,
                    user_id,
                    "await_movie_index",
                    "await_movie_msg_id",
                    msg_ids,
                    extra_keys=("media_movies",),
                )
                sent = True
                skip_cleanup = True
        elif txt == "2":
            shows = await asyncio.to_thread(kodi_api.list_tvshows)
            if not shows:
                await send_and_track(ctx, chat_id, "📺 No series found in Kodi.")
                sent = True
            else:
                msg_ids = await send_chunked_selection(
                    ctx,
                    chat_id,
                    "📺 Select series:",
                    show_list_lines(shows),
                    footer="q = cancel",
                )
                ctx.user_data["media_shows"] = shows
                activate_prompt(
                    ctx,
                    chat_id,
                    user_id,
                    "await_show_index",
                    "await_show_msg_id",
                    msg_ids,
                    extra_keys=("media_shows",),
                )
                sent = True
                skip_cleanup = True
        else:
            await send_and_track(ctx, chat_id, "Please enter 1 or 2 (or q to cancel).")
            sent = True
            skip_cleanup = True
        await delete_message_if_present(ctx, chat_id, prompt_id)
        if sent and not skip_cleanup:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_av_action"):
        cancel_prompt_timeout(chat_id, user_id, "await_av_action")
        ctx.user_data["await_av_action"] = False
        prompt_id = ctx.user_data.pop("await_av_action_msg_id", None)
        await delete_message_if_present(ctx, chat_id, msg_id)
        if txt_lower == "q":
            await send_and_track(ctx, chat_id, "Cancelled.")
            sent = True
        elif txt == "1":
            av_state = await asyncio.to_thread(kodi_api.get_av_settings)
            audio_streams = av_state.get("audiostreams") or []
            if av_state.get("playerid") is None:
                await send_and_track(ctx, chat_id, "⚠ Nothing is currently playing.")
                sent = True
            elif av_state.get("error"):
                await send_and_track(ctx, chat_id, "⚠ Audio streams could not be loaded.")
                sent = True
            elif not audio_streams:
                await send_and_track(ctx, chat_id, "⚠ No audio streams available.")
                sent = True
            else:
                lines = []
                current_index = (av_state.get("currentaudiostream") or {}).get("index")
                for i, stream in enumerate(audio_streams, start=1):
                    marker = " [active]" if stream.get("index") == current_index else ""
                    lines.append(f"{i}. {av_stream_label(stream)}{marker}")
                msg = await send_and_track(
                    ctx,
                    chat_id,
                    "🗣 Select audio:\n" + "\n".join(lines) + "\nq = cancel",
                )
                ctx.user_data["audio_streams"] = audio_streams
                activate_prompt(
                    ctx,
                    chat_id,
                    user_id,
                    "await_audio_index",
                    "await_audio_msg_id",
                    msg.message_id,
                    extra_keys=("audio_streams",),
                )
                sent = True
                skip_cleanup = True
        elif txt == "2":
            av_state = await asyncio.to_thread(kodi_api.get_av_settings)
            subtitles = av_state.get("subtitles") or []
            if av_state.get("playerid") is None:
                await send_and_track(ctx, chat_id, "⚠ Nothing is currently playing.")
                sent = True
            elif av_state.get("error"):
                await send_and_track(ctx, chat_id, "⚠ Subtitle streams could not be loaded.")
                sent = True
            else:
                lines = ["1. Off"]
                current_index = (av_state.get("currentsubtitle") or {}).get("index")
                for i, stream in enumerate(subtitles, start=2):
                    active = av_state.get("subtitleenabled") and stream.get("index") == current_index
                    marker = " [active]" if active else ""
                    lines.append(f"{i}. {av_stream_label(stream)}{marker}")
                msg = await send_and_track(
                    ctx,
                    chat_id,
                    "💬 Select subtitles:\n" + "\n".join(lines) + "\nq = cancel",
                )
                ctx.user_data["subtitle_streams"] = subtitles
                activate_prompt(
                    ctx,
                    chat_id,
                    user_id,
                    "await_subtitle_index",
                    "await_subtitle_msg_id",
                    msg.message_id,
                    extra_keys=("subtitle_streams",),
                )
                sent = True
                skip_cleanup = True
        else:
            await send_and_track(ctx, chat_id, "Please enter 1 or 2 (or q to cancel).")
            sent = True
            skip_cleanup = True
        await delete_message_if_present(ctx, chat_id, prompt_id)
        if sent and not skip_cleanup:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
            await update_now_playing_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_movie_index"):
        cancel_prompt_timeout(chat_id, user_id, "await_movie_index")
        ctx.user_data["await_movie_index"] = False
        prompt_id = ctx.user_data.pop("await_movie_msg_id", None)
        movies = ctx.user_data.pop("media_movies", [])
        await delete_message_if_present(ctx, chat_id, msg_id)
        if txt_lower == "q":
            await send_and_track(ctx, chat_id, "Cancelled.")
        elif txt.isdigit():
            i = int(txt) - 1
            if 0 <= i < len(movies):
                movie = movies[i]
                ok = await asyncio.to_thread(kodi_api.play_movie, movie.get("movieid"))
                if ok:
                    queue_state.clear_bot_playback_state()
                    await send_and_track(ctx, chat_id, f"🎬 Playing: {movie.get('title')}")
                else:
                    await send_and_track(ctx, chat_id, "⚠ Movie could not be played.")
            else:
                await send_and_track(ctx, chat_id, "That number does not exist.")
        else:
            await send_and_track(ctx, chat_id, "Please enter a number only (or q to cancel).")
        sent = True
        await delete_message_if_present(ctx, chat_id, prompt_id)
        if sent:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
            await update_now_playing_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_audio_index"):
        cancel_prompt_timeout(chat_id, user_id, "await_audio_index")
        ctx.user_data["await_audio_index"] = False
        prompt_id = ctx.user_data.pop("await_audio_msg_id", None)
        audio_streams = ctx.user_data.pop("audio_streams", [])
        await delete_message_if_present(ctx, chat_id, msg_id)
        if txt_lower == "q":
            await send_and_track(ctx, chat_id, "Cancelled.")
        elif txt.isdigit():
            i = int(txt) - 1
            if 0 <= i < len(audio_streams):
                ok = await asyncio.to_thread(kodi_api.set_audio_stream, audio_streams[i].get("index"))
                if ok:
                    await send_and_track(ctx, chat_id, f"🗣 Audio set: {av_stream_label(audio_streams[i])}")
                else:
                    await send_and_track(ctx, chat_id, "⚠ Audio could not be changed.")
            else:
                await send_and_track(ctx, chat_id, "That number does not exist.")
        else:
            await send_and_track(ctx, chat_id, "Please enter a number only (or q to cancel).")
        sent = True
        await delete_message_if_present(ctx, chat_id, prompt_id)
        if sent:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
            await update_now_playing_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_subtitle_index"):
        cancel_prompt_timeout(chat_id, user_id, "await_subtitle_index")
        ctx.user_data["await_subtitle_index"] = False
        prompt_id = ctx.user_data.pop("await_subtitle_msg_id", None)
        subtitles = ctx.user_data.pop("subtitle_streams", [])
        await delete_message_if_present(ctx, chat_id, msg_id)
        if txt_lower == "q":
            await send_and_track(ctx, chat_id, "Cancelled.")
        elif txt.isdigit():
            i = int(txt) - 1
            if i == 0:
                ok = await asyncio.to_thread(kodi_api.disable_subtitles)
                if ok:
                    await send_and_track(ctx, chat_id, "💬 Subtitles off.")
                else:
                    await send_and_track(ctx, chat_id, "⚠ Subtitles could not be disabled.")
            elif 0 < i <= len(subtitles):
                selected = subtitles[i - 1]
                ok = await asyncio.to_thread(kodi_api.set_subtitle_stream, selected.get("index"))
                if ok:
                    await send_and_track(ctx, chat_id, f"💬 Subtitles set: {av_stream_label(selected)}")
                else:
                    await send_and_track(ctx, chat_id, "⚠ Subtitles could not be changed.")
            else:
                await send_and_track(ctx, chat_id, "That number does not exist.")
        else:
            await send_and_track(ctx, chat_id, "Please enter a number only (or q to cancel).")
        sent = True
        await delete_message_if_present(ctx, chat_id, prompt_id)
        if sent:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
            await update_now_playing_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_show_index"):
        cancel_prompt_timeout(chat_id, user_id, "await_show_index")
        ctx.user_data["await_show_index"] = False
        prompt_id = ctx.user_data.pop("await_show_msg_id", None)
        shows = ctx.user_data.pop("media_shows", [])
        await delete_message_if_present(ctx, chat_id, msg_id)
        if txt_lower == "q":
            await send_and_track(ctx, chat_id, "Cancelled.")
            sent = True
        elif txt.isdigit():
            i = int(txt) - 1
            if 0 <= i < len(shows):
                show = shows[i]
                episodes = await asyncio.to_thread(
                    kodi_api.list_tvshow_episodes,
                    show.get("tvshowid"),
                    show.get("title") or "",
                )
                if not episodes:
                    await send_and_track(ctx, chat_id, "📺 No episodes found for this series.")
                    sent = True
                else:
                    lines = episode_list_lines(episodes)
                    lines.append(f"{len(episodes) + 1}. Play all episodes")
                    msg_ids = await send_chunked_selection(
                        ctx,
                        chat_id,
                        f"📺 {html.escape(show.get('title') or 'Serie', quote=False)}\n",
                        lines,
                    )
                    ctx.user_data["media_show"] = show
                    ctx.user_data["media_episodes"] = episodes
                    activate_prompt(
                        ctx,
                        chat_id,
                        user_id,
                        "await_episode_index",
                        "await_episode_msg_id",
                        msg_ids,
                        extra_keys=("media_show", "media_episodes"),
                    )
                    sent = True
                    skip_cleanup = True
            else:
                await send_and_track(ctx, chat_id, "That number does not exist.")
                sent = True
                skip_cleanup = True
        else:
            await send_and_track(ctx, chat_id, "Please enter a number only (or q to cancel).")
            sent = True
            skip_cleanup = True
        await delete_message_if_present(ctx, chat_id, prompt_id)
        if sent and not skip_cleanup:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
            await update_now_playing_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_episode_index"):
        cancel_prompt_timeout(chat_id, user_id, "await_episode_index")
        ctx.user_data["await_episode_index"] = False
        prompt_id = ctx.user_data.pop("await_episode_msg_id", None)
        show = ctx.user_data.pop("media_show", {})
        episodes = ctx.user_data.pop("media_episodes", [])
        await delete_message_if_present(ctx, chat_id, msg_id)
        if txt_lower == "q":
            await send_and_track(ctx, chat_id, "Cancelled.")
        elif txt.isdigit():
            i = int(txt) - 1
            if 0 <= i < len(episodes):
                episode = episodes[i]
                ok = await asyncio.to_thread(kodi_api.play_episode, episode.get("episodeid"))
                if ok:
                    queue_state.clear_bot_playback_state()
                    await send_and_track(ctx, chat_id, f"📺 Playing: {episode.get('title')}")
                else:
                    await send_and_track(ctx, chat_id, "⚠ Episode could not be played.")
            elif i == len(episodes):
                ok = await asyncio.to_thread(
                    kodi_api.play_all_episodes,
                    [episode.get("episodeid") for episode in episodes],
                )
                if ok:
                    queue_state.clear_bot_playback_state()
                    await send_and_track(ctx, chat_id, f"📺 Playing all episodes: {show.get('title')}")
                else:
                    await send_and_track(ctx, chat_id, "⚠ Episodes could not be played.")
            else:
                await send_and_track(ctx, chat_id, "That number does not exist.")
        else:
            await send_and_track(ctx, chat_id, "Please enter a number only (or q to cancel).")
        sent = True
        await delete_message_if_present(ctx, chat_id, prompt_id)
        if sent:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
            await update_now_playing_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_playlist_save_name"):
        cancel_prompt_timeout(chat_id, user_id, "await_playlist_save_name")
        ctx.user_data["await_playlist_save_name"] = False
        prompt_id = ctx.user_data.pop("await_playlist_save_msg_id", None)
        if txt_lower == "q":
            await send_and_track(ctx, chat_id, "Cancelled.")
            sent = True
            if prompt_id:
                try:
                    await telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=prompt_id)
                except Exception:
                    pass
            if sent:
                schedule_cleanup(ctx, chat_id, prev_id)
                await update_list_message(ctx, chat_id)
            return
        with queue_state.LOCK:
            items = list(queue_state.QUEUE)
        path = playlist_store.playlist_path_for_name(PLAYLIST_DIR, txt)
        if os.path.exists(path):
            msg = await send_and_track(ctx, chat_id, "Playlist already exists. Replace? (y/n, q = cancel)")
            ctx.user_data["playlist_overwrite_name"] = txt
            ctx.user_data["playlist_overwrite_items"] = items
            activate_prompt(
                ctx,
                chat_id,
                user_id,
                "await_playlist_overwrite_confirm",
                "await_playlist_overwrite_msg_id",
                msg.message_id,
                extra_keys=("playlist_overwrite_name", "playlist_overwrite_items"),
            )
            sent = True
            skip_cleanup = True
            try:
                await telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
        else:
            ok, res = playlist_store.save_playlist_to_disk(PLAYLIST_DIR, txt, items)
            if ok:
                await send_and_track(ctx, chat_id, f"💾 Saved as {os.path.splitext(res)[0]}")
            else:
                await send_and_track(ctx, chat_id, f"⚠ {res}")
            sent = True
        if prompt_id:
            try:
                await telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=prompt_id)
            except Exception:
                pass
        if sent and not skip_cleanup:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_playlist_overwrite_confirm"):
        cancel_prompt_timeout(chat_id, user_id, "await_playlist_overwrite_confirm")
        if txt_lower in ("y", "yes"):
            ctx.user_data["await_playlist_overwrite_confirm"] = False
            prompt_id = ctx.user_data.pop("await_playlist_overwrite_msg_id", None)
            name = ctx.user_data.pop("playlist_overwrite_name", "")
            items = ctx.user_data.pop("playlist_overwrite_items", [])
            ok, res = playlist_store.save_playlist_to_disk_overwrite(PLAYLIST_DIR, name, items)
            if ok:
                await send_and_track(ctx, chat_id, f"💾 Saved as {os.path.splitext(res)[0]}")
            else:
                await send_and_track(ctx, chat_id, f"⚠ {res}")
            sent = True
            if prompt_id:
                try:
                    await telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=prompt_id)
                except Exception:
                    pass
            if sent:
                schedule_cleanup(ctx, chat_id, prev_id)
                await update_list_message(ctx, chat_id)
            return
        if txt_lower in ("n", "no"):
            ctx.user_data["await_playlist_overwrite_confirm"] = False
            prompt_id = ctx.user_data.pop("await_playlist_overwrite_msg_id", None)
            ctx.user_data.pop("playlist_overwrite_name", None)
            ctx.user_data.pop("playlist_overwrite_items", None)
            await send_and_track(ctx, chat_id, "Cancelled.")
            sent = True
            if prompt_id:
                try:
                    await telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=prompt_id)
                except Exception:
                    pass
            if sent:
                schedule_cleanup(ctx, chat_id, prev_id)
                await update_list_message(ctx, chat_id)
            return
        if txt_lower == "q":
            ctx.user_data["await_playlist_overwrite_confirm"] = False
            prompt_id = ctx.user_data.pop("await_playlist_overwrite_msg_id", None)
            ctx.user_data.pop("playlist_overwrite_name", None)
            ctx.user_data.pop("playlist_overwrite_items", None)
            await send_and_track(ctx, chat_id, "Cancelled.")
            sent = True
            if prompt_id:
                try:
                    await telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=prompt_id)
                except Exception:
                    pass
            if sent:
                schedule_cleanup(ctx, chat_id, prev_id)
                await update_list_message(ctx, chat_id)
            return
        prompt_id = ctx.user_data.get("await_playlist_overwrite_msg_id")
        if prompt_id:
            activate_prompt(
                ctx,
                chat_id,
                user_id,
                "await_playlist_overwrite_confirm",
                "await_playlist_overwrite_msg_id",
                prompt_id,
                extra_keys=("playlist_overwrite_name", "playlist_overwrite_items"),
            )
        await send_and_track(ctx, chat_id, "Please answer with y or n (or q to cancel).")
        sent = True
        return

    if ctx.user_data.get("await_playlist_load_index"):
        cancel_prompt_timeout(chat_id, user_id, "await_playlist_load_index")
        ctx.user_data["await_playlist_load_index"] = False
        prompt_id = ctx.user_data.pop("await_playlist_load_msg_id", None)
        files = ctx.user_data.pop("playlist_load_files", [])
        if txt_lower == "q":
            await send_and_track(ctx, chat_id, "Cancelled.")
        elif txt.isdigit():
            i = int(txt) - 1
            if 0 <= i < len(files):
                ok, items = playlist_store.load_playlist_from_disk(PLAYLIST_DIR, files[i])
                if ok:
                    queue_state.hard_stop_and_clear()
                    queue_state.clear_queue()
                    with queue_state.LOCK:
                        queue_state.QUEUE.extend(items)
                    queue_state.mark_list_dirty()
                    await send_and_track(ctx, chat_id, f"📂 Loaded {os.path.splitext(files[i])[0]}")
                else:
                    await send_and_track(ctx, chat_id, f"⚠ {items}")
            else:
                await send_and_track(ctx, chat_id, "That number does not exist.")
        else:
            await send_and_track(ctx, chat_id, "Please enter a number only (or q to cancel).")
        sent = True
        if prompt_id:
            try:
                await telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=prompt_id)
            except Exception:
                pass
        if sent:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_playlist_delete_index"):
        cancel_prompt_timeout(chat_id, user_id, "await_playlist_delete_index")
        ctx.user_data["await_playlist_delete_index"] = False
        prompt_id = ctx.user_data.pop("await_playlist_delete_msg_id", None)
        files = ctx.user_data.pop("playlist_delete_files", [])
        if txt_lower == "q":
            await send_and_track(ctx, chat_id, "Cancelled.")
        elif txt.isdigit():
            i = int(txt) - 1
            if 0 <= i < len(files):
                ok, res = playlist_store.delete_playlist_from_disk(PLAYLIST_DIR, files[i])
                if ok:
                    await send_and_track(ctx, chat_id, f"🗑 Deleted {res}")
                else:
                    await send_and_track(ctx, chat_id, f"⚠ {res}")
            else:
                await send_and_track(ctx, chat_id, "That number does not exist.")
        else:
            await send_and_track(ctx, chat_id, "Please enter a number only (or q to cancel).")
        sent = True
        if prompt_id:
            try:
                await telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=prompt_id)
            except Exception:
                pass
        if sent:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_play_index"):
        cancel_prompt_timeout(chat_id, user_id, "await_play_index")
        ctx.user_data["await_play_index"] = False
        prompt_id = ctx.user_data.pop("await_play_msg_id", None)
        if txt_lower == "q":
            await send_and_track(ctx, chat_id, "Cancelled.")
        elif txt.isdigit():
            i = int(txt) - 1
            with queue_state.LOCK:
                in_range = 0 <= i < len(queue_state.QUEUE)
            if not in_range:
                await send_and_track(ctx, chat_id, "That number does not exist.")
            elif queue_state.is_requested_track_already_playing(i):
                await send_and_track(ctx, chat_id, "▶ This track is already playing.")
            else:
                queue_state.play_index(i)
                await send_and_track(ctx, chat_id, f"▶ Playing track {txt}.")
        else:
            await send_and_track(ctx, chat_id, "Please enter a number only (or q to cancel).")
        sent = True
        if prompt_id:
            try:
                await telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=prompt_id)
            except Exception:
                pass
        if sent:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
        return
    if ctx.user_data.get("await_favourite_index"):
        cancel_prompt_timeout(chat_id, user_id, "await_favourite_index")
        ctx.user_data["await_favourite_index"] = False
        prompt_id = ctx.user_data.pop("await_favourite_msg_id", None)
        favourites = ctx.user_data.pop("favourites", [])
        if txt_lower == "q":
            await send_and_track(ctx, chat_id, "Cancelled.")
        elif txt.isdigit():
            i = int(txt) - 1
            if 0 <= i < len(favourites):
                selected = favourites[i]
                ok = await asyncio.to_thread(kodi_api.play_favourite_target, selected.get("target"))
                if ok:
                    queue_state.clear_bot_playback_state()
                    await send_and_track(ctx, chat_id, f"⭐ Playing favourite: {selected.get('title')}")
                else:
                    await send_and_track(ctx, chat_id, "⚠ Favourite could not be played.")
            else:
                await send_and_track(ctx, chat_id, "That number does not exist.")
        else:
            await send_and_track(ctx, chat_id, "Please enter a number only (or q to cancel).")
        sent = True
        if prompt_id:
            try:
                await telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=prompt_id)
            except Exception:
                pass
        if sent:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
            await update_now_playing_message(ctx, chat_id)
        return
    if ctx.user_data.get("await_seek_percent"):
        cancel_prompt_timeout(chat_id, user_id, "await_seek_percent")
        ctx.user_data["await_seek_percent"] = False
        prompt_id = ctx.user_data.pop("await_seek_percent_msg_id", None)
        m = re.match(r"^\s*(\d{1,3})\s*%?\s*$", txt)
        if txt_lower == "q":
            await send_and_track(ctx, chat_id, "Cancelled.")
        elif m:
            val = int(m.group(1))
            if 0 <= val <= 100:
                ok = queue_state.seek_percent(val)
                await send_and_track(ctx, chat_id, "⏩ Seeked." if ok else "⚠ Seek failed.")
            else:
                await send_and_track(ctx, chat_id, "Please enter a percentage from 0 to 100 (or q to cancel).")
        else:
            await send_and_track(ctx, chat_id, "Please enter a percentage from 0 to 100 (or q to cancel).")
        sent = True
        if prompt_id:
            try:
                await telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=prompt_id)
            except Exception:
                pass
        if sent:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
        return
    if ctx.user_data.get("await_delete_index"):
        cancel_prompt_timeout(chat_id, user_id, "await_delete_index")
        ctx.user_data["await_delete_index"] = False
        prompt_id = ctx.user_data.pop("await_delete_msg_id", None)
        if txt_lower == "q":
            await send_and_track(ctx, chat_id, "Cancelled.")
        elif txt.isdigit():
            ok, msg = queue_state.delete_index(int(txt) - 1)
            if ok:
                await send_and_track(ctx, chat_id, "🗑 Track deleted.")
            else:
                await send_and_track(ctx, chat_id, msg)
        else:
            await send_and_track(ctx, chat_id, "Please enter a number only (or q to cancel).")
        sent = True
        if prompt_id:
            try:
                await telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=prompt_id)
            except Exception:
                pass
        if sent:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
        return

    uid = update.effective_user.id
    txt = update.message.text.strip()

    if uid in pending:
        cancel_pending_timeout(uid)
        if txt.lower() == "1":
            await queue_state.queue_video_async(pending[uid]["video"])
            await send_and_track(ctx, chat_id, "✔ Track added to the queue.")
            pending.pop(uid)
        elif txt.lower() == "l":
            count = await queue_state.queue_playlist_async(pending[uid]["list"])
            await send_and_track(ctx, chat_id, f"✔ Playlist with {count} tracks added.")
            pending.pop(uid)
        elif txt_lower == "q":
            pending.pop(uid, None)
            await send_and_track(ctx, chat_id, "Cancelled.")
        else:
            await send_and_track(ctx, chat_id, "Please reply with 1, l or q.")
        sent = True
        if sent:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
        return

    sc_set = kodi_api.SC_SET.search(txt)
    if sc_set and queue_state.is_sc_set_url(sc_set.group(0)):
        count = await queue_state.queue_soundcloud_set_async(sc_set.group(0))
        if count > 0:
            await send_and_track(ctx, chat_id, f"✔ SoundCloud set with {count} tracks added.")
        else:
            await send_and_track(ctx, chat_id, "⚠ This SoundCloud set could not be added.")
        sent = True
        if sent:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
        return
    sc = kodi_api.SC.search(txt)
    if not sc:
        scs = kodi_api.SC_SHORT.search(txt)
        if scs:
            try:
                resolved = await asyncio.to_thread(queue_state.resolve_sc_short, scs.group(0))
            except Exception:
                resolved = None
            if resolved and queue_state.is_sc_set_url(resolved):
                count = await queue_state.queue_soundcloud_set_async(resolved)
                if count > 0:
                    await send_and_track(ctx, chat_id, f"✔ SoundCloud set with {count} tracks added.")
                else:
                    await send_and_track(ctx, chat_id, "⚠ This SoundCloud set could not be added.")
                sent = True
                if sent:
                    schedule_cleanup(ctx, chat_id, prev_id)
                    await update_list_message(ctx, chat_id)
                return
            if resolved and queue_state.is_sc_track_url(resolved):
                txt = resolved
                sc = kodi_api.SC.search(txt)
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
            item = queue_state.make_soundcloud(sc.group(0))
            queue_state.queue_item(item)
            await send_and_track(ctx, chat_id, "✔ SoundCloud track added to the queue.")
        except Exception:
            await send_and_track(ctx, chat_id, "⚠ This SoundCloud link is not playable.")
        sent = True
        if sent:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
        return

    vid = kodi_api.YT.search(txt)
    pl = kodi_api.PL.search(txt)

    if vid and pl:
        msg = await send_and_track(ctx, chat_id, "1 = Track, L = Playlist, q = cancel")
        activate_pending_choice(ctx, chat_id, uid, msg.message_id, vid.group(1), pl.group(1))
        sent = True
    elif vid:
        await queue_state.queue_video_async(vid.group(1))
        await send_and_track(ctx, chat_id, "✔ Track added to the queue.")
        sent = True
    elif pl:
        count = await queue_state.queue_playlist_async(pl.group(1))
        await send_and_track(ctx, chat_id, f"✔ Playlist with {count} tracks added.")
        sent = True

    if not sent:
        try:
            item = await telegram_media.download_social_video_item(txt)
        except Exception as e:
            print(f"SOCIAL VIDEO DOWNLOAD FAIL chat_id={chat_id} message_id={msg_id} err={e}", flush=True)
            await send_and_track(ctx, chat_id, "⚠ Video link could not be downloaded.")
            schedule_cleanup(ctx, chat_id, prev_id)
            return
        if item is not None:
            try:
                await asyncio.to_thread(queue_state.clear_bot_playback_state)
                await asyncio.to_thread(queue_state.play_item, item)
            except Exception as e:
                print(f"SOCIAL VIDEO PLAY FAIL chat_id={chat_id} message_id={msg_id} err={e}", flush=True)
                telegram_media.cleanup_temp_media(item.get("url"))
                await send_and_track(ctx, chat_id, "⚠ Video link could not be played.")
                schedule_cleanup(ctx, chat_id, prev_id)
                return
            try:
                await telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
            await update_now_playing_message(ctx, chat_id)
            return

    if sent:
        schedule_cleanup(ctx, chat_id, prev_id)
        await update_list_message(ctx, chat_id)
        return

    await warn_and_cleanup_chat(ctx, chat_id, msg_id)


# Handle non-text messages (files, photos, videos, stickers, etc.).
async def handle_nontext(update, ctx):
    record_last_seen(ctx, update)
    msg = update.effective_message
    if not msg:
        return
    chat_id = update.effective_chat.id
    media_group_id = getattr(msg, "media_group_id", None)

    try:
        item = await telegram_media.download_media_item(ctx.bot, msg)
    except Exception as e:
        print(f"MEDIA DOWNLOAD FAIL chat_id={chat_id} message_id={msg.message_id} err={e}", flush=True)
        user_msg = getattr(e, "user_message", "⚠ Upload could not be processed.")
        await send_and_track(ctx, chat_id, user_msg)
        schedule_cleanup(ctx, chat_id, LAST_BOT_ID.get(chat_id))
        return
    if item is None:
        await warn_and_cleanup_chat(ctx, chat_id, msg.message_id)
        return

    if item.get("kind") == "image":
        if media_group_id:
            group_key = (chat_id, media_group_id)
            bucket = IMAGE_GROUPS.setdefault(group_key, {"items": [], "message_ids": []})
            bucket["items"].append(item)
            bucket["message_ids"].append(msg.message_id)
            task = IMAGE_GROUP_TASKS.pop(group_key, None)
            if task is not None and not task.done():
                task.cancel()
            IMAGE_GROUP_TASKS[group_key] = ctx.application.create_task(_flush_image_group(ctx, chat_id, group_key))
            return
        await play_image_items(ctx, chat_id, [msg.message_id], [item])
        return

    try:
        await asyncio.to_thread(queue_state.clear_bot_playback_state)
        await asyncio.to_thread(queue_state.play_item, item)
    except Exception as e:
        print(f"MEDIA PLAY FAIL chat_id={chat_id} message_id={msg.message_id} err={e}", flush=True)
        telegram_media.cleanup_temp_media(item.get("url"))
        await send_and_track(ctx, chat_id, "⚠ Upload could not be played.")
        schedule_cleanup(ctx, chat_id, LAST_BOT_ID.get(chat_id))
        return

    try:
        await telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=msg.message_id)
    except Exception:
        pass
    await update_now_playing_message(ctx, chat_id)


async def handle_unknown_command(update, ctx):
    record_last_seen(ctx, update)
    msg = update.effective_message
    if not msg:
        return
    await warn_and_cleanup_chat(ctx, update.effective_chat.id, msg.message_id)


async def reset_panel_command(update, ctx):
    async with RESET_PANEL_LOCK:
        record_last_seen(ctx, update)
        chat_id = update.effective_chat.id
        msg = update.effective_message
        RESETTING_CHATS.add(chat_id)
        try:
            old_list_id = LIST_MSG_ID.get(chat_id)
            old_panel_id = PANEL_MSG_ID.get(chat_id)

            await asyncio.to_thread(queue_state.hard_stop_and_clear)
            queue_state.clear_queue()
            pending.clear()
            for task in list(PROMPT_TIMEOUT_TASKS.values()):
                if task is not None and not task.done():
                    task.cancel()
            PROMPT_TIMEOUT_TASKS.clear()
            for task in list(PENDING_TIMEOUT_TASKS.values()):
                if task is not None and not task.done():
                    task.cancel()
            PENDING_TIMEOUT_TASKS.clear()

            try:
                for user_id in list(ctx.application.user_data.keys()):
                    ctx.application.user_data[user_id].clear()
            except Exception:
                pass

            for mid in (old_list_id, old_panel_id):
                if not mid:
                    continue
                try:
                    await telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=mid)
                except Exception as e:
                    print(f"DELETE FAIL chat_id={chat_id} message_id={mid} err={e}", flush=True)

            LAST_BOT_ID.pop(chat_id, None)
            PREV_BOT_ID.pop(chat_id, None)
            LAST_SEEN_ID.pop(chat_id, None)
            LAST_CLEANUP_ID.pop(chat_id, None)
            FIRST_BOT_ID.pop(chat_id, None)
            STARTUP_POSTED.pop(chat_id, None)
            LIST_MSG_ID.pop(chat_id, None)
            PANEL_MSG_ID.pop(chat_id, None)
            LIST_RENDER_CACHE.pop(chat_id, None)
            PANEL_RENDER_CACHE.pop(chat_id, None)
            save_ui_state()

            STARTUP_POSTED[chat_id] = True
            await send_info_list_panel(ctx, chat_id)
            await refresh_hifi_status_cache(force=True)
            await refresh_airplay_status_cache(force=True)
            await refresh_denon_volume_cache(force=True)
            await update_now_playing_message(ctx, chat_id)
        finally:
            RESETTING_CHATS.discard(chat_id)

        if msg:
            try:
                await telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=msg.message_id)
            except Exception:
                pass


# Initialize the bot, handlers, and start polling.
def run(token: str):
    telegram_media.start_media_server()
    builder = Application.builder().token(token)
    telegram_base_url = (os.environ.get("TELEGRAM_BASE_URL") or "").strip()
    telegram_base_file_url = (os.environ.get("TELEGRAM_BASE_FILE_URL") or "").strip()
    telegram_local_mode = (os.environ.get("TELEGRAM_LOCAL_MODE") or "").strip().lower() in ("1", "true", "yes", "on")
    telegram_read_timeout = float(os.environ.get("TELEGRAM_READ_TIMEOUT", "300"))
    telegram_write_timeout = float(os.environ.get("TELEGRAM_WRITE_TIMEOUT", "30"))
    telegram_connect_timeout = float(os.environ.get("TELEGRAM_CONNECT_TIMEOUT", "30"))
    telegram_pool_timeout = float(os.environ.get("TELEGRAM_POOL_TIMEOUT", "30"))

    if telegram_base_url and hasattr(builder, "base_url"):
        builder = builder.base_url(telegram_base_url)
    if telegram_base_file_url and hasattr(builder, "base_file_url"):
        builder = builder.base_file_url(telegram_base_file_url)
    if telegram_local_mode and hasattr(builder, "local_mode"):
        builder = builder.local_mode(True)
    if hasattr(builder, "read_timeout"):
        builder = builder.read_timeout(telegram_read_timeout)
    if hasattr(builder, "write_timeout"):
        builder = builder.write_timeout(telegram_write_timeout)
    if hasattr(builder, "connect_timeout"):
        builder = builder.connect_timeout(telegram_connect_timeout)
    if hasattr(builder, "pool_timeout"):
        builder = builder.pool_timeout(telegram_pool_timeout)

    print(
        "TELEGRAM API config "
        f"local_mode={telegram_local_mode} "
        f"base_url={telegram_base_url or 'default'} "
        f"base_file_url={telegram_base_file_url or 'default'} "
        f"read_timeout={telegram_read_timeout}",
        flush=True,
    )

    app = builder.build()
    load_ui_state()

    queue_state.set_ui_callbacks(schedule_now_playing_refresh)
    queue_state.start_autoplay_thread()
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(CommandHandler("resetpanel", reset_panel_command))

    app.add_handler(MessageHandler(filters.COMMAND, handle_unknown_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
    app.add_handler(MessageHandler(filters.ATTACHMENT | filters.Sticker.ALL, handle_nontext))
    app.add_error_handler(_error_handler)

    async def _post_init(app):
        try:
            global APP_INSTANCE, MAIN_LOOP, LIST_REFRESH_TASK, WS_LISTENER_TASK
            APP_INSTANCE = app
            MAIN_LOOP = asyncio.get_running_loop()
            STARTUP_POSTED[STARTUP_CHAT_ID] = True
            await update_list_message(app, STARTUP_CHAT_ID)
            await update_now_playing_message(app, STARTUP_CHAT_ID)
            await refresh_hifi_status_cache(force=True)
            await refresh_airplay_status_cache(force=True)
            await refresh_denon_volume_cache(force=True)
            await update_now_playing_message(app, STARTUP_CHAT_ID)
        except Exception as e:
            print(f"STARTUP POST FAIL chat_id={STARTUP_CHAT_ID} err={e}", flush=True)
        loop = asyncio.get_running_loop()
        LIST_REFRESH_TASK = loop.create_task(list_refresher(app))
        WS_LISTENER_TASK = loop.create_task(kodi_api.kodi_ws_listener())
    app.post_init = _post_init

    async def _post_shutdown(app):
        global LIST_REFRESH_TASK, WS_LISTENER_TASK, APP_INSTANCE, MAIN_LOOP
        for task in list(PROMPT_TIMEOUT_TASKS.values()):
            if task is not None and not task.done():
                task.cancel()
        for task in list(PENDING_TIMEOUT_TASKS.values()):
            if task is not None and not task.done():
                task.cancel()
        for task in list(IMAGE_GROUP_TASKS.values()):
            if task is not None and not task.done():
                task.cancel()
        for task in (LIST_REFRESH_TASK, WS_LISTENER_TASK):
            if task is not None and not task.done():
                task.cancel()
        for task in (LIST_REFRESH_TASK, WS_LISTENER_TASK):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
        LIST_REFRESH_TASK = None
        WS_LISTENER_TASK = None
        PROMPT_TIMEOUT_TASKS.clear()
        PENDING_TIMEOUT_TASKS.clear()
        IMAGE_GROUPS.clear()
        IMAGE_GROUP_TASKS.clear()
        APP_INSTANCE = None
        MAIN_LOOP = None
    app.post_shutdown = _post_shutdown

    app.run_polling()


async def _error_handler(update, ctx):
    err = ctx.error
    if isinstance(err, NetworkError):
        print(f"TG WARN network error: {err}", flush=True)
        return
    print(f"TG ERROR: {err}", flush=True)
    traceback.print_exception(type(err), err, err.__traceback__)
