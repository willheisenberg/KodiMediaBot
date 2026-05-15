import asyncio
import html
import json
import logging
import os
import threading
import time
import traceback

from telegram.ext import Application, MessageHandler, filters, CallbackQueryHandler, CommandHandler
from telegram.error import RetryAfter, TimedOut, NetworkError, BadRequest

from kodibot.core import kodi_api
from kodibot.core import playlist_store
from kodibot.core import queue_state
from kodibot.core import homeassistant as ha
from kodibot.telegram import media
from kodibot.telegram import state as S
from kodibot.config import CFG

# ── Import extracted modules ─────────────────────────────────────────
from kodibot.telegram.rate import (
    telegram_request,
    telegram_request_delete,
    send_and_track,
    delete_message_if_present,
)
from kodibot.telegram.panel import (
    should_recreate_after_edit_error,
    is_not_modified_error,
    resolve_airplay_status_text,
    control_panel,
    cancel_markup,
    send_delete_confirmation,
    set_panel_menu_mode,
    save_ui_state,
    load_ui_state,
    format_item_line,
    build_list_text,
    format_link_line,
    chunk_selection_text,
    send_chunked_selection,
    send_button_selection,
    send_toast_message,
    movie_list_lines,
    show_list_lines,
    episode_list_lines,
    av_stream_label,
    current_subtitle_label,
    send_info_list_panel,
    update_list_message,
    get_now_playing_text,
    update_now_playing_message,
    refresh_hifi_status_cache,
    refresh_airplay_status_cache,
    refresh_denon_volume_cache,
)
from kodibot.telegram.ha_ui import (
    cancel_ha_menu_timeout,
    arm_ha_menu_timeout,
    touch_ha_menu_timeout,
    close_ha_menu_message,
    format_ha_state_text,
    saved_color_name,
    build_main_mini_app_url,
    build_ha_main_menu_markup,
    build_ha_preset_menu_markup,
    show_ha_menu,
    show_ha_preset_menu,
)

log = logging.getLogger(__name__)

# ── Backward-compatible aliases for globals ──────────────────────────
# Functions in this file still reference these names; they point to
# the canonical instances in telegram_state.
pending = S.pending
IMAGE_GROUPS = S.IMAGE_GROUPS
IMAGE_GROUP_TASKS = S.IMAGE_GROUP_TASKS
IMAGE_GROUP_DELAY_SECONDS = S.IMAGE_GROUP_DELAY_SECONDS

LAST_BOT_ID = S.LAST_BOT_ID
PREV_BOT_ID = S.PREV_BOT_ID
LAST_SEEN_ID = S.LAST_SEEN_ID
LAST_CLEANUP_ID = S.LAST_CLEANUP_ID
FIRST_BOT_ID = S.FIRST_BOT_ID
STARTUP_POSTED = S.STARTUP_POSTED
LIST_MSG_ID = S.LIST_MSG_ID
PANEL_MSG_ID = S.PANEL_MSG_ID
PANEL_MENU_MODE = S.PANEL_MENU_MODE
LIST_RENDER_CACHE = S.LIST_RENDER_CACHE
PANEL_RENDER_CACHE = S.PANEL_RENDER_CACHE
HA_MENU_MSG_ID = S.HA_MENU_MSG_ID

HIFI_STATUS_CACHE = S.HIFI_STATUS_CACHE
HIFI_STATUS_TS = S.HIFI_STATUS_TS
AIRPLAY_STATUS_CACHE = S.AIRPLAY_STATUS_CACHE
AIRPLAY_STATUS_TS = S.AIRPLAY_STATUS_TS
DENON_VOLUME_CACHE = S.DENON_VOLUME_CACHE
DENON_VOLUME_TS = S.DENON_VOLUME_TS

PLAYBACK_TASK_LOCK = S.PLAYBACK_TASK_LOCK
NP_REFRESH_LOCK = S.NP_REFRESH_LOCK
RESET_PANEL_LOCK = S.RESET_PANEL_LOCK
RESETTING_CHATS = S.RESETTING_CHATS
PROMPT_TIMEOUT_SECONDS = S.PROMPT_TIMEOUT_SECONDS
PROMPT_TIMEOUT_TASKS = S.PROMPT_TIMEOUT_TASKS
PENDING_TIMEOUT_TASKS = S.PENDING_TIMEOUT_TASKS
HA_MENU_TIMEOUT_SECONDS = S.HA_MENU_TIMEOUT_SECONDS
HA_MENU_TIMEOUT_TASKS = S.HA_MENU_TIMEOUT_TASKS


def remember_last_seen(chat_id, message_id):
    prev = LAST_SEEN_ID.get(chat_id)
    if prev is None or message_id > prev:
        LAST_SEEN_ID[chat_id] = message_id
    return LAST_SEEN_ID.get(chat_id)


def schedule_playback_action(ctx, chat_id, action, *args):
    async def _run():
        async with PLAYBACK_TASK_LOCK:
            try:
                await asyncio.to_thread(action, *args)
            except Exception as e:
                log.info(
                    "PLAYBACK ACTION ERROR action=%s err=%s",
                    getattr(action, "__name__", action),
                    e,
                )

    asyncio.get_running_loop().create_task(_run())


async def _refresh_playback_ui(chat_id):
    if queue_state.LIST_DIRTY:
        await update_list_message(S.APP_INSTANCE, chat_id)
    await update_now_playing_message(S.APP_INSTANCE, chat_id)


# Refresh now-playing panel from non-async contexts.
def schedule_now_playing_refresh():
    if S.APP_INSTANCE is None or S.MAIN_LOOP is None:
        return
    now = time.time()
    with S.NP_REFRESH_LOCK:
        if S.NP_REFRESH_FUTURE is not None and not S.NP_REFRESH_FUTURE.done():
            if now - S.NP_REFRESH_LAST_TS < S.NP_REFRESH_MIN_INTERVAL:
                return
        S.NP_REFRESH_LAST_TS = now
        S.NP_REFRESH_FUTURE = asyncio.run_coroutine_threadsafe(
            _refresh_playback_ui(CFG.startup_chat_id),
            S.MAIN_LOOP,
        )
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


async def request_delete_confirmation(ctx, chat_id, user_id, text, payload):
    old_msg_id = ctx.user_data.get("await_delete_confirm_msg_id")
    if old_msg_id:
        cancel_prompt_timeout(chat_id, user_id, "await_delete_confirm")
        await delete_message_if_present(ctx, chat_id, old_msg_id)

    token = str(int(time.time() * 1000))
    pending_payload = dict(payload)
    pending_payload["token"] = token
    msg_id = await send_delete_confirmation(ctx, chat_id, text, token=token)
    ctx.user_data["pending_delete"] = pending_payload
    activate_prompt(
        ctx,
        chat_id,
        user_id,
        "await_delete_confirm",
        "await_delete_confirm_msg_id",
        msg_id,
        extra_keys=("pending_delete",),
    )
    return msg_id


def queue_delete_confirmation_payload(index, success_text="🗑 Track deleted."):
    with queue_state.LOCK:
        if index < 0 or index >= len(queue_state.QUEUE):
            return None, "Invalid index."
        if queue_state.DISPLAY_INDEX is not None and index == queue_state.DISPLAY_INDEX:
            return None, "You cannot delete the currently playing title. Use /skip or /stop first."
        item = queue_state.QUEUE[index]
        title = item.get("title") or f"Track {index + 1}"
        identity = {
            "title": item.get("title"),
            "url": item.get("url"),
            "link": item.get("link"),
            "kind": item.get("kind"),
        }
    return {
        "kind": "queue_index",
        "index": index,
        "title": title,
        "identity": identity,
        "success_text": success_text,
    }, None


def queue_delete_target_matches(index, identity):
    with queue_state.LOCK:
        if index < 0 or index >= len(queue_state.QUEUE):
            return False
        item = queue_state.QUEUE[index]
        return all(item.get(key) == value for key, value in (identity or {}).items())


def media_prompt_active(user_data):
    return any(
        user_data.get(key)
        for key in (
            "await_media_type",
            "await_movie_index",
            "await_movie_start_mode",
            "await_show_index",
            "await_episode_index",
            "await_episode_start_mode",
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


async def list_refresher(ctx):
    last_np = 0.0
    last_hifi = 0.0
    last_airplay = 0.0
    last_volume = 0.0
    try:
        while True:
            try:
                if CFG.startup_chat_id in RESETTING_CHATS:
                    await asyncio.sleep(1)
                    continue
                if queue_state.LIST_DIRTY:
                    await update_list_message(ctx, CFG.startup_chat_id)
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
                    await update_now_playing_message(ctx, CFG.startup_chat_id)
                await asyncio.sleep(2)
            except Exception:
                log.error("List refresher loop error", exc_info=True)
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
        log.info(
            "SEEN chat_id=%s message_id=%s stored=%s",
            update.effective_chat.id,
            msg.message_id,
            seen_id,
        )


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
    log.info(
        "SCHEDULE CLEANUP chat_id=%s prev_id=%s end_id=%s inclusive=%s last_cleanup=%s",
        chat_id,
        prev_id,
        end_id,
        start_inclusive,
        LAST_CLEANUP_ID.get(chat_id),
    )
    if hasattr(ctx, "application"):
        ctx.application.create_task(_cleanup_after_delay(ctx, chat_id, prev_id, end_id, start_inclusive))
    elif S.MAIN_LOOP is not None:
        asyncio.run_coroutine_threadsafe(
            _cleanup_after_delay(ctx, chat_id, prev_id, end_id, start_inclusive),
            S.MAIN_LOOP,
        )
    else:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_cleanup_after_delay(ctx, chat_id, prev_id, end_id, start_inclusive))
        except RuntimeError:
            log.info("SCHEDULE CLEANUP skipped: no running event loop")


# Delete a range of messages after a delay.
async def _cleanup_after_delay(ctx, chat_id, start_id, end_id, start_inclusive):
    await asyncio.sleep(4)
    log.info(
        "RUN CLEANUP chat_id=%s start_id=%s end_id=%s inclusive=%s",
        chat_id,
        start_id,
        end_id,
        start_inclusive,
    )
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
                log.info("DELETE FAIL chat_id=%s message_id=%s err=%s", chat_id, mid, e)
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
        log.info("DELETE FAIL chat_id=%s message_id=%s err=%s", chat_id, warn.message_id, e)
    try:
        await telegram_request(ctx.bot.delete_message, chat_id=chat_id, message_id=user_msg_id)
    except Exception as e:
        log.info("DELETE FAIL chat_id=%s message_id=%s err=%s", chat_id, user_msg_id, e)


async def play_image_items(ctx, chat_id, message_ids, items):
    created_session = False
    try:
        session = await asyncio.to_thread(media.get_image_session)
        picture_active = await asyncio.to_thread(kodi_api.is_picture_player_active)
        if session and not picture_active:
            await asyncio.to_thread(media.cleanup_active_image_session)
            session = None
        if session is None:
            await asyncio.to_thread(queue_state.clear_bot_playback_state)
            session = await asyncio.to_thread(media.start_image_session, items[0])
            created_session = True
            start_index = 1
        else:
            start_index = 0
        for item in items[start_index:]:
            session = await asyncio.to_thread(media.add_image_to_session, item)
        if session["count"] > 1:
            ok = await asyncio.to_thread(kodi_api.play_picture_slideshow, session["kodi_dir"])
        else:
            kodi_image_path = media.resolve_kodi_media_path(session["image_paths"][0])
            ok = await asyncio.to_thread(kodi_api.play_picture, kodi_image_path)
        if not ok:
            ok = await asyncio.to_thread(kodi_api.wait_for_picture_player_active)
        if not ok:
            raise RuntimeError("Kodi rejected picture playback.")
    except Exception as e:
        log.info(
            "IMAGE PLAY FAIL chat_id=%s message_ids=%s count=%s err=%s",
            chat_id,
            message_ids,
            len(items),
            e,
        )
        if created_session:
            media.cleanup_active_image_session()
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


from kodibot.telegram.ui_callbacks import on_button
from kodibot.telegram.ui_text import handle_text
from kodibot.telegram.ui_commands import (
    handle_nontext,
    handle_unknown_command,
    start_command,
    reset_panel_command,
)


# Initialize the bot, handlers, and start polling.
def run(token: str):
    media.start_media_server()
    builder = Application.builder().token(token)
    telegram_base_url = CFG.telegram_base_url
    telegram_base_file_url = CFG.telegram_base_file_url
    telegram_local_mode = CFG.telegram_local_mode
    telegram_read_timeout = CFG.telegram_read_timeout
    telegram_write_timeout = CFG.telegram_write_timeout
    telegram_connect_timeout = CFG.telegram_connect_timeout
    telegram_pool_timeout = CFG.telegram_pool_timeout

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

    log.info(
        "Telegram API config local_mode=%s base_url=%s base_file_url=%s read_timeout=%s",
        telegram_local_mode,
        telegram_base_url or 'default',
        telegram_base_file_url or 'default',
        telegram_read_timeout,
    )
    log.info(
        "HA webapp config explicit=%s resolved=%s",
        CFG.ha_webapp_url or "-",
        ha.resolve_ha_webapp_url() or "-",
    )

    app = builder.build()
    load_ui_state()

    queue_state.set_ui_callbacks(schedule_now_playing_refresh)
    queue_state.register_ws_callbacks()
    queue_state.start_autoplay_thread()
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("resetpanel", reset_panel_command))

    app.add_handler(MessageHandler(filters.COMMAND, handle_unknown_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
    app.add_handler(MessageHandler(filters.ATTACHMENT | filters.Sticker.ALL, handle_nontext))
    app.add_error_handler(_error_handler)

    async def _post_init(app):
        try:
            S.APP_INSTANCE = app
            S.MAIN_LOOP = asyncio.get_running_loop()
            S.STARTUP_POSTED[CFG.startup_chat_id] = True
            await update_list_message(app, CFG.startup_chat_id)
            await update_now_playing_message(app, CFG.startup_chat_id)
            await refresh_hifi_status_cache(force=True)
            await refresh_airplay_status_cache(force=True)
            await refresh_denon_volume_cache(force=True)
            await update_now_playing_message(app, CFG.startup_chat_id)
        except Exception as e:
            log.error("Startup post fail chat_id=%s err=%s", CFG.startup_chat_id, e)
        loop = asyncio.get_running_loop()
        S.LIST_REFRESH_TASK = loop.create_task(list_refresher(app))
        S.WS_LISTENER_TASK = loop.create_task(kodi_api.kodi_ws_listener())
    app.post_init = _post_init

    async def _post_shutdown(app):
        for task in list(S.PROMPT_TIMEOUT_TASKS.values()):
            if task is not None and not task.done():
                task.cancel()
        for task in list(S.PENDING_TIMEOUT_TASKS.values()):
            if task is not None and not task.done():
                task.cancel()
        for task in list(S.IMAGE_GROUP_TASKS.values()):
            if task is not None and not task.done():
                task.cancel()
        for task in (S.LIST_REFRESH_TASK, S.WS_LISTENER_TASK):
            if task is not None and not task.done():
                task.cancel()
        for task in (S.LIST_REFRESH_TASK, S.WS_LISTENER_TASK):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
        S.LIST_REFRESH_TASK = None
        S.WS_LISTENER_TASK = None
        S.PROMPT_TIMEOUT_TASKS.clear()
        S.PENDING_TIMEOUT_TASKS.clear()
        S.IMAGE_GROUPS.clear()
        S.IMAGE_GROUP_TASKS.clear()
        S.APP_INSTANCE = None
        S.MAIN_LOOP = None
    app.post_shutdown = _post_shutdown

    app.run_polling()


async def _error_handler(update, ctx):
    err = ctx.error
    if isinstance(err, NetworkError):
        log.warning("TG network error: %s", err)
        return
    log.error("TG error: %s", err, exc_info=True)
