import asyncio
import html
import json
import logging
import os
import re
import threading
import time
import traceback

from telegram.ext import Application, MessageHandler, filters, CallbackQueryHandler, CommandHandler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter, TimedOut, NetworkError, BadRequest

from kodibot.core import kodi_api
from kodibot.core import playlist_store
from kodibot.core import queue_state
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
    save_ui_state,
    load_ui_state,
    format_item_line,
    build_list_text,
    format_link_line,
    chunk_selection_text,
    send_chunked_selection,
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
            update_now_playing_message(S.APP_INSTANCE, CFG.startup_chat_id),
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


# Handle inline keyboard button callbacks.
async def on_button(update, ctx):
    global AIRPLAY_STATUS_CACHE, AIRPLAY_STATUS_TS
    q = update.callback_query
    await q.answer()
    cmd = q.data
    if q.message:
        seen_id = remember_last_seen(update.effective_chat.id, q.message.message_id)
        log.info(
            "SEEN chat_id=%s message_id=%s stored=%s",
            update.effective_chat.id,
            q.message.message_id,
            seen_id,
        )
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
        scan_ok = await asyncio.to_thread(kodi_api.scan_video_library)
        if not scan_ok:
            await send_and_track(ctx, chat_id, "⚠ Library scan RPC failed.")
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
        files = playlist_store.list_playlist_files(CFG.playlist_dir)
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
        files = playlist_store.list_playlist_files(CFG.playlist_dir)
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
        if CFG.denon_host:
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
        if CFG.denon_host:
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
                msg = await send_and_track(
                    ctx,
                    chat_id,
                    "🎬 Start movie\n1. From Beginning\n2. Continue\nq = cancel",
                )
                ctx.user_data["media_movie"] = movie
                activate_prompt(
                    ctx,
                    chat_id,
                    user_id,
                    "await_movie_start_mode",
                    "await_movie_start_mode_msg_id",
                    msg.message_id,
                    extra_keys=("media_movie",),
                )
                skip_cleanup = True
            else:
                await send_and_track(ctx, chat_id, "That number does not exist.")
        else:
            await send_and_track(ctx, chat_id, "Please enter a number only (or q to cancel).")
        sent = True
        await delete_message_if_present(ctx, chat_id, prompt_id)
        if sent and not skip_cleanup:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
            await update_now_playing_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_movie_start_mode"):
        cancel_prompt_timeout(chat_id, user_id, "await_movie_start_mode")
        ctx.user_data["await_movie_start_mode"] = False
        prompt_id = ctx.user_data.pop("await_movie_start_mode_msg_id", None)
        movie = ctx.user_data.pop("media_movie", {})
        await delete_message_if_present(ctx, chat_id, msg_id)
        if txt_lower == "q":
            await send_and_track(ctx, chat_id, "Cancelled.")
        elif txt in ("1", "2"):
            resume = txt == "2"
            ok = await asyncio.to_thread(kodi_api.play_movie, movie.get("movieid"), resume)
            if ok:
                queue_state.clear_bot_playback_state()
                await send_and_track(ctx, chat_id, f"🎬 Playing: {movie.get('title')}")
            else:
                await send_and_track(ctx, chat_id, "⚠ Movie could not be played.")
        else:
            await send_and_track(ctx, chat_id, "Please enter 1 or 2 (or q to cancel).")
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
                msg = await send_and_track(
                    ctx,
                    chat_id,
                    "📺 Start episode\n1. From Beginning\n2. Continue\nq = cancel",
                )
                ctx.user_data["media_episode"] = episode
                activate_prompt(
                    ctx,
                    chat_id,
                    user_id,
                    "await_episode_start_mode",
                    "await_episode_start_mode_msg_id",
                    msg.message_id,
                    extra_keys=("media_episode",),
                )
                skip_cleanup = True
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
        if sent and not skip_cleanup:
            schedule_cleanup(ctx, chat_id, prev_id)
            await update_list_message(ctx, chat_id)
            await update_now_playing_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_episode_start_mode"):
        cancel_prompt_timeout(chat_id, user_id, "await_episode_start_mode")
        ctx.user_data["await_episode_start_mode"] = False
        prompt_id = ctx.user_data.pop("await_episode_start_mode_msg_id", None)
        episode = ctx.user_data.pop("media_episode", {})
        await delete_message_if_present(ctx, chat_id, msg_id)
        if txt_lower == "q":
            await send_and_track(ctx, chat_id, "Cancelled.")
        elif txt in ("1", "2"):
            resume = txt == "2"
            ok = await asyncio.to_thread(kodi_api.play_episode, episode.get("episodeid"), resume)
            if ok:
                queue_state.clear_bot_playback_state()
                await send_and_track(ctx, chat_id, f"📺 Playing: {episode.get('title')}")
            else:
                await send_and_track(ctx, chat_id, "⚠ Episode could not be played.")
        else:
            await send_and_track(ctx, chat_id, "Please enter 1 or 2 (or q to cancel).")
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
        path = playlist_store.playlist_path_for_name(CFG.playlist_dir, txt)
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
            ok, res = playlist_store.save_playlist_to_disk(CFG.playlist_dir, txt, items)
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
            ok, res = playlist_store.save_playlist_to_disk_overwrite(CFG.playlist_dir, name, items)
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
                ok, items = playlist_store.load_playlist_from_disk(CFG.playlist_dir, files[i])
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
                ok, res = playlist_store.delete_playlist_from_disk(CFG.playlist_dir, files[i])
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
            item = await media.download_social_video_item(txt)
        except Exception as e:
            log.info("SOCIAL VIDEO DOWNLOAD FAIL chat_id=%s message_id=%s err=%s", chat_id, msg_id, e)
            await send_and_track(ctx, chat_id, "⚠ Video link could not be downloaded.")
            schedule_cleanup(ctx, chat_id, prev_id)
            return
        if item is not None:
            try:
                await asyncio.to_thread(queue_state.clear_bot_playback_state)
                await asyncio.to_thread(queue_state.play_item, item)
            except Exception as e:
                log.info("SOCIAL VIDEO PLAY FAIL chat_id=%s message_id=%s err=%s", chat_id, msg_id, e)
                media.cleanup_temp_media(item.get("url"))
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
        item = await media.download_media_item(ctx.bot, msg)
    except Exception as e:
        log.info("MEDIA DOWNLOAD FAIL chat_id=%s message_id=%s err=%s", chat_id, msg.message_id, e)
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
        log.info("MEDIA PLAY FAIL chat_id=%s message_id=%s err=%s", chat_id, msg.message_id, e)
        media.cleanup_temp_media(item.get("url"))
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
                    log.info("DELETE FAIL chat_id=%s message_id=%s err=%s", chat_id, mid, e)

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

    app = builder.build()
    load_ui_state()

    queue_state.set_ui_callbacks(schedule_now_playing_refresh)
    queue_state.register_ws_callbacks()
    queue_state.start_autoplay_thread()
    app.add_handler(CallbackQueryHandler(on_button))
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
