import asyncio
import os
import time

from kodibot.telegram import ui as UI
from kodibot.core import radio_browser


async def _refresh_ha_menu(ctx, chat_id, update):
    """Re-fetch the light state and update the HA menu message in-place."""
    q = getattr(update, "callback_query", None)
    msg_id = q.message.message_id if q and q.message else None
    if not msg_id:
        return
    chat_type = getattr(update.effective_chat, "type", "") or ""
    bot_username = getattr(ctx.bot, "username", "") or ""
    state = await asyncio.to_thread(UI.ha.get_light_state)
    await UI.show_ha_menu(
        ctx,
        chat_id,
        chat_type=chat_type,
        bot_username=bot_username,
        state=state,
        edit_message_id=msg_id,
    )


def _forget_prompt(ctx, chat_id, user_id, state_key, msg_key, *extra_keys):
    UI.cancel_prompt_timeout(chat_id, user_id, state_key)
    ctx.user_data[state_key] = False
    ctx.user_data.pop(msg_key, None)
    for key in extra_keys:
        ctx.user_data.pop(key, None)


async def _execute_pending_delete(ctx, chat_id, pending_delete):
    kind = (pending_delete or {}).get("kind")

    if kind == "queue_all":
        UI.queue_state.clear_queue()
        return "🗑 Queue cleared", False

    if kind == "queue_index":
        index = pending_delete.get("index")
        if not isinstance(index, int):
            return "Invalid index.", True
        identity = pending_delete.get("identity")
        if identity and not UI.queue_delete_target_matches(index, identity):
            return "⚠ Queue changed. Delete cancelled.", True
        ok, msg = UI.queue_state.delete_index(index)
        if ok:
            return pending_delete.get("success_text") or "🗑 Track deleted.", False
        return msg or "⚠ Track could not be deleted.", True

    if kind == "playlist_file":
        filename = pending_delete.get("filename")
        if not filename:
            return "⚠ Playlist not found.", True
        ok, res = await asyncio.to_thread(
            UI.playlist_store.delete_playlist_from_disk,
            UI.CFG.playlist_dir,
            filename,
        )
        return (f"🗑 Deleted: {res}" if ok else f"⚠ {res}"), True

    if kind == "favourite":
        title = pending_delete.get("title")
        if not title:
            return "⚠ Favourite not found.", True
        ok = await asyncio.to_thread(UI.kodi_api.remove_favourite, title)
        return (f"🗑 Deleted favourite: {title}" if ok else "⚠ Favourite could not be deleted."), True

    if kind == "ha_color":
        color_name = pending_delete.get("name", "")
        label = pending_delete.get("label") or color_name or "?"
        if not color_name:
            return "⚠ Color not found.", True
        ok = await asyncio.to_thread(UI.ha.delete_saved_color, color_name)
        if ok:
            menu_message_id = UI.HA_MENU_MSG_ID.get(chat_id)
            if menu_message_id:
                await UI.show_ha_preset_menu(ctx, chat_id, edit_message_id=menu_message_id)
            return f"🗑 Color deleted: {label}", True
        return "⚠ Color could not be deleted.", True

    return "⚠ Nothing to delete.", True


async def on_button(update, ctx):
    q = update.callback_query
    # await q.answer()  <-- Moved to individual branches or end
    cmd = q.data
    if q.message:
        seen_id = UI.remember_last_seen(update.effective_chat.id, q.message.message_id)
        UI.log.info(
            "SEEN chat_id=%s message_id=%s stored=%s",
            update.effective_chat.id,
            q.message.message_id,
            seen_id,
        )
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    prev_id = UI.LAST_BOT_ID.get(chat_id)
    sent = False
    skip_cleanup = False

    if cmd.startswith("ha:") and cmd != "ha:close" and q.message:
        UI.touch_ha_menu_timeout(ctx, chat_id, q.message.message_id)

    if cmd == "skip":
        with UI.queue_state.LOCK:
            has_queue = len(UI.queue_state.QUEUE) > 0
        if not has_queue:
            await q.answer(text="⏹ End of queue.")
            sent = True
        else:
            UI.schedule_playback_action(ctx, chat_id, UI.queue_state.skip_queue)
            await q.answer(text="⏭ Next")
            # Brief yield so the playback thread has time to update DISPLAY_INDEX
            # and set BOT_EXPECTING_WS, then immediately refresh the panel so the
            # new track name/link appears before Kodi has even started playing.
            await asyncio.sleep(0.05)
            await UI.update_list_message(ctx, chat_id)
            await UI.update_now_playing_message(ctx, chat_id)
            sent = True
            skip_cleanup = True

    elif cmd == "back":
        with UI.queue_state.LOCK:
            has_queue = len(UI.queue_state.QUEUE) > 0
        if not has_queue:
            await q.answer(text="⏹ End of queue.")
            sent = True
        else:
            UI.schedule_playback_action(ctx, chat_id, UI.queue_state.back_queue)
            await q.answer(text="⏮ Back")
            # Brief yield so the playback thread has time to update DISPLAY_INDEX
            # and set BOT_EXPECTING_WS, then immediately refresh the panel so the
            # new track name/link appears before Kodi has even started playing.
            await asyncio.sleep(0.05)
            await UI.update_list_message(ctx, chat_id)
            await UI.update_now_playing_message(ctx, chat_id)
            sent = True
            skip_cleanup = True

    elif cmd == "cancel_reconnect":
        UI.cancel_reconnect_action(chat_id)
        await q.answer(text="Reconnection cancelled.")
        sent = True
        skip_cleanup = True

    elif cmd == "playpause":
        pid = UI.kodi_api.get_active_playerid()
        if pid is not None:
            UI.schedule_playback_action(ctx, chat_id, UI.kodi_api.kodi_call, "Player.PlayPause", {"playerid": pid})
            await q.answer(text="⏯")
            sent = True
        else:
            with UI.queue_state.LOCK:
                display_index = UI.queue_state.DISPLAY_INDEX
                has_queue = len(UI.queue_state.QUEUE) > 0
            if display_index is not None:
                UI.schedule_playback_action(ctx, chat_id, UI.queue_state.play_index, display_index)
                await q.answer(text="▶ Play")
                sent = True
            elif has_queue:
                UI.schedule_playback_action(ctx, chat_id, UI.queue_state.play_index, 0)
                await q.answer(text="▶ Play")
                sent = True
            else:
                await q.answer(text="⏹ Queue empty.")
                sent = True

    elif cmd == "stop":
        UI.schedule_playback_action(ctx, chat_id, UI.queue_state.hard_stop_and_clear)
        await q.answer(text="⏹ Stop")
        sent = True

    elif cmd.startswith("seek:"):
        if cmd == "seek:percent":
            if ctx.user_data.get("await_seek_percent"):
                await q.answer()
                return
            button_items = [("0%", 0), ("25%", 25), ("50%", 50), ("75%", 75), ("100%", 100)]
            msg_id = await UI.send_button_selection(
                ctx, 
                chat_id, 
                "⏱ Select or enter percentage (0-100):", 
                button_items, 
                "seek_to",
                items_per_row=5
            )
            UI.activate_prompt(ctx, chat_id, user_id, "await_seek_percent", "await_seek_percent_msg_id", msg_id)
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
                await q.answer(text="⚠ Unknown seek.")
                sent = True
            else:
                ok = UI.queue_state.seek_relative_seconds(delta)
                await q.answer(text="⏩ Seeked." if ok else "⚠ Seek failed.")
                sent = True

    elif cmd == "repeat":
        UI.queue_state.REPEAT_MODE = {"off":"one","one":"all","all":"off"}[UI.queue_state.REPEAT_MODE]
        await q.answer(text=f"🔁 Repeat: {UI.queue_state.REPEAT_MODE}")
        sent = True

    elif cmd == "deleteall":
        with UI.queue_state.LOCK:
            queue_count = len(UI.queue_state.QUEUE)
        if queue_count == 0:
            await q.answer(text="🗑 Queue empty.")
            sent = True
        else:
            UI.queue_state.clear_queue()
            await q.answer(text="🗑 Queue cleared")
            sent = True

    elif cmd == "delete:first":
        payload, msg = UI.queue_delete_confirmation_payload(0, "🗑 First track deleted.")
        if payload:
            answer_text, _ = await _execute_pending_delete(ctx, chat_id, payload)
            await q.answer(text=answer_text)
        else:
            await q.answer(text=msg)
        sent = True

    elif cmd == "delete:last":
        with UI.queue_state.LOCK:
            last_idx = len(UI.queue_state.QUEUE) - 1
        payload, msg = UI.queue_delete_confirmation_payload(last_idx, "🗑 Last track deleted.")
        if payload:
            answer_text, _ = await _execute_pending_delete(ctx, chat_id, payload)
            await q.answer(text=answer_text)
        else:
            await q.answer(text=msg)
        sent = True

    elif cmd == "play:ask":
        if ctx.user_data.get("await_play_index"):
            await q.answer()
            return
        with UI.queue_state.LOCK:
            has_queue = len(UI.queue_state.QUEUE) > 0
        if not has_queue:
            await q.answer(text="⏹ Queue empty.")
            sent = True
        else:
            msg = await UI.send_and_track(ctx, chat_id, "▶ Which number should be played? (e.g. 3)", reply_markup=UI.cancel_markup())
            UI.activate_prompt(ctx, chat_id, user_id, "await_play_index", "await_play_msg_id", msg.message_id)
            sent = True
            skip_cleanup = True
    elif cmd == "fav:ask":
        if ctx.user_data.get("await_favourite_index"):
            await q.answer()
            return
        favourites = await asyncio.to_thread(UI.kodi_api.get_playable_favourites)
        if not favourites:
            await q.answer(text="⭐ No playable Kodi favourites found.")
            sent = True
        else:
            button_items = [(fav['title'], i) for i, fav in enumerate(favourites)]
            msg_id = await UI.send_button_selection(
                ctx,
                chat_id,
                "⭐ Select a Kodi favourite:",
                button_items,
                "play_fav"
            )
            ctx.user_data["favourites"] = favourites
            UI.activate_prompt(
                ctx,
                chat_id,
                user_id,
                "await_favourite_index",
                "await_favourite_msg_id",
                msg_id,
                extra_keys=("favourites",),
            )
            sent = True
            skip_cleanup = True
    elif cmd.startswith("seek_to:"):
        pct = int(cmd.split(":")[1])
        ok = await asyncio.to_thread(UI.queue_state.seek_percent, pct)
        if ok:
            await q.answer(text=f"⏩ Seeked to {pct}%")
            if q.message:
                await UI.delete_message_if_present(ctx, chat_id, q.message.message_id)
            ctx.user_data["await_seek_percent"] = False
            ctx.user_data.pop("await_seek_percent_msg_id", None)
        else:
            await q.answer(text="⚠ Seek failed.")
        sent = True

    elif cmd == "media:ask":
        if UI.media_prompt_active(ctx.user_data):
            await q.answer()
            return
        scan_ok = await asyncio.to_thread(UI.kodi_api.scan_video_library)
        if not scan_ok:
            await q.answer(text="⚠ Library scan RPC failed.")
            sent = True
        else:
            button_items = [("🎬 Movies", "movies"), ("📺 Series", "shows")]
            msg_id = await UI.send_button_selection(
                ctx,
                chat_id,
                "🎬 Media browser:",
                button_items,
                "media_type"
            )
            UI.activate_prompt(ctx, chat_id, user_id, "await_media_type", "await_media_type_msg_id", msg_id)
            sent = True
            skip_cleanup = True
    elif cmd == "av:ask":
        if UI.av_prompt_active(ctx.user_data):
            await q.answer()
            return
        av_state = await asyncio.to_thread(UI.kodi_api.get_av_settings)
        if av_state.get("playerid") is None:
            await q.answer(text="⚠ Nothing is currently playing.")
            sent = True
        elif av_state.get("error"):
            await q.answer(text="⚠ Audio/subtitle information could not be loaded.")
            sent = True
        else:
            current_audio = UI.av_stream_label(av_state.get("currentaudiostream") or {})
            current_sub = UI.current_subtitle_label(av_state)
            header = (
                "🗣 Audio / Subtitles\n"
                f"Current audio: {current_audio}\n"
                f"Current subtitles: {current_sub}"
            )
            button_items = [("🗣 Change Audio", "audio"), ("💬 Change Subtitles", "subtitles")]
            msg_id = await UI.send_button_selection(ctx, chat_id, header, button_items, "av_action")
            UI.activate_prompt(ctx, chat_id, user_id, "await_av_action", "await_av_action_msg_id", msg_id)
            sent = True
            skip_cleanup = True
    elif cmd == "delete:ask":
        if ctx.user_data.get("await_delete_index"):
            await q.answer()
            return
        with UI.queue_state.LOCK:
            has_queue = len(UI.queue_state.QUEUE) > 0
        if not has_queue:
            await q.answer(text="🗑 Queue empty.")
            sent = True
        else:
            msg = await UI.send_and_track(ctx, chat_id, "🗑 Which number should be deleted? (e.g. 3)", reply_markup=UI.cancel_markup())
            UI.activate_prompt(ctx, chat_id, user_id, "await_delete_index", "await_delete_msg_id", msg.message_id)
            sent = True
            skip_cleanup = True
    elif cmd == "plist:save":
        if ctx.user_data.get("await_playlist_save_name"):
            await q.answer()
            return
        with UI.queue_state.LOCK:
            has_queue = len(UI.queue_state.QUEUE) > 0
        if not has_queue:
            await q.answer(text="🗒 Queue is empty.")
            sent = True
        else:
            msg = await UI.send_and_track(ctx, chat_id, "💾 Playlist name?", reply_markup=UI.cancel_markup())
            UI.activate_prompt(ctx, chat_id, user_id, "await_playlist_save_name", "await_playlist_save_msg_id", msg.message_id)
            sent = True
            skip_cleanup = True
    elif cmd == "plist:load":
        if ctx.user_data.get("await_playlist_load_index"):
            await q.answer()
            return
        files = UI.playlist_store.list_playlist_files(UI.CFG.playlist_dir)
        if not files:
            await q.answer(text="📂 No saved playlists found.")
            sent = True
        else:
            button_items = [(os.path.splitext(f)[0], i) for i, f in enumerate(files)]
            msg_id = await UI.send_button_selection(
                ctx,
                chat_id,
                "📂 Select a playlist:",
                button_items,
                "load_plist"
            )
            ctx.user_data["playlist_load_files"] = files
            UI.activate_prompt(
                ctx,
                chat_id,
                user_id,
                "await_playlist_load_index",
                "await_playlist_load_msg_id",
                msg_id,
                extra_keys=("playlist_load_files",),
            )
            sent = True
            skip_cleanup = True
    elif cmd == "radio:ask":
        if ctx.user_data.get("await_radio_search"):
            await q.answer()
            return
        msg = await UI.send_and_track(ctx, chat_id, "📻 Which station do you want to search for?", reply_markup=UI.cancel_markup())
        UI.activate_prompt(ctx, chat_id, user_id, "await_radio_search", "await_radio_search_msg_id", msg.message_id)
        sent = True
        skip_cleanup = True
    elif cmd == "radio:favorite":
        # Get currently playing item
        pid = await asyncio.to_thread(UI.kodi_api.get_active_playerid)
        if pid is None:
            await q.answer(text="⚠ Nothing is currently playing.")
            sent = True
        else:
            resp = await asyncio.to_thread(UI.kodi_api.kodi_call, "Player.GetItem", {"playerid": pid, "properties": ["file", "channel", "thumbnail"]})
            item = resp.get("result", {}).get("item", {})
            file_url = item.get("file") or ""
            channel = item.get("channel") or item.get("label") or "Unknown station"
            logo = item.get("thumbnail") or ""
            
            # Versuche einen besseren Namen und Logo via Radio-Browser API zu finden
            if file_url.startswith("http"):
                info = await asyncio.to_thread(radio_browser.get_station_info_by_url, file_url)
                if info:
                    if info.get("name"):
                        channel = info["name"]
                    if info.get("favicon"):
                        logo = info["favicon"]
            
            if not file_url.startswith("http"):
                await q.answer(text="⚠ This doesn't seem to be a radio stream.")
                sent = True
            else:
                ok_kodi = await asyncio.to_thread(UI.kodi_api.add_to_favourites, channel, file_url, logo)
                
                if ok_kodi:
                    await q.answer(text=f"⭐ {channel} added to favourites.")
                else:
                    await q.answer(text=f"⚠ {channel} could not be added to favourites.")
                sent = True
    elif cmd == "radio:delete:ask":
        if ctx.user_data.get("await_radio_delete_index"):
            await q.answer()
            return
        favs = await asyncio.to_thread(UI.kodi_api.get_favourites)
        if not favs:
            await q.answer(text="⭐ No Kodi favourites found.")
            sent = True
        else:
            button_items = [(f['title'], i) for i, f in enumerate(favs)]
            msg_id = await UI.send_button_selection(
                ctx,
                chat_id,
                "⭐ Which Kodi favourite do you want to delete?",
                button_items,
                "delete_fav"
            )
            ctx.user_data["favourites_delete"] = favs
            UI.activate_prompt(
                ctx,
                chat_id,
                user_id,
                "await_radio_delete_index",
                "await_radio_delete_index_msg_id",
                msg_id,
                extra_keys=("favourites_delete",),
            )
            sent = True
            skip_cleanup = True
    elif cmd == "plist:delete":
        if ctx.user_data.get("await_playlist_delete_index"):
            await q.answer()
            return
        files = UI.playlist_store.list_playlist_files(UI.CFG.playlist_dir)
        if not files:
            await q.answer(text="🗑 No saved playlists found.")
            sent = True
        else:
            button_items = [(os.path.splitext(f)[0], i) for i, f in enumerate(files)]
            msg_id = await UI.send_button_selection(
                ctx,
                chat_id,
                "🗑 Delete which playlist?",
                button_items,
                "delete_plist"
            )
            ctx.user_data["playlist_delete_files"] = files
            UI.activate_prompt(
                ctx,
                chat_id,
                user_id,
                "await_playlist_delete_index",
                "await_playlist_delete_msg_id",
                msg_id,
                extra_keys=("playlist_delete_files",),
            )
            sent = True
            skip_cleanup = True
    elif cmd == "vol:up5":
        ok = await asyncio.to_thread(UI.kodi_api.run_volume_delta, 5)
        await q.answer(text="🔊 +5" if ok else "⚠ Volume +5 failed")
        await asyncio.sleep(0.35)
        await UI.refresh_denon_volume_cache(force=True)
        await UI.update_now_playing_message(ctx, chat_id)
        sent = True
    elif cmd == "vol:up10":
        ok = await asyncio.to_thread(UI.kodi_api.run_volume_delta, 10)
        await q.answer(text="🔊 +10" if ok else "⚠ Volume +10 failed")
        await asyncio.sleep(0.35)
        await UI.refresh_denon_volume_cache(force=True)
        await UI.update_now_playing_message(ctx, chat_id)
        sent = True
    elif cmd == "vol:down5":
        ok = await asyncio.to_thread(UI.kodi_api.run_volume_delta, -5)
        await q.answer(text="🔉 -5" if ok else "⚠ Volume -5 failed")
        await asyncio.sleep(0.35)
        await UI.refresh_denon_volume_cache(force=True)
        await UI.update_now_playing_message(ctx, chat_id)
        sent = True
    elif cmd == "vol:down10":
        ok = await asyncio.to_thread(UI.kodi_api.run_volume_delta, -10)
        await q.answer(text="🔉 -10" if ok else "⚠ Volume -10 failed")
        await asyncio.sleep(0.35)
        await UI.refresh_denon_volume_cache(force=True)
        await UI.update_now_playing_message(ctx, chat_id)
        sent = True
    elif cmd == "hifi:on":
        ok = await asyncio.to_thread(UI.kodi_api.run_cec_power, True)
        await q.answer(text="🔌 Hifi On" if ok else "⚠ Hifi On failed")
        if UI.CFG.denon_host:
            UI.schedule_cleanup(ctx, chat_id, prev_id)
            await UI.update_list_message(ctx, chat_id)
            skip_cleanup = True
        else:
            await asyncio.sleep(10)
        await UI.refresh_hifi_status_cache(force=True)
        await UI.update_now_playing_message(ctx, chat_id)
        await UI.refresh_denon_volume_cache(force=True)
        sent = True
    elif cmd == "hifi:off":
        ok = await asyncio.to_thread(UI.kodi_api.run_cec_power, False)
        await q.answer(text="🔌 Hifi Off" if ok else "⚠ Hifi Off failed")
        if UI.CFG.denon_host:
            UI.schedule_cleanup(ctx, chat_id, prev_id)
            await UI.update_list_message(ctx, chat_id)
            skip_cleanup = True
        else:
            await asyncio.sleep(10)
        await UI.refresh_hifi_status_cache(force=True)
        await UI.update_now_playing_message(ctx, chat_id)
        sent = True
    elif cmd == "beamer:on":
        from kodibot.core.projector import projector
        ok = await asyncio.to_thread(projector.power_on)
        await q.answer(text="📽 Beamer On" if ok else "⚠ Beamer On failed")
        sent = True
    elif cmd == "beamer:off":
        from kodibot.core.projector import projector
        ok = await asyncio.to_thread(projector.power_off)
        await q.answer(text="📽 Beamer Off" if ok else "⚠ Beamer Off failed")
        sent = True
    elif cmd == "airplay:kill":
        ok = await asyncio.to_thread(UI.kodi_api.run_airplay_kill)
        status = await asyncio.to_thread(UI.kodi_api.get_airplay_status)
        UI.AIRPLAY_STATUS_CACHE = UI.resolve_airplay_status_text(status)
        UI.AIRPLAY_STATUS_TS = time.time()
        status_text = UI.AIRPLAY_STATUS_CACHE
        if ok:
            await q.answer(text=f"☠️ AirPlay Kill | {status_text}")
        else:
            await q.answer(text=f"⚠ AirPlay Kill failed | {status_text}")
        await UI.update_now_playing_message(ctx, chat_id)
        sent = True

    elif cmd == "ha:menu":
        if not UI.ha.ha_available():
            await q.answer(text="⚠ Home Assistant is not configured.")
            sent = True
        else:
            chat_type = getattr(update.effective_chat, "type", "") or ""
            bot_username = getattr(ctx.bot, "username", "") or ""
            state = await asyncio.to_thread(UI.ha.get_light_state)
            await UI.show_ha_menu(
                ctx, chat_id,
                chat_type=chat_type,
                bot_username=bot_username,
                state=state,
            )
            await q.answer()
            return
    elif cmd == "controls:menu":
        UI.set_panel_menu_mode(chat_id, "controls")
        await UI.update_now_playing_message(ctx, chat_id)
        await q.answer()
        return
    elif cmd == "controls:back":
        UI.set_panel_menu_mode(chat_id, "main")
        await UI.update_now_playing_message(ctx, chat_id)
        await q.answer()
        return
    elif cmd == "ha:back":
        if not UI.ha.ha_available():
            await UI.close_ha_menu_message(ctx, chat_id, q.message.message_id if q.message else None)
            await q.answer()
            return
        chat_type = getattr(update.effective_chat, "type", "") or ""
        bot_username = getattr(ctx.bot, "username", "") or ""
        state = await asyncio.to_thread(UI.ha.get_light_state)
        await UI.show_ha_menu(
            ctx,
            chat_id,
            chat_type=chat_type,
            bot_username=bot_username,
            state=state,
            edit_message_id=q.message.message_id if q.message else None,
        )
        await q.answer()
        return
    elif cmd == "ha:close":
        await UI.close_ha_menu_message(ctx, chat_id, q.message.message_id if q.message else None)
        await q.answer(text="❌ Cancelled")
        return
    elif cmd == "ha:noop":
        await q.answer()
        return
    elif cmd == "ha:toggle":
        ok, new_state = await asyncio.to_thread(UI.ha.toggle_light)
        if ok:
            emoji = "🟢" if new_state == "on" else "🔴"
            await q.answer(text=f"{emoji} Light: {new_state}")
            await _refresh_ha_menu(ctx, chat_id, update)
        else:
            await q.answer(text="⚠ Toggle failed.")
        sent = True
    elif cmd in {"ha:setcolor", "ha:loadcolor"}:
        await UI.show_ha_preset_menu(
            ctx,
            chat_id,
            edit_message_id=q.message.message_id if q.message else None,
        )
        await q.answer()
        return
    elif cmd == "ha:brightness":
        if ctx.user_data.get("await_ha_brightness_pct"):
            await q.answer()
            return
        state = await asyncio.to_thread(UI.ha.get_light_state)
        current_pct = UI.ha.brightness_percent_from_ha((state or {}).get("brightness"))
        prompt = "🔆 Select or enter brightness percent (0-100)"
        if current_pct is not None:
            prompt += f"\nCurrent: {current_pct}%"
        
        button_items = [("0%", 0), ("25%", 25), ("50%", 50), ("75%", 75), ("100%", 100)]
        msg_id = await UI.send_button_selection(ctx, chat_id, prompt, button_items, "ha_brightness_to", items_per_row=5)
        UI.activate_prompt(ctx, chat_id, user_id, "await_ha_brightness_pct", "await_ha_brightness_msg_id", msg_id)
        sent = True
        skip_cleanup = True
    elif cmd == "ha:deletecolor:ask":
        colors = await asyncio.to_thread(UI.ha.load_saved_colors)
        if not colors:
            await q.answer(text="⚠ No saved colors found.")
            sent = True
        elif ctx.user_data.get("await_ha_delete_color_index"):
            await q.answer()
            return
        else:
            button_items = [(UI.saved_color_name(color, i), i) for i, color in enumerate(colors)]
            msg_id = await UI.send_button_selection(
                ctx,
                chat_id,
                "🗑 Delete which saved color?",
                button_items,
                "delete_ha_color"
            )
            ctx.user_data["ha_delete_colors"] = colors
            UI.activate_prompt(
                ctx,
                chat_id,
                user_id,
                "await_ha_delete_color_index",
                "await_ha_delete_color_msg_id",
                msg_id,
                extra_keys=("ha_delete_colors",),
            )
            sent = True
            skip_cleanup = True
    elif cmd.startswith("ha:savedcolor:"):
        idx_text = cmd.rsplit(":", 1)[-1]
        colors = await asyncio.to_thread(UI.ha.load_saved_colors)
        if not idx_text.isdigit():
            await q.answer(text="⚠ Invalid saved color.")
        else:
            idx = int(idx_text)
            if 0 <= idx < len(colors):
                color = colors[idx]
                r = int(color.get("r", 0))
                g = int(color.get("g", 0))
                b = int(color.get("b", 0))
                ok = await asyncio.to_thread(UI.ha.set_light_color, r, g, b)
                if ok:
                    await q.answer(
                        text=f"🎨 Color \"{color.get('name', '?')}\" applied: #{r:02X}{g:02X}{b:02X}",
                    )
                    await _refresh_ha_menu(ctx, chat_id, update)
                else:
                    await q.answer(text="⚠ Color could not be applied.")
            else:
                await q.answer(text="⚠ Saved color not found.")
        sent = True
    elif cmd.startswith("ha:effect:"):
        effect_name = cmd.split(":", 2)[2].strip()
        if not effect_name:
            await q.answer(text="⚠ Invalid effect.")
        else:
            ok = await asyncio.to_thread(UI.ha.set_light_effect, effect_name)
            if ok:
                label = "Disco" if effect_name == "colorloop" else effect_name
                await q.answer(text=f"🪩 Effect enabled: {label}")
                await _refresh_ha_menu(ctx, chat_id, update)
            else:
                await q.answer(text="⚠ Effect could not be enabled.")
        sent = True
    elif cmd.startswith("ha:color:"):
        hex_part = cmd.split(":", 2)[2]
        parsed = UI.ha.parse_hex_color(hex_part)
        if parsed:
            r, g, b = parsed
            ok = await asyncio.to_thread(UI.ha.set_light_color, r, g, b)
            if ok:
                await q.answer(text=f"🎨 Color applied: #{hex_part}")
                await _refresh_ha_menu(ctx, chat_id, update)
            else:
                await q.answer(text="⚠ Color could not be applied.")
        else:
            await q.answer(text="⚠ Invalid color code.")
        sent = True
    elif cmd == "ha:sethex":
        if ctx.user_data.get("await_ha_hex"):
            await q.answer()
            return
        msg = await UI.send_and_track(ctx, chat_id, "🔢 Enter a hex code (e.g. #FF5500 or FF5500)", reply_markup=UI.cancel_markup())
        UI.activate_prompt(ctx, chat_id, user_id, "await_ha_hex", "await_ha_hex_msg_id", msg.message_id)
        sent = True
        skip_cleanup = True
    elif cmd == "ha:savecolor":
        if ctx.user_data.get("await_ha_save_color_name"):
            await q.answer()
            return
        state = await asyncio.to_thread(UI.ha.get_light_state)
        rgb = (state or {}).get("rgb_color")
        if not rgb or len(rgb) != 3:
            await q.answer(text="⚠ Current color could not be determined.")
            sent = True
        else:
            ctx.user_data["ha_save_rgb"] = list(rgb)
            msg = await UI.send_and_track(
                ctx, chat_id,
                f"💾 Current color: #{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}\n"
                "Enter a name for this color:",
                reply_markup=UI.cancel_markup()
            )
            UI.activate_prompt(
                ctx, chat_id, user_id,
                "await_ha_save_color_name", "await_ha_save_color_msg_id",
                msg.message_id,
                extra_keys=("ha_save_rgb",),
            )
            sent = True
            skip_cleanup = True
    elif cmd.startswith("play_fav:"):
        idx = int(cmd.split(":")[1])
        favourites = ctx.user_data.get("favourites", [])
        if 0 <= idx < len(favourites):
            fav = favourites[idx]
            ok = await asyncio.to_thread(UI.kodi_api.play_favourite_target, fav.get("target"), fav.get("title"))
            if ok:
                UI.queue_state.set_last_played_radio(fav.get("target"), fav.get("title"))
                await q.answer(text=f"⭐ Playing favourite: {fav['title']}")
                if q.message:
                    await UI.delete_message_if_present(ctx, chat_id, q.message.message_id)
                ctx.user_data["await_favourite_index"] = False
                ctx.user_data.pop("await_favourite_msg_id", None)
                ctx.user_data.pop("favourites", None)
            else:
                await q.answer(text="⚠ Favourite could not be played.")
        else:
            await q.answer(text="⚠ Favourite not found.")
        sent = True

    elif cmd.startswith("load_plist:"):
        idx = int(cmd.split(":")[1])
        files = ctx.user_data.get("playlist_load_files", [])
        if 0 <= idx < len(files):
            filename = files[idx]
            ok, items = await asyncio.to_thread(UI.playlist_store.load_playlist_from_disk, UI.CFG.playlist_dir, filename)
            if ok:
                UI.queue_state.hard_stop_and_clear()
                UI.queue_state.clear_queue()
                with UI.queue_state.LOCK:
                    UI.queue_state.QUEUE.extend(items)
                UI.queue_state.mark_list_dirty()
                await q.answer(text=f"📂 Loaded: {os.path.splitext(filename)[0]}")
                if q.message:
                    await UI.delete_message_if_present(ctx, chat_id, q.message.message_id)
                ctx.user_data["await_playlist_load_index"] = False
                ctx.user_data.pop("await_playlist_load_msg_id", None)
                ctx.user_data.pop("playlist_load_files", None)
            else:
                await q.answer(text=f"⚠ {items}")
        else:
            await q.answer(text="⚠ Playlist not found.")
        sent = True

    elif cmd.startswith("delete_confirm:"):
        parts = cmd.split(":")
        if len(parts) == 3:
            _, token, choice = parts
        else:
            token = None
            choice = parts[1] if len(parts) > 1 else ""
        pending_delete = ctx.user_data.get("pending_delete")
        if q.message:
            await UI.delete_message_if_present(ctx, chat_id, q.message.message_id)

        expired = token and pending_delete and pending_delete.get("token") != token
        if expired:
            await q.answer(text="⚠ Confirmation expired.")
            skip_cleanup = True
        else:
            UI.cancel_prompt_timeout(chat_id, user_id, "await_delete_confirm")
            ctx.user_data["await_delete_confirm"] = False
            ctx.user_data.pop("await_delete_confirm_msg_id", None)
            pending_delete = ctx.user_data.pop("pending_delete", None)
            if choice == "no":
                await q.answer(text="❌ Cancelled")
                skip_cleanup = True
            elif not pending_delete:
                await q.answer(text="⚠ Nothing to delete.")
                skip_cleanup = True
            else:
                answer_text, skip_cleanup = await _execute_pending_delete(ctx, chat_id, pending_delete)
                await q.answer(text=answer_text)
        sent = True

    elif cmd.startswith("delete_plist:"):
        idx = int(cmd.split(":")[1])
        files = ctx.user_data.get("playlist_load_files", []) or ctx.user_data.get("playlist_delete_files", [])
        if 0 <= idx < len(files):
            filename = files[idx]
            if q.message:
                await UI.delete_message_if_present(ctx, chat_id, q.message.message_id)
            _forget_prompt(
                ctx,
                chat_id,
                user_id,
                "await_playlist_delete_index",
                "await_playlist_delete_msg_id",
                "playlist_delete_files",
                "playlist_load_files",
            )
            await UI.request_delete_confirmation(
                ctx,
                chat_id,
                user_id,
                f"Are you sure you want to delete playlist \"{os.path.splitext(filename)[0]}\"?",
                {"kind": "playlist_file", "filename": filename},
            )
            skip_cleanup = True
        else:
            await q.answer(text="⚠ Playlist not found.")
        sent = True

    elif cmd.startswith("delete_fav:"):
        idx = int(cmd.split(":")[1])
        favs = ctx.user_data.get("favourites_delete", [])
        if 0 <= idx < len(favs):
            fav = favs[idx]
            title = fav.get("title") or "?"
            if q.message:
                await UI.delete_message_if_present(ctx, chat_id, q.message.message_id)
            _forget_prompt(
                ctx,
                chat_id,
                user_id,
                "await_radio_delete_index",
                "await_radio_delete_index_msg_id",
                "favourites_delete",
            )
            await UI.request_delete_confirmation(
                ctx,
                chat_id,
                user_id,
                f"Are you sure you want to delete favourite \"{title}\"?",
                {"kind": "favourite", "title": fav.get("title")},
            )
            skip_cleanup = True
        else:
            await q.answer(text="⚠ Favourite not found.")
        sent = True

    elif cmd.startswith("media_type:"):
        mtype = cmd.split(":")[1]
        if q.message:
            await UI.delete_message_if_present(ctx, chat_id, q.message.message_id)
        ctx.user_data["await_media_type"] = False
        
        if mtype == "movies":
            movies = await asyncio.to_thread(UI.kodi_api.list_movies)
            if not movies:
                await q.answer(text="🎬 No movies found.")
            else:
                msg_ids = await UI.send_chunked_selection(
                    ctx, chat_id, "🎬 Select movie:", UI.movie_list_lines(movies),
                )
                ctx.user_data["media_movies"] = movies
                UI.activate_prompt(ctx, chat_id, user_id, "await_movie_index", "await_movie_msg_id", msg_ids, extra_keys=("media_movies",))
                skip_cleanup = True
        elif mtype == "shows":
            shows = await asyncio.to_thread(UI.kodi_api.list_tvshows)
            if not shows:
                await q.answer(text="📺 No series found.")
            else:
                msg_ids = await UI.send_chunked_selection(
                    ctx, chat_id, "📺 Select series:", UI.show_list_lines(shows),
                )
                ctx.user_data["media_shows"] = shows
                UI.activate_prompt(ctx, chat_id, user_id, "await_show_index", "await_show_msg_id", msg_ids, extra_keys=("media_shows",))
                skip_cleanup = True
        sent = True


    elif cmd.startswith("av_action:"):
        action = cmd.split(":")[1]
        av_state = await asyncio.to_thread(UI.kodi_api.get_av_settings)
        if q.message:
            await UI.delete_message_if_present(ctx, chat_id, q.message.message_id)
        ctx.user_data["await_av_action"] = False
        
        if action == "audio":
            audio_streams = av_state.get("audiostreams") or []
            if not audio_streams:
                await q.answer(text="⚠ No audio streams available.")
            else:
                button_items = [(f"{UI.av_stream_label(s)}{' [active]' if s.get('index') == (av_state.get('currentaudiostream') or {}).get('index') else ''}", i) for i, s in enumerate(audio_streams)]
                msg_id = await UI.send_button_selection(
                    ctx, chat_id, "🗣 Select audio:", button_items, "set_audio"
                )
                ctx.user_data["audio_streams"] = audio_streams
                UI.activate_prompt(ctx, chat_id, user_id, "await_audio_index", "await_audio_msg_id", msg_id, extra_keys=("audio_streams",))
                skip_cleanup = True
        elif action == "subtitles":
            subtitles = av_state.get("subtitles") or []
            button_items = [("Off", -1)]
            current_index = (av_state.get("currentsubtitle") or {}).get("index")
            for i, s in enumerate(subtitles):
                active = av_state.get("subtitleenabled") and s.get("index") == current_index
                label = f"{UI.av_stream_label(s)}{' [active]' if active else ''}"
                button_items.append((label, i))
            
            msg_id = await UI.send_button_selection(
                ctx, chat_id, "💬 Select subtitles:", button_items, "set_subtitle"
            )
            ctx.user_data["subtitle_streams"] = subtitles
            UI.activate_prompt(ctx, chat_id, user_id, "await_subtitle_index", "await_subtitle_msg_id", msg_id, extra_keys=("subtitle_streams",))
            skip_cleanup = True
        sent = True

    elif cmd.startswith("movie_start_mode:"):
        mode = cmd.split(":")[1]
        movie = ctx.user_data.get("media_movie")
        if movie:
            resume = (mode == "resume")
            ok = await asyncio.to_thread(UI.kodi_api.play_movie, movie.get("movieid"), resume)
            if ok:
                await q.answer(text=f"🎬 Playing: {movie.get('title')}")
                if q.message:
                    await UI.delete_message_if_present(ctx, chat_id, q.message.message_id)
                ctx.user_data["await_movie_start_mode"] = False
                ctx.user_data.pop("media_movie", None)
            else:
                await q.answer(text="⚠ Movie could not be played.")
        sent = True

    elif cmd.startswith("episode_start_mode:"):
        mode = cmd.split(":")[1]
        episode = ctx.user_data.get("media_episode")
        if episode:
            resume = (mode == "resume")
            ok = await asyncio.to_thread(UI.kodi_api.play_episode, episode.get("episodeid"), resume)
            if ok:
                await q.answer(text=f"📺 Playing: {episode.get('title')}")
                if q.message:
                    await UI.delete_message_if_present(ctx, chat_id, q.message.message_id)
                ctx.user_data["await_episode_start_mode"] = False
                ctx.user_data.pop("media_episode", None)
            else:
                await q.answer(text="⚠ Episode could not be played.")
        sent = True
    elif cmd == "play_all_episodes":
        show = ctx.user_data.get("media_show")
        episodes = ctx.user_data.get("media_episodes", [])
        if episodes:
            ok = await asyncio.to_thread(
                UI.kodi_api.play_all_episodes,
                [episode.get("episodeid") for episode in episodes],
            )
            if ok:
                UI.queue_state.clear_bot_playback_state()
                title = show.get("title") if show else "Series"
                await q.answer(text=f"📺 Playing all episodes: {title}")
                if q.message:
                    await UI.delete_message_if_present(ctx, chat_id, q.message.message_id)
                _forget_prompt(
                    ctx, chat_id, user_id,
                    "await_episode_index", "await_episode_msg_id",
                    "media_show", "media_episodes",
                )
            else:
                await q.answer(text="⚠ Episodes could not be played.")
        else:
            await q.answer(text="⚠ No episodes found.")
        sent = True

    elif cmd.startswith("play_radio:"):
        idx = int(cmd.split(":")[1])
        stations = ctx.user_data.get("radio_results", [])
        if 0 <= idx < len(stations):
            selected = stations[idx]
            name = selected.get("name")
            url = selected.get("url_resolved") or selected.get("url")
            ok = await asyncio.to_thread(UI.kodi_api.play_favourite_target, url, name)
            if ok:
                UI.queue_state.set_last_played_radio(url, name)
                await q.answer(text=f"📻 Playing: {name}")
                if q.message:
                    await UI.delete_message_if_present(ctx, chat_id, q.message.message_id)
                ctx.user_data["await_radio_index"] = False
                ctx.user_data.pop("await_radio_index_msg_id", None)
                ctx.user_data.pop("radio_results", None)
            else:
                await q.answer(text="⚠ Radio station could not be played.")
        else:
            await q.answer(text="⚠ Station not found.")
        sent = True

    elif cmd == "ha:brightness":
        if ctx.user_data.get("await_ha_brightness_pct"):
            await q.answer()
            return
        state = await asyncio.to_thread(UI.ha.get_light_state)
        current_pct = UI.ha.brightness_percent_from_ha((state or {}).get("brightness"))
        prompt = "🔆 Select or enter brightness percent (0-100)"
        if current_pct is not None:
            prompt += f"\nCurrent: {current_pct}%"
        
        button_items = [("0%", 0), ("25%", 25), ("50%", 50), ("75%", 75), ("100%", 100)]
        msg_id = await UI.send_button_selection(ctx, chat_id, prompt, button_items, "ha_brightness_to", items_per_row=5)
        UI.activate_prompt(ctx, chat_id, user_id, "await_ha_brightness_pct", "await_ha_brightness_msg_id", msg_id)
        sent = True
        skip_cleanup = True

    elif cmd.startswith("ha_brightness_to:"):
        pct = int(cmd.split(":")[1])
        ok = await asyncio.to_thread(UI.ha.set_light_brightness, pct)
        if ok:
            await q.answer(text=f"🔆 Brightness set: {pct}%")
            if q.message:
                await UI.delete_message_if_present(ctx, chat_id, q.message.message_id)
            ctx.user_data["await_ha_brightness_pct"] = False
            ctx.user_data.pop("await_ha_brightness_msg_id", None)
            # Update HA menu if open
            menu_message_id = UI.HA_MENU_MSG_ID.get(chat_id)
            if menu_message_id:
                state = await asyncio.to_thread(UI.ha.get_light_state)
                await UI.show_ha_menu(ctx, chat_id, chat_type=q.message.chat.type if q.message else "private", bot_username=ctx.bot.username, state=state, edit_message_id=menu_message_id)
        else:
            await q.answer(text="⚠ Brightness update failed.")
        sent = True

    elif cmd.startswith("delete_ha_color:"):
        idx = int(cmd.split(":")[1])
        colors = ctx.user_data.get("ha_delete_colors", [])
        if 0 <= idx < len(colors):
            color = colors[idx]
            color_name = UI.saved_color_name(color, idx)
            if q.message:
                await UI.delete_message_if_present(ctx, chat_id, q.message.message_id)
            _forget_prompt(
                ctx,
                chat_id,
                user_id,
                "await_ha_delete_color_index",
                "await_ha_delete_color_msg_id",
                "ha_delete_colors",
            )
            await UI.request_delete_confirmation(
                ctx,
                chat_id,
                user_id,
                f"Are you sure you want to delete color \"{color_name}\"?",
                {"kind": "ha_color", "name": color.get("name", ""), "label": color_name},
            )
            skip_cleanup = True
        else:
            await q.answer(text="⚠ Color not found.")
        sent = True

    elif cmd.startswith("set_audio:"):
        idx = int(cmd.split(":")[1])
        audio_streams = ctx.user_data.get("audio_streams", [])
        if 0 <= idx < len(audio_streams):
            stream = audio_streams[idx]
            ok = await asyncio.to_thread(UI.kodi_api.set_audio_stream, stream.get("index"))
            if ok:
                await q.answer(text=f"🗣 Audio set: {UI.av_stream_label(stream)}")
                if q.message:
                    await UI.delete_message_if_present(ctx, chat_id, q.message.message_id)
                ctx.user_data["await_audio_index"] = False
                ctx.user_data.pop("await_audio_msg_id", None)
                ctx.user_data.pop("audio_streams", None)
            else:
                await q.answer(text="⚠ Audio stream update failed.")
        sent = True

    elif cmd.startswith("set_subtitle:"):
        idx = int(cmd.split(":")[1])
        subtitles = ctx.user_data.get("subtitle_streams", [])
        if idx == -1:
            ok = await asyncio.to_thread(UI.kodi_api.set_subtitle_stream, "off")
            label = "Off"
        elif 0 <= idx < len(subtitles):
            stream = subtitles[idx]
            ok = await asyncio.to_thread(UI.kodi_api.set_subtitle_stream, stream.get("index"))
            label = UI.av_stream_label(stream)
        else:
            ok = False
            label = "?"
            
        if ok:
            await q.answer(text=f"💬 Subtitles set: {label}")
            if q.message:
                await UI.delete_message_if_present(ctx, chat_id, q.message.message_id)
            ctx.user_data["await_subtitle_index"] = False
            ctx.user_data.pop("await_subtitle_msg_id", None)
            ctx.user_data.pop("subtitle_streams", None)
        else:
            await q.answer(text="⚠ Subtitle update failed.")
        sent = True

    elif cmd.startswith("plist_overwrite:"):
        choice = cmd.split(":")[1]
        name = ctx.user_data.get("playlist_overwrite_name")
        items = ctx.user_data.get("playlist_overwrite_items", [])
        
        if choice == "yes" and name:
            ok, res = await asyncio.to_thread(UI.playlist_store.save_playlist_to_disk_overwrite, UI.CFG.playlist_dir, name, items)
            if ok:
                await q.answer(text=f"💾 Saved as {os.path.splitext(res)[0]}")
                if q.message:
                    await UI.delete_message_if_present(ctx, chat_id, q.message.message_id)
            else:
                await q.answer(text=f"⚠ {res}")
        else:
            await q.answer(text="❌ Cancelled")
            if q.message:
                await UI.delete_message_if_present(ctx, chat_id, q.message.message_id)
                
        ctx.user_data["await_playlist_overwrite_confirm"] = False
        ctx.user_data.pop("await_playlist_overwrite_msg_id", None)
        ctx.user_data.pop("playlist_overwrite_name", None)
        ctx.user_data.pop("playlist_overwrite_items", None)
        sent = True

    elif cmd == "prompt:cancel":
        if q.message:
            await UI.delete_message_if_present(ctx, chat_id, q.message.message_id)
        
        # Clear all possible prompt states and delete associated messages
        keys_to_clear = []
        for k, v in list(ctx.user_data.items()):
            if k.endswith("_msg_id"):
                if isinstance(v, list):
                    for mid in v:
                        if q.message and mid == q.message.message_id:
                            continue
                        await UI.delete_message_if_present(ctx, chat_id, mid)
                elif isinstance(v, (int, str)):
                    try:
                        mid = int(v)
                        if not (q.message and mid == q.message.message_id):
                            await UI.delete_message_if_present(ctx, chat_id, mid)
                    except ValueError:
                        pass
                keys_to_clear.append(k)
            elif k.startswith("await_") or k.endswith("_files") or k == "favourites" or k.startswith("media_") or k.startswith("ha_delete_") or k == "audio_streams" or k == "subtitle_streams" or k == "radio_results" or k == "pending_delete":
                keys_to_clear.append(k)
                
        for k in keys_to_clear:
            ctx.user_data.pop(k, None)
            
        await q.answer(text="❌ Cancelled")
        sent = True

    if sent and not skip_cleanup:
        UI.schedule_cleanup(ctx, chat_id, prev_id)
        await UI.update_list_message(ctx, chat_id)

    # Always answer to clear the loading spinner
    try:
        await q.answer()
    except Exception:
        pass
