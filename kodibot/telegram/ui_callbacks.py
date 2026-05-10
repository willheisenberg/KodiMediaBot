import asyncio
import os
import time

from kodibot.telegram import ui as UI


async def on_button(update, ctx):
    q = update.callback_query
    await q.answer()
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
            await UI.send_and_track(ctx, chat_id, "⏹ End of queue.")
            sent = True
        else:
            UI.schedule_playback_action(ctx, chat_id, UI.queue_state.skip_queue)
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
            await UI.send_and_track(ctx, chat_id, "⏹ End of queue.")
            sent = True
        else:
            UI.schedule_playback_action(ctx, chat_id, UI.queue_state.back_queue)
            # Brief yield so the playback thread has time to update DISPLAY_INDEX
            # and set BOT_EXPECTING_WS, then immediately refresh the panel so the
            # new track name/link appears before Kodi has even started playing.
            await asyncio.sleep(0.05)
            await UI.update_list_message(ctx, chat_id)
            await UI.update_now_playing_message(ctx, chat_id)
            sent = True
            skip_cleanup = True

    elif cmd == "playpause":
        pid = UI.kodi_api.get_active_playerid()
        if pid is not None:
            UI.schedule_playback_action(ctx, chat_id, UI.kodi_api.kodi_call, "Player.PlayPause", {"playerid": pid})
            await UI.send_and_track(ctx, chat_id, "⏯")
            sent = True
        else:
            with UI.queue_state.LOCK:
                display_index = UI.queue_state.DISPLAY_INDEX
                has_queue = len(UI.queue_state.QUEUE) > 0
            if display_index is not None:
                UI.schedule_playback_action(ctx, chat_id, UI.queue_state.play_index, display_index)
                await UI.send_and_track(ctx, chat_id, "▶ Play")
                sent = True
            elif has_queue:
                UI.schedule_playback_action(ctx, chat_id, UI.queue_state.play_index, 0)
                await UI.send_and_track(ctx, chat_id, "▶ Play")
                sent = True
            else:
                await UI.send_and_track(ctx, chat_id, "⏹ Queue empty.")
                sent = True

    elif cmd == "stop":
        UI.schedule_playback_action(ctx, chat_id, UI.queue_state.hard_stop_and_clear)
        await UI.send_and_track(ctx, chat_id, "⏹ Stop")
        sent = True

    elif cmd.startswith("seek:"):
        if cmd == "seek:percent":
            if ctx.user_data.get("await_seek_percent"):
                return
            msg = await UI.send_and_track(ctx, chat_id, "⏱ Percent? (0-100, q = cancel)")
            UI.activate_prompt(ctx, chat_id, user_id, "await_seek_percent", "await_seek_percent_msg_id", msg.message_id)
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
                await UI.send_and_track(ctx, chat_id, "⚠ Unknown seek.")
                sent = True
            else:
                ok = UI.queue_state.seek_relative_seconds(delta)
                await UI.send_and_track(ctx, chat_id, "⏩ Seeked." if ok else "⚠ Seek failed.")
                sent = True

    elif cmd == "repeat":
        UI.queue_state.REPEAT_MODE = {"off":"one","one":"all","all":"off"}[UI.queue_state.REPEAT_MODE]
        await UI.send_and_track(ctx, chat_id, f"🔁 Repeat: {UI.queue_state.REPEAT_MODE}")
        sent = True

    elif cmd == "deleteall":
        UI.queue_state.clear_queue()
        await UI.send_and_track(ctx, chat_id, "🗑 Queue cleared")
        sent = True

    elif cmd == "delete:first":
        ok, msg = UI.queue_state.delete_index(0)
        if ok:
            await UI.send_and_track(ctx, chat_id, "🗑 First track deleted.")
        else:
            await UI.send_and_track(ctx, chat_id, msg)
        sent = True

    elif cmd == "delete:last":
        with UI.queue_state.LOCK:
            last_idx = len(UI.queue_state.QUEUE) - 1
        ok, msg = UI.queue_state.delete_index(last_idx)
        if ok:
            await UI.send_and_track(ctx, chat_id, "🗑 Last track deleted.")
        else:
            await UI.send_and_track(ctx, chat_id, msg)
        sent = True

    elif cmd == "play:ask":
        if ctx.user_data.get("await_play_index"):
            return
        msg = await UI.send_and_track(ctx, chat_id, "▶ Which number should be played? (e.g. 3, q = cancel)")
        UI.activate_prompt(ctx, chat_id, user_id, "await_play_index", "await_play_msg_id", msg.message_id)
        sent = True
        skip_cleanup = True
    elif cmd == "fav:ask":
        if ctx.user_data.get("await_favourite_index"):
            return
        favourites = await asyncio.to_thread(UI.kodi_api.get_playable_favourites)
        if not favourites:
            await UI.send_and_track(ctx, chat_id, "⭐ No playable Kodi favourites found.")
            sent = True
        else:
            lines = [f"{i+1}. {fav['title']}" for i, fav in enumerate(favourites)]
            msg = await UI.send_and_track(
                ctx,
                chat_id,
                "⭐ Select a Kodi favourite (q = cancel):\n" + "\n".join(lines),
            )
            ctx.user_data["favourites"] = favourites
            UI.activate_prompt(
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
        if UI.media_prompt_active(ctx.user_data):
            return
        scan_ok = await asyncio.to_thread(UI.kodi_api.scan_video_library)
        if not scan_ok:
            await UI.send_and_track(ctx, chat_id, "⚠ Library scan RPC failed.")
        msg = await UI.send_and_track(
            ctx,
            chat_id,
            "🎬 Media browser\n1. Movies\n2. Series\nq = cancel",
        )
        UI.activate_prompt(ctx, chat_id, user_id, "await_media_type", "await_media_type_msg_id", msg.message_id)
        sent = True
        skip_cleanup = True
    elif cmd == "av:ask":
        if UI.av_prompt_active(ctx.user_data):
            return
        av_state = await asyncio.to_thread(UI.kodi_api.get_av_settings)
        if av_state.get("playerid") is None:
            await UI.send_and_track(ctx, chat_id, "⚠ Nothing is currently playing.")
            sent = True
        elif av_state.get("error"):
            await UI.send_and_track(ctx, chat_id, "⚠ Audio/subtitle information could not be loaded.")
            sent = True
        else:
            current_audio = UI.av_stream_label(av_state.get("currentaudiostream") or {})
            current_sub = UI.current_subtitle_label(av_state)
            msg = await UI.send_and_track(
                ctx,
                chat_id,
                "🗣 Audio / Subtitles\n"
                "1. Change audio\n"
                "2. Change subtitles\n"
                f"Current audio: {current_audio}\n"
                f"Current subtitles: {current_sub}\n"
                "q = cancel",
            )
            UI.activate_prompt(ctx, chat_id, user_id, "await_av_action", "await_av_action_msg_id", msg.message_id)
            sent = True
            skip_cleanup = True
    elif cmd == "delete:ask":
        if ctx.user_data.get("await_delete_index"):
            return
        msg = await UI.send_and_track(ctx, chat_id, "🗑 Which number should be deleted? (e.g. 3, q = cancel)")
        UI.activate_prompt(ctx, chat_id, user_id, "await_delete_index", "await_delete_msg_id", msg.message_id)
        sent = True
        skip_cleanup = True
    elif cmd == "plist:save":
        if ctx.user_data.get("await_playlist_save_name"):
            return
        with UI.queue_state.LOCK:
            has_queue = len(UI.queue_state.QUEUE) > 0
        if not has_queue:
            await UI.send_and_track(ctx, chat_id, "🗒 Queue is empty.")
            sent = True
        else:
            msg = await UI.send_and_track(ctx, chat_id, "💾 Playlist name? (q = cancel)")
            UI.activate_prompt(ctx, chat_id, user_id, "await_playlist_save_name", "await_playlist_save_msg_id", msg.message_id)
            sent = True
            skip_cleanup = True
    elif cmd == "plist:load":
        if ctx.user_data.get("await_playlist_load_index"):
            return
        files = UI.playlist_store.list_playlist_files(UI.CFG.playlist_dir)
        if not files:
            await UI.send_and_track(ctx, chat_id, "📂 No saved playlists found.")
            sent = True
        else:
            lines = [f"{i+1}. {os.path.splitext(f)[0]}" for i, f in enumerate(files)]
            msg = await UI.send_and_track(
                ctx,
                chat_id,
                "📂 Select a playlist (q = cancel):\n" + "\n".join(lines),
            )
            ctx.user_data["playlist_load_files"] = files
            UI.activate_prompt(
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
        files = UI.playlist_store.list_playlist_files(UI.CFG.playlist_dir)
        if not files:
            await UI.send_and_track(ctx, chat_id, "🗑 No saved playlists found.")
            sent = True
        else:
            lines = [f"{i+1}. {os.path.splitext(f)[0]}" for i, f in enumerate(files)]
            msg = await UI.send_and_track(
                ctx,
                chat_id,
                "🗑 Delete which playlist? (q = cancel)\n" + "\n".join(lines),
            )
            ctx.user_data["playlist_delete_files"] = files
            UI.activate_prompt(
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
        ok = await asyncio.to_thread(UI.kodi_api.run_volume_delta, 5)
        await UI.send_and_track(ctx, chat_id, "🔊 +5" if ok else "⚠ Volume +5 failed")
        await asyncio.sleep(0.35)
        await UI.refresh_denon_volume_cache(force=True)
        await UI.update_now_playing_message(ctx, chat_id)
        sent = True
    elif cmd == "vol:up10":
        ok = await asyncio.to_thread(UI.kodi_api.run_volume_delta, 10)
        await UI.send_and_track(ctx, chat_id, "🔊 +10" if ok else "⚠ Volume +10 failed")
        await asyncio.sleep(0.35)
        await UI.refresh_denon_volume_cache(force=True)
        await UI.update_now_playing_message(ctx, chat_id)
        sent = True
    elif cmd == "vol:down5":
        ok = await asyncio.to_thread(UI.kodi_api.run_volume_delta, -5)
        await UI.send_and_track(ctx, chat_id, "🔉 -5" if ok else "⚠ Volume -5 failed")
        await asyncio.sleep(0.35)
        await UI.refresh_denon_volume_cache(force=True)
        await UI.update_now_playing_message(ctx, chat_id)
        sent = True
    elif cmd == "vol:down10":
        ok = await asyncio.to_thread(UI.kodi_api.run_volume_delta, -10)
        await UI.send_and_track(ctx, chat_id, "🔉 -10" if ok else "⚠ Volume -10 failed")
        await asyncio.sleep(0.35)
        await UI.refresh_denon_volume_cache(force=True)
        await UI.update_now_playing_message(ctx, chat_id)
        sent = True
    elif cmd == "hifi:on":
        ok = await asyncio.to_thread(UI.kodi_api.run_cec_power, True)
        await UI.send_and_track(ctx, chat_id, "🔌 Hifi On" if ok else "⚠ Hifi On failed")
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
        await UI.send_and_track(ctx, chat_id, "🔌 Hifi Off" if ok else "⚠ Hifi Off failed")
        if UI.CFG.denon_host:
            UI.schedule_cleanup(ctx, chat_id, prev_id)
            await UI.update_list_message(ctx, chat_id)
            skip_cleanup = True
        else:
            await asyncio.sleep(10)
        await UI.refresh_hifi_status_cache(force=True)
        await UI.update_now_playing_message(ctx, chat_id)
        sent = True
    elif cmd == "airplay:kill":
        ok = await asyncio.to_thread(UI.kodi_api.run_airplay_kill)
        status = await asyncio.to_thread(UI.kodi_api.get_airplay_status)
        UI.AIRPLAY_STATUS_CACHE = UI.resolve_airplay_status_text(status)
        UI.AIRPLAY_STATUS_TS = time.time()
        status_text = UI.AIRPLAY_STATUS_CACHE
        if ok:
            await UI.send_and_track(ctx, chat_id, f"☠️ AirPlay Kill | {status_text}")
        else:
            await UI.send_and_track(ctx, chat_id, f"⚠ AirPlay Kill failed | {status_text}")
        await UI.update_now_playing_message(ctx, chat_id)
        sent = True

    elif cmd == "ha:menu":
        if not UI.ha.ha_available():
            await UI.send_and_track(ctx, chat_id, "⚠ Home Assistant is not configured.")
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
            return
    elif cmd == "controls:menu":
        UI.set_panel_menu_mode(chat_id, "controls")
        await UI.update_now_playing_message(ctx, chat_id)
        return
    elif cmd == "controls:back":
        UI.set_panel_menu_mode(chat_id, "main")
        await UI.update_now_playing_message(ctx, chat_id)
        return
    elif cmd == "ha:back":
        if not UI.ha.ha_available():
            await UI.close_ha_menu_message(ctx, chat_id, q.message.message_id if q.message else None)
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
        return
    elif cmd == "ha:close":
        await UI.close_ha_menu_message(ctx, chat_id, q.message.message_id if q.message else None)
        return
    elif cmd == "ha:noop":
        return
    elif cmd == "ha:toggle":
        ok, new_state = await asyncio.to_thread(UI.ha.toggle_light)
        if ok:
            emoji = "🟢" if new_state == "on" else "🔴"
            await UI.send_and_track(ctx, chat_id, f"{emoji} Light: {new_state}")
        else:
            await UI.send_and_track(ctx, chat_id, "⚠ Toggle failed.")
        sent = True
    elif cmd in {"ha:setcolor", "ha:loadcolor"}:
        await UI.show_ha_preset_menu(
            ctx,
            chat_id,
            edit_message_id=q.message.message_id if q.message else None,
        )
        return
    elif cmd == "ha:brightness":
        if ctx.user_data.get("await_ha_brightness_pct"):
            return
        state = await asyncio.to_thread(UI.ha.get_light_state)
        current_pct = UI.ha.brightness_percent_from_ha((state or {}).get("brightness"))
        prompt = "🔆 Enter brightness percent (0-100, q = cancel)"
        if current_pct is not None:
            prompt += f"\nCurrent brightness: {current_pct}%"
        msg = await UI.send_and_track(ctx, chat_id, prompt)
        UI.activate_prompt(
            ctx,
            chat_id,
            user_id,
            "await_ha_brightness_pct",
            "await_ha_brightness_msg_id",
            msg.message_id,
        )
        sent = True
        skip_cleanup = True
    elif cmd == "ha:deletecolor:ask":
        colors = await asyncio.to_thread(UI.ha.load_saved_colors)
        if not colors:
            await UI.send_and_track(ctx, chat_id, "⚠ No saved colors found.")
            sent = True
        elif ctx.user_data.get("await_ha_delete_color_index"):
            return
        else:
            lines = [f"{i+1}. {UI.saved_color_name(color, i)}" for i, color in enumerate(colors)]
            ctx.user_data["ha_delete_colors"] = colors
            msg = await UI.send_and_track(
                ctx,
                chat_id,
                "🗑 Delete which saved color? (q = cancel)\n" + "\n".join(lines),
            )
            UI.activate_prompt(
                ctx,
                chat_id,
                user_id,
                "await_ha_delete_color_index",
                "await_ha_delete_color_msg_id",
                msg.message_id,
                extra_keys=("ha_delete_colors",),
            )
            sent = True
            skip_cleanup = True
    elif cmd.startswith("ha:savedcolor:"):
        idx_text = cmd.rsplit(":", 1)[-1]
        colors = await asyncio.to_thread(UI.ha.load_saved_colors)
        if not idx_text.isdigit():
            await UI.send_and_track(ctx, chat_id, "⚠ Invalid saved color.")
        else:
            idx = int(idx_text)
            if 0 <= idx < len(colors):
                color = colors[idx]
                r = int(color.get("r", 0))
                g = int(color.get("g", 0))
                b = int(color.get("b", 0))
                ok = await asyncio.to_thread(UI.ha.set_light_color, r, g, b)
                if ok:
                    await UI.send_and_track(
                        ctx,
                        chat_id,
                        f"🎨 Color \"{color.get('name', '?')}\" applied: #{r:02X}{g:02X}{b:02X}",
                    )
                else:
                    await UI.send_and_track(ctx, chat_id, "⚠ Color could not be applied.")
            else:
                await UI.send_and_track(ctx, chat_id, "⚠ Saved color not found.")
        sent = True
    elif cmd.startswith("ha:effect:"):
        effect_name = cmd.split(":", 2)[2].strip()
        if not effect_name:
            await UI.send_and_track(ctx, chat_id, "⚠ Invalid effect.")
        else:
            ok = await asyncio.to_thread(UI.ha.set_light_effect, effect_name)
            if ok:
                label = "Disco" if effect_name == "colorloop" else effect_name
                await UI.send_and_track(ctx, chat_id, f"🪩 Effect enabled: {label}")
            else:
                await UI.send_and_track(ctx, chat_id, "⚠ Effect could not be enabled.")
        sent = True
    elif cmd.startswith("ha:color:"):
        hex_part = cmd.split(":", 2)[2]
        parsed = UI.ha.parse_hex_color(hex_part)
        if parsed:
            r, g, b = parsed
            ok = await asyncio.to_thread(UI.ha.set_light_color, r, g, b)
            if ok:
                await UI.send_and_track(ctx, chat_id, f"🎨 Color applied: #{hex_part}")
            else:
                await UI.send_and_track(ctx, chat_id, "⚠ Color could not be applied.")
        else:
            await UI.send_and_track(ctx, chat_id, "⚠ Invalid color code.")
        sent = True
    elif cmd == "ha:sethex":
        if ctx.user_data.get("await_ha_hex"):
            return
        msg = await UI.send_and_track(ctx, chat_id, "🔢 Enter a hex code (e.g. #FF5500 or FF5500, q = cancel)")
        UI.activate_prompt(ctx, chat_id, user_id, "await_ha_hex", "await_ha_hex_msg_id", msg.message_id)
        sent = True
        skip_cleanup = True
    elif cmd == "ha:savecolor":
        if ctx.user_data.get("await_ha_save_color_name"):
            return
        state = await asyncio.to_thread(UI.ha.get_light_state)
        rgb = (state or {}).get("rgb_color")
        if not rgb or len(rgb) != 3:
            await UI.send_and_track(ctx, chat_id, "⚠ Current color could not be determined.")
            sent = True
        else:
            ctx.user_data["ha_save_rgb"] = list(rgb)
            msg = await UI.send_and_track(
                ctx, chat_id,
                f"💾 Current color: #{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}\n"
                "Enter a name for this color (q = cancel):"
            )
            UI.activate_prompt(
                ctx, chat_id, user_id,
                "await_ha_save_color_name", "await_ha_save_color_msg_id",
                msg.message_id,
                extra_keys=("ha_save_rgb",),
            )
            sent = True
            skip_cleanup = True
    if sent and not skip_cleanup:
        UI.schedule_cleanup(ctx, chat_id, prev_id)
        await UI.update_list_message(ctx, chat_id)
