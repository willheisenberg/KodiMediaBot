import asyncio
import html
import os
import re
import time

from telegram.ext import Application, MessageHandler, filters, CallbackQueryHandler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter, TimedOut

import kodi_api
import playlist_store
import queue_state

STARTUP_CHAT_ID = -1003641420817
PLAYLIST_DIR = os.environ.get("PLAYLIST_DIR", "/data/playlists")

pending = {}

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

TG_RATE_LOCK = asyncio.Lock()
TG_DELETE_RATE_LOCK = asyncio.Lock()
TG_LAST_TS = 0.0
TG_DELETE_LAST_TS = 0.0
TG_MIN_INTERVAL = 0.6
TG_DELETE_MIN_INTERVAL = 1.0
TG_MAX_RETRIES = 3
TG_DYNAMIC_DELAY = 0.0
TG_DYNAMIC_UNTIL = 0.0

APP_INSTANCE = None
MAIN_LOOP = None


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


# Refresh now-playing panel from non-async contexts.
def schedule_now_playing_refresh():
    if APP_INSTANCE is None or MAIN_LOOP is None:
        return
    asyncio.run_coroutine_threadsafe(
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
            InlineKeyboardButton("🗑 All", callback_data="deleteall"),
        ],
        [
            InlineKeyboardButton("🗑 First", callback_data="delete:first"),
            InlineKeyboardButton("🗑 Last", callback_data="delete:last"),
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
        [
            InlineKeyboardButton("💾 Save", callback_data="plist:save"),
            InlineKeyboardButton("📂 Load", callback_data="plist:load"),
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


# Update or create the queue list message.
async def update_list_message(ctx, chat_id):
    msg_id = LIST_MSG_ID.get(chat_id)
    if not msg_id:
        list_msg = await send_and_track(ctx, chat_id, build_list_text(), parse_mode="HTML")
        LIST_MSG_ID[chat_id] = list_msg.message_id
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
        queue_state.LIST_DIRTY = False


# Assemble the now-playing display text.
def get_now_playing_text():
    name = None
    link = None
    with queue_state.LOCK:
        if not queue_state.EXTERNAL_PLAYBACK and queue_state.DISPLAY_INDEX is not None and 0 <= queue_state.DISPLAY_INDEX < len(queue_state.QUEUE):
            it = queue_state.QUEUE[queue_state.DISPLAY_INDEX]
            name = it.get("title") or None
            link = it.get("link")

    players = kodi_api.get_active_players()
    if not players:
        if kodi_api.WS_PLAYING and name:
            safe_name = html.escape(name, quote=False)
            if link:
                safe_link = html.escape(link, quote=True)
                return f"▶ <a href=\"{safe_link}\">{safe_name}</a>"
            return f"▶ {safe_name}"
        if kodi_api.WS_PLAYING and not name:
            return "▶ Playing..."
        queue_state.EXTERNAL_PLAYBACK = False
        if name:
            safe_name = html.escape(name, quote=False)
            if link:
                safe_link = html.escape(link, quote=True)
                return f"▶ <a href=\"{safe_link}\">{safe_name}</a>"
            return f"▶ {safe_name}"
        return "⏸ Nothing playing"

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
        return "⏸ Nothing playing"

    props = kodi_api.kodi_call(
        "Player.GetProperties",
        {"playerid": pid, "properties": ["time", "totaltime"]}
    ).get("result", {})

    if not name:
        item = kodi_api.kodi_call(
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
        ).get("result", {}).get("item", {})
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
    if link:
        safe_link = html.escape(link, quote=True)
        return f"▶ <a href=\"{safe_link}\">{safe_name}</a> | {cur} / {total}"
    return f"▶ {safe_name} | {cur} / {total}"


# Update or create the now-playing panel message.
async def update_now_playing_message(ctx, chat_id):
    msg_id = PANEL_MSG_ID.get(chat_id)
    text = get_now_playing_text()
    hifi_text = HIFI_STATUS_CACHE
    repeat_text = f"🔁 Repeat: {queue_state.REPEAT_MODE}"
    if not msg_id:
        panel_msg = await send_and_track(
            ctx,
            chat_id,
            f"🎛 Kodi Remote - Current track:\n{text}\n{hifi_text} | {repeat_text}",
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
            text=f"🎛 Kodi Remote - Current track:\n{text}\n{hifi_text} | {repeat_text}",
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
    status = await asyncio.to_thread(kodi_api.get_hifi_power_status)
    if status == "On":
        HIFI_STATUS_CACHE = "🟢 Hifi: On"
    elif status == "Standby":
        HIFI_STATUS_CACHE = "🔴 Hifi: Standby"
    HIFI_STATUS_TS = now


# Background task to refresh list and now-playing messages.
async def list_refresher(ctx):
    last_np = 0.0
    last_hifi = 0.0
    while True:
        if queue_state.LIST_DIRTY:
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
        if queue_state.skip_queue():
            await send_and_track(ctx, chat_id, "⏭ Next")
            sent = True
        else:
            await send_and_track(ctx, chat_id, "⏹ End of queue.")
            sent = True

    elif cmd == "back":
        if queue_state.back_queue():
            await send_and_track(ctx, chat_id, "⏮ Back")
            sent = True

    elif cmd == "playpause":
        if queue_state.DISPLAY_INDEX is None:
            with queue_state.LOCK:
                has_queue = len(queue_state.QUEUE) > 0
            if has_queue:
                queue_state.play_index(0)
                await send_and_track(ctx, chat_id, "▶ Play")
            else:
                await send_and_track(ctx, chat_id, "⏹ Queue empty.")
            sent = True
        else:
            pid = kodi_api.get_active_playerid()
            if pid is not None:
                kodi_api.kodi_call("Player.PlayPause", {"playerid": pid})
                await send_and_track(ctx, chat_id, "⏯")
                sent = True
            else:
                queue_state.play_index(queue_state.DISPLAY_INDEX)
                await send_and_track(ctx, chat_id, "▶ Play")
                sent = True

    elif cmd == "stop":
        queue_state.hard_stop_and_clear()
        await send_and_track(ctx, chat_id, "⏹ Stop")
        sent = True

    elif cmd.startswith("seek:"):
        if cmd == "seek:percent":
            await send_and_track(ctx, chat_id, "⏱ Percent? (0-100)")
            ctx.user_data["await_seek_percent"] = True
            sent = True
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
        await send_and_track(ctx, chat_id, "▶ Which number should be played? (e.g. 3)")
        ctx.user_data["await_play_index"] = True
        sent = True
    elif cmd == "delete:ask":
        await send_and_track(ctx, chat_id, "🗑 Which number should be deleted? (e.g. 3)")
        ctx.user_data["await_delete_index"] = True
        sent = True
    elif cmd == "plist:save":
        with queue_state.LOCK:
            has_queue = len(queue_state.QUEUE) > 0
        if not has_queue:
            await send_and_track(ctx, chat_id, "🗒 Queue is empty.")
            sent = True
        else:
            await send_and_track(ctx, chat_id, "💾 Playlist name?")
            ctx.user_data["await_playlist_save_name"] = True
            sent = True
    elif cmd == "plist:load":
        files = playlist_store.list_playlist_files(PLAYLIST_DIR)
        if not files:
            await send_and_track(ctx, chat_id, "📂 No saved playlists found.")
            sent = True
        else:
            lines = [f"{i+1}. {f}" for i, f in enumerate(files)]
            await send_and_track(ctx, chat_id, "📂 Select a playlist:\n" + "\n".join(lines))
            ctx.user_data["await_playlist_load_index"] = True
            ctx.user_data["playlist_load_files"] = files
            sent = True
    elif cmd == "vol:up5":
        ok = await asyncio.to_thread(kodi_api.run_cec_volume, 9, kodi_api.CEC_CMD_VOL_UP)
        await send_and_track(ctx, chat_id, "🔊 +5" if ok else "⚠ Volume +5 failed")
        sent = True
    elif cmd == "vol:up10":
        ok = await asyncio.to_thread(kodi_api.run_cec_volume, 18, kodi_api.CEC_CMD_VOL_UP)
        await send_and_track(ctx, chat_id, "🔊 +10" if ok else "⚠ Volume +10 failed")
        sent = True
    elif cmd == "vol:down5":
        ok = await asyncio.to_thread(kodi_api.run_cec_volume, 9, kodi_api.CEC_CMD_VOL_DOWN)
        await send_and_track(ctx, chat_id, "🔉 -5" if ok else "⚠ Volume -5 failed")
        sent = True
    elif cmd == "vol:down10":
        ok = await asyncio.to_thread(kodi_api.run_cec_volume, 18, kodi_api.CEC_CMD_VOL_DOWN)
        await send_and_track(ctx, chat_id, "🔉 -10" if ok else "⚠ Volume -10 failed")
        sent = True
    elif cmd == "hifi:on":
        ok = await asyncio.to_thread(kodi_api.run_cec_power, True)
        await send_and_track(ctx, chat_id, "🔌 Hifi On" if ok else "⚠ Hifi On failed")
        await asyncio.sleep(10)
        await refresh_hifi_status_cache(force=True)
        await update_now_playing_message(ctx, chat_id)
        sent = True
    elif cmd == "hifi:off":
        ok = await asyncio.to_thread(kodi_api.run_cec_power, False)
        await send_and_track(ctx, chat_id, "🔌 Hifi Off" if ok else "⚠ Hifi Off failed")
        await asyncio.sleep(10)
        await refresh_hifi_status_cache(force=True)
        await update_now_playing_message(ctx, chat_id)
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

    if ctx.user_data.get("await_playlist_save_name"):
        ctx.user_data["await_playlist_save_name"] = False
        with queue_state.LOCK:
            items = list(queue_state.QUEUE)
        ok, res = playlist_store.save_playlist_to_disk(PLAYLIST_DIR, txt, items)
        if ok:
            await send_and_track(ctx, chat_id, f"💾 Saved as {res}")
        else:
            await send_and_track(ctx, chat_id, f"⚠ {res}")
        sent = True
        if sent:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_playlist_load_index"):
        ctx.user_data["await_playlist_load_index"] = False
        files = ctx.user_data.pop("playlist_load_files", [])
        if txt.isdigit():
            i = int(txt) - 1
            if 0 <= i < len(files):
                ok, items = playlist_store.load_playlist_from_disk(PLAYLIST_DIR, files[i])
                if ok:
                    queue_state.clear_queue()
                    with queue_state.LOCK:
                        queue_state.QUEUE.extend(items)
                    queue_state.mark_list_dirty()
                    await send_and_track(ctx, chat_id, f"📂 Loaded {files[i]}")
                else:
                    await send_and_track(ctx, chat_id, f"⚠ {items}")
            else:
                await send_and_track(ctx, chat_id, "That number does not exist.")
        else:
            await send_and_track(ctx, chat_id, "Please enter a number only.")
        sent = True
        if sent:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_play_index"):
        ctx.user_data["await_play_index"] = False
        if txt.isdigit():
            i = int(txt) - 1
            with queue_state.LOCK:
                in_range = 0 <= i < len(queue_state.QUEUE)
            if not in_range:
                await send_and_track(ctx, chat_id, "That number does not exist.")
            elif queue_state.is_requested_track_already_playing(i):
                await send_and_track(ctx, chat_id, "▶ Dieser Track läuft bereits.")
            else:
                queue_state.play_index(i)
                await send_and_track(ctx, chat_id, f"▶ Playing track {txt}.")
        else:
            await send_and_track(ctx, chat_id, "Please enter a number only.")
        sent = True
        if sent:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
        return
    if ctx.user_data.get("await_seek_percent"):
        ctx.user_data["await_seek_percent"] = False
        m = re.match(r"^\s*(\d{1,3})\s*%?\s*$", txt)
        if m:
            val = int(m.group(1))
            if 0 <= val <= 100:
                ok = queue_state.seek_percent(val)
                await send_and_track(ctx, chat_id, "⏩ Seeked." if ok else "⚠ Seek failed.")
            else:
                await send_and_track(ctx, chat_id, "Please enter a percentage from 0 to 100.")
        else:
            await send_and_track(ctx, chat_id, "Please enter a percentage from 0 to 100.")
        sent = True
        if sent:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
        return
    if ctx.user_data.get("await_delete_index"):
        ctx.user_data["await_delete_index"] = False
        if txt.isdigit():
            ok, msg = queue_state.delete_index(int(txt) - 1)
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
            await queue_state.queue_video_async(pending[uid]["video"])
            await send_and_track(ctx, chat_id, "✔ Track added to the queue.")
            pending.pop(uid)
        elif txt.lower() == "l":
            count = await queue_state.queue_playlist_async(pending[uid]["list"])
            await send_and_track(ctx, chat_id, f"✔ Playlist with {count} tracks added.")
            pending.pop(uid)
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
        pending[uid] = {"video": vid.group(1), "list": pl.group(1)}
        await send_and_track(ctx, chat_id, "1 = Track, L = Playlist")
        sent = True
    elif vid:
        await queue_state.queue_video_async(vid.group(1))
        await send_and_track(ctx, chat_id, "✔ Track added to the queue.")
        sent = True
    elif pl:
        count = await queue_state.queue_playlist_async(pl.group(1))
        await send_and_track(ctx, chat_id, f"✔ Playlist with {count} tracks added.")
        sent = True

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
    await warn_and_cleanup_chat(ctx, update.effective_chat.id, msg.message_id)


# Initialize the bot, handlers, and start polling.
def run(token: str):
    app = Application.builder().token(token).build()

    queue_state.set_ui_callbacks(schedule_now_playing_refresh)
    queue_state.start_autoplay_thread()
    app.add_handler(CallbackQueryHandler(on_button))

    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
    app.add_handler(MessageHandler(filters.ATTACHMENT | filters.Sticker.ALL, handle_nontext))

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
        asyncio.get_running_loop().create_task(kodi_api.kodi_ws_listener())
    app.post_init = _post_init

    app.run_polling()
