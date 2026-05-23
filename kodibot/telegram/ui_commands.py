import asyncio

from kodibot.telegram import ui as UI


async def handle_nontext(update, ctx):
    UI.record_last_seen(ctx, update)
    msg = update.effective_message
    if not msg:
        return
    chat_id = update.effective_chat.id
    media_group_id = getattr(msg, "media_group_id", None)

    # Delete immediately from the chat in the background to make the bot feel instant!
    ctx.application.create_task(
        UI.telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=msg.message_id)
    )

    # Fast check if it's a media type we debounce (image, video, audio) before downloading
    media_info = UI.media.classify_message(msg)
    if media_info and media_info.get("kind") in ("image", "video", "audio"):
        group_key = chat_id
        bucket = UI.IMAGE_GROUPS.setdefault(group_key, {"messages": [], "message_ids": []})
        bucket["messages"].append(msg)
        bucket["message_ids"].append(msg.message_id)
        task = UI.IMAGE_GROUP_TASKS.pop(group_key, None)
        if task is not None and not task.done():
            task.cancel()
        UI.IMAGE_GROUP_TASKS[group_key] = ctx.application.create_task(UI._flush_image_group(ctx, chat_id, group_key))
        return

    try:
        item = await UI.media.download_media_item(ctx.bot, msg)
    except Exception as e:
        UI.log.info("MEDIA DOWNLOAD FAIL chat_id=%s message_id=%s err=%s", chat_id, msg.message_id, e)
        user_msg = getattr(e, "user_message", "⚠ Upload could not be processed.")
        await UI.send_and_track(ctx, chat_id, user_msg)
        UI.schedule_cleanup(ctx, chat_id, UI.LAST_BOT_ID.get(chat_id))
        return
    if item is None:
        await UI.warn_and_cleanup_chat(ctx, chat_id, msg.message_id)
        return

    try:
        await asyncio.to_thread(UI.queue_state.clear_bot_playback_state)
        await asyncio.to_thread(UI.queue_state.play_item, item)
    except Exception as e:
        UI.log.info("MEDIA PLAY FAIL chat_id=%s message_id=%s err=%s", chat_id, msg.message_id, e)
        UI.media.cleanup_temp_media(item.get("url"))
        await UI.send_and_track(ctx, chat_id, "⚠ Upload could not be played.")
        UI.schedule_cleanup(ctx, chat_id, UI.LAST_BOT_ID.get(chat_id))
        return

    await UI.update_now_playing_message(ctx, chat_id)


async def handle_unknown_command(update, ctx):
    UI.record_last_seen(ctx, update)
    msg = update.effective_message
    if not msg:
        return
    await UI.warn_and_cleanup_chat(ctx, update.effective_chat.id, msg.message_id)


async def start_command(update, ctx):
    UI.record_last_seen(ctx, update)
    chat = update.effective_chat
    msg = update.effective_message
    if chat is None:
        return

    start_param = ctx.args[0] if getattr(ctx, "args", None) else ""
    if start_param == "ha_livecolor":
        if chat.type != "private":
            sent = await UI.send_and_track(ctx, chat.id, "🔒 Please open Live Color in the private chat with the bot.")
            UI.schedule_cleanup(ctx, chat.id, sent.message_id)
            return
        if not UI.ha.ha_available():
            await UI.send_and_track(ctx, chat.id, "⚠ Home Assistant is not configured.")
            return
        state = await asyncio.to_thread(UI.ha.get_light_state)
        bot_username = getattr(ctx.bot, "username", "") or ""
        await UI.show_ha_menu(
            ctx,
            chat.id,
            chat_type="private",
            bot_username=bot_username,
            state=state,
        )
        if msg is not None:
            await UI.delete_message_if_present(ctx, chat.id, msg.message_id)
        return

    if chat.type == "private" and UI.ha.ha_available():
        state = await asyncio.to_thread(UI.ha.get_light_state)
        bot_username = getattr(ctx.bot, "username", "") or ""
        await UI.show_ha_menu(
            ctx,
            chat.id,
            chat_type="private",
            bot_username=bot_username,
            state=state,
        )
        if msg is not None:
            await UI.delete_message_if_present(ctx, chat.id, msg.message_id)
        return

    if msg is not None:
        await UI.warn_and_cleanup_chat(ctx, chat.id, msg.message_id)


async def reset_panel_command(update, ctx):
    async with UI.RESET_PANEL_LOCK:
        UI.record_last_seen(ctx, update)
        chat_id = update.effective_chat.id
        msg = update.effective_message
        UI.RESETTING_CHATS.add(chat_id)
        try:
            old_list_id = UI.LIST_MSG_ID.get(chat_id)
            old_panel_id = UI.PANEL_MSG_ID.get(chat_id)

            await asyncio.to_thread(UI.queue_state.hard_stop_and_clear)
            UI.queue_state.clear_queue()
            UI.pending.clear()
            for task in list(UI.PROMPT_TIMEOUT_TASKS.values()):
                if task is not None and not task.done():
                    task.cancel()
            UI.PROMPT_TIMEOUT_TASKS.clear()
            for task in list(UI.PENDING_TIMEOUT_TASKS.values()):
                if task is not None and not task.done():
                    task.cancel()
            UI.PENDING_TIMEOUT_TASKS.clear()

            try:
                for user_id in list(ctx.application.user_data.keys()):
                    ctx.application.user_data[user_id].clear()
            except Exception:
                pass

            for mid in (old_list_id, old_panel_id):
                if not mid:
                    continue
                try:
                    await UI.telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=mid)
                except Exception as e:
                    UI.log.info("DELETE FAIL chat_id=%s message_id=%s err=%s", chat_id, mid, e)

            UI.LAST_BOT_ID.pop(chat_id, None)
            UI.PREV_BOT_ID.pop(chat_id, None)
            UI.LAST_SEEN_ID.pop(chat_id, None)
            UI.LAST_CLEANUP_ID.pop(chat_id, None)
            UI.FIRST_BOT_ID.pop(chat_id, None)
            UI.STARTUP_POSTED.pop(chat_id, None)
            UI.LIST_MSG_ID.pop(chat_id, None)
            UI.PANEL_MSG_ID.pop(chat_id, None)
            UI.PANEL_MENU_MODE.pop(chat_id, None)
            UI.LIST_RENDER_CACHE.pop(chat_id, None)
            UI.PANEL_RENDER_CACHE.pop(chat_id, None)
            UI.save_ui_state()

            UI.STARTUP_POSTED[chat_id] = True
            await UI.send_info_list_panel(ctx, chat_id)
            await UI.refresh_hifi_status_cache(force=True)
            await UI.refresh_airplay_status_cache(force=True)
            await UI.refresh_denon_volume_cache(force=True)
            await UI.update_now_playing_message(ctx, chat_id)
        finally:
            UI.RESETTING_CHATS.discard(chat_id)

        if msg:
            try:
                await UI.telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=msg.message_id)
            except Exception:
                pass
