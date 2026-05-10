import asyncio
import html
import os
import re

from kodibot.telegram import ui as UI


async def handle_text(update, ctx):
    UI.record_last_seen(ctx, update)
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    prev_id = UI.LAST_BOT_ID.get(chat_id)
    sent = False
    skip_cleanup = False
    msg_id = update.message.message_id
    txt = update.message.text.strip()
    txt_lower = txt.lower()

    if ctx.user_data.get("await_ha_hex"):
        UI.cancel_prompt_timeout(chat_id, user_id, "await_ha_hex")
        ctx.user_data["await_ha_hex"] = False
        prompt_id = ctx.user_data.pop("await_ha_hex_msg_id", None)
        await UI.delete_message_if_present(ctx, chat_id, msg_id)
        if txt_lower == "q":
            await UI.send_and_track(ctx, chat_id, "Cancelled.")
        else:
            parsed = UI.ha.parse_hex_color(txt)
            if parsed:
                r, g, b = parsed
                ok = await asyncio.to_thread(UI.ha.set_light_color, r, g, b)
                if ok:
                    await UI.send_and_track(ctx, chat_id, f"🎨 Color applied: #{r:02X}{g:02X}{b:02X}")
                else:
                    await UI.send_and_track(ctx, chat_id, "⚠ Color could not be applied.")
            else:
                await UI.send_and_track(ctx, chat_id, "⚠ Invalid hex code. Use the format #FF5500 or FF5500.")
        sent = True
        await UI.delete_message_if_present(ctx, chat_id, prompt_id)
        if sent:
            UI.schedule_cleanup(ctx, chat_id, prev_id)
            await UI.update_list_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_ha_brightness_pct"):
        UI.cancel_prompt_timeout(chat_id, user_id, "await_ha_brightness_pct")
        ctx.user_data["await_ha_brightness_pct"] = False
        prompt_id = ctx.user_data.pop("await_ha_brightness_msg_id", None)
        await UI.delete_message_if_present(ctx, chat_id, msg_id)
        if txt_lower == "q":
            await UI.send_and_track(ctx, chat_id, "Cancelled.")
        else:
            try:
                percent = int(txt)
            except ValueError:
                percent = -1
            if 0 <= percent <= 100:
                ok = await asyncio.to_thread(UI.ha.set_light_brightness, percent)
                if ok:
                    menu_message_id = UI.HA_MENU_MSG_ID.get(chat_id)
                    if menu_message_id:
                        state = await asyncio.to_thread(UI.ha.get_light_state)
                        await UI.show_ha_menu(
                            ctx,
                            chat_id,
                            chat_type=getattr(update.effective_chat, "type", "") or "",
                            bot_username=getattr(ctx.bot, "username", "") or "",
                            state=state,
                            edit_message_id=menu_message_id,
                        )
                    await UI.send_and_track(ctx, chat_id, f"🔆 Brightness set: {percent}%")
                else:
                    await UI.send_and_track(ctx, chat_id, "⚠ Brightness could not be updated.")
            else:
                await UI.send_and_track(ctx, chat_id, "⚠ Enter a brightness percent from 0 to 100.")
        sent = True
        await UI.delete_message_if_present(ctx, chat_id, prompt_id)
        if sent:
            UI.schedule_cleanup(ctx, chat_id, prev_id)
            await UI.update_list_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_ha_save_color_name"):
        UI.cancel_prompt_timeout(chat_id, user_id, "await_ha_save_color_name")
        ctx.user_data["await_ha_save_color_name"] = False
        prompt_id = ctx.user_data.pop("await_ha_save_color_msg_id", None)
        rgb = ctx.user_data.pop("ha_save_rgb", None)
        await UI.delete_message_if_present(ctx, chat_id, msg_id)
        if txt_lower == "q":
            await UI.send_and_track(ctx, chat_id, "Cancelled.")
        elif rgb and len(rgb) == 3:
            ok = await asyncio.to_thread(UI.ha.save_color, txt.strip(), rgb[0], rgb[1], rgb[2])
            if ok:
                await UI.send_and_track(ctx, chat_id, f"💾 Color \"{txt.strip()}\" saved.")
            else:
                await UI.send_and_track(ctx, chat_id, "⚠ Color could not be saved.")
        else:
            await UI.send_and_track(ctx, chat_id, "⚠ No color available.")
        sent = True
        await UI.delete_message_if_present(ctx, chat_id, prompt_id)
        if sent:
            UI.schedule_cleanup(ctx, chat_id, prev_id)
            await UI.update_list_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_ha_delete_color_index"):
        UI.cancel_prompt_timeout(chat_id, user_id, "await_ha_delete_color_index")
        ctx.user_data["await_ha_delete_color_index"] = False
        prompt_id = ctx.user_data.pop("await_ha_delete_color_msg_id", None)
        colors = ctx.user_data.pop("ha_delete_colors", [])
        await UI.delete_message_if_present(ctx, chat_id, msg_id)
        if txt_lower == "q":
            await UI.send_and_track(ctx, chat_id, "Cancelled.")
        elif txt.isdigit():
            idx = int(txt) - 1
            if 0 <= idx < len(colors):
                color = colors[idx]
                color_name = UI.saved_color_name(color, idx)
                ok = await asyncio.to_thread(UI.ha.delete_saved_color, color.get("name", ""))
                if ok:
                    menu_message_id = UI.HA_MENU_MSG_ID.get(chat_id)
                    if menu_message_id:
                        await UI.show_ha_preset_menu(ctx, chat_id, edit_message_id=menu_message_id)
                    await UI.send_and_track(ctx, chat_id, f"🗑 Color \"{color_name}\" deleted.")
                else:
                    await UI.send_and_track(ctx, chat_id, "⚠ Color could not be deleted.")
            else:
                await UI.send_and_track(ctx, chat_id, "⚠ Invalid color number.")
        else:
            await UI.send_and_track(ctx, chat_id, "⚠ Enter a valid number or q to cancel.")
        sent = True
        await UI.delete_message_if_present(ctx, chat_id, prompt_id)
        if sent:
            UI.schedule_cleanup(ctx, chat_id, prev_id)
            await UI.update_list_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_media_type"):
        UI.cancel_prompt_timeout(chat_id, user_id, "await_media_type")
        ctx.user_data["await_media_type"] = False
        prompt_id = ctx.user_data.pop("await_media_type_msg_id", None)
        await UI.delete_message_if_present(ctx, chat_id, msg_id)
        if txt_lower == "q":
            await UI.send_and_track(ctx, chat_id, "Cancelled.")
            sent = True
        elif txt == "1":
            movies = await asyncio.to_thread(UI.kodi_api.list_movies)
            if not movies:
                await UI.send_and_track(ctx, chat_id, "🎬 No movies found in Kodi.")
                sent = True
            else:
                msg_ids = await UI.send_chunked_selection(
                    ctx,
                    chat_id,
                    "🎬 Select movie:",
                    UI.movie_list_lines(movies),
                    footer="q = cancel",
                )
                ctx.user_data["media_movies"] = movies
                UI.activate_prompt(
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
            shows = await asyncio.to_thread(UI.kodi_api.list_tvshows)
            if not shows:
                await UI.send_and_track(ctx, chat_id, "📺 No series found in Kodi.")
                sent = True
            else:
                msg_ids = await UI.send_chunked_selection(
                    ctx,
                    chat_id,
                    "📺 Select series:",
                    UI.show_list_lines(shows),
                    footer="q = cancel",
                )
                ctx.user_data["media_shows"] = shows
                UI.activate_prompt(
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
            await UI.send_and_track(ctx, chat_id, "Please enter 1 or 2 (or q to cancel).")
            sent = True
            skip_cleanup = True
        await UI.delete_message_if_present(ctx, chat_id, prompt_id)
        if sent and not skip_cleanup:
            UI.schedule_cleanup(ctx, chat_id, prev_id)
            await UI.update_list_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_av_action"):
        UI.cancel_prompt_timeout(chat_id, user_id, "await_av_action")
        ctx.user_data["await_av_action"] = False
        prompt_id = ctx.user_data.pop("await_av_action_msg_id", None)
        await UI.delete_message_if_present(ctx, chat_id, msg_id)
        if txt_lower == "q":
            await UI.send_and_track(ctx, chat_id, "Cancelled.")
            sent = True
        elif txt == "1":
            av_state = await asyncio.to_thread(UI.kodi_api.get_av_settings)
            audio_streams = av_state.get("audiostreams") or []
            if av_state.get("playerid") is None:
                await UI.send_and_track(ctx, chat_id, "⚠ Nothing is currently playing.")
                sent = True
            elif av_state.get("error"):
                await UI.send_and_track(ctx, chat_id, "⚠ Audio streams could not be loaded.")
                sent = True
            elif not audio_streams:
                await UI.send_and_track(ctx, chat_id, "⚠ No audio streams available.")
                sent = True
            else:
                lines = []
                current_index = (av_state.get("currentaudiostream") or {}).get("index")
                for i, stream in enumerate(audio_streams, start=1):
                    marker = " [active]" if stream.get("index") == current_index else ""
                    lines.append(f"{i}. {UI.av_stream_label(stream)}{marker}")
                msg = await UI.send_and_track(
                    ctx,
                    chat_id,
                    "🗣 Select audio:\n" + "\n".join(lines) + "\nq = cancel",
                )
                ctx.user_data["audio_streams"] = audio_streams
                UI.activate_prompt(
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
            av_state = await asyncio.to_thread(UI.kodi_api.get_av_settings)
            subtitles = av_state.get("subtitles") or []
            if av_state.get("playerid") is None:
                await UI.send_and_track(ctx, chat_id, "⚠ Nothing is currently playing.")
                sent = True
            elif av_state.get("error"):
                await UI.send_and_track(ctx, chat_id, "⚠ Subtitle streams could not be loaded.")
                sent = True
            else:
                lines = ["1. Off"]
                current_index = (av_state.get("currentsubtitle") or {}).get("index")
                for i, stream in enumerate(subtitles, start=2):
                    active = av_state.get("subtitleenabled") and stream.get("index") == current_index
                    marker = " [active]" if active else ""
                    lines.append(f"{i}. {UI.av_stream_label(stream)}{marker}")
                msg = await UI.send_and_track(
                    ctx,
                    chat_id,
                    "💬 Select subtitles:\n" + "\n".join(lines) + "\nq = cancel",
                )
                ctx.user_data["subtitle_streams"] = subtitles
                UI.activate_prompt(
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
            await UI.send_and_track(ctx, chat_id, "Please enter 1 or 2 (or q to cancel).")
            sent = True
            skip_cleanup = True
        await UI.delete_message_if_present(ctx, chat_id, prompt_id)
        if sent and not skip_cleanup:
            UI.schedule_cleanup(ctx, chat_id, prev_id)
            await UI.update_list_message(ctx, chat_id)
            await UI.update_now_playing_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_movie_index"):
        UI.cancel_prompt_timeout(chat_id, user_id, "await_movie_index")
        ctx.user_data["await_movie_index"] = False
        prompt_id = ctx.user_data.pop("await_movie_msg_id", None)
        movies = ctx.user_data.pop("media_movies", [])
        await UI.delete_message_if_present(ctx, chat_id, msg_id)
        if txt_lower == "q":
            await UI.send_and_track(ctx, chat_id, "Cancelled.")
        elif txt.isdigit():
            i = int(txt) - 1
            if 0 <= i < len(movies):
                movie = movies[i]
                msg = await UI.send_and_track(
                    ctx,
                    chat_id,
                    "🎬 Start movie\n1. From Beginning\n2. Continue\nq = cancel",
                )
                ctx.user_data["media_movie"] = movie
                UI.activate_prompt(
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
                await UI.send_and_track(ctx, chat_id, "That number does not exist.")
        else:
            await UI.send_and_track(ctx, chat_id, "Please enter a number only (or q to cancel).")
        sent = True
        await UI.delete_message_if_present(ctx, chat_id, prompt_id)
        if sent and not skip_cleanup:
            UI.schedule_cleanup(ctx, chat_id, prev_id)
            await UI.update_list_message(ctx, chat_id)
            await UI.update_now_playing_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_movie_start_mode"):
        UI.cancel_prompt_timeout(chat_id, user_id, "await_movie_start_mode")
        ctx.user_data["await_movie_start_mode"] = False
        prompt_id = ctx.user_data.pop("await_movie_start_mode_msg_id", None)
        movie = ctx.user_data.pop("media_movie", {})
        await UI.delete_message_if_present(ctx, chat_id, msg_id)
        if txt_lower == "q":
            await UI.send_and_track(ctx, chat_id, "Cancelled.")
        elif txt in ("1", "2"):
            resume = txt == "2"
            ok = await asyncio.to_thread(UI.kodi_api.play_movie, movie.get("movieid"), resume)
            if ok:
                UI.queue_state.clear_bot_playback_state()
                await UI.send_and_track(ctx, chat_id, f"🎬 Playing: {movie.get('title')}")
            else:
                await UI.send_and_track(ctx, chat_id, "⚠ Movie could not be played.")
        else:
            await UI.send_and_track(ctx, chat_id, "Please enter 1 or 2 (or q to cancel).")
        sent = True
        await UI.delete_message_if_present(ctx, chat_id, prompt_id)
        if sent:
            UI.schedule_cleanup(ctx, chat_id, prev_id)
            await UI.update_list_message(ctx, chat_id)
            await UI.update_now_playing_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_audio_index"):
        UI.cancel_prompt_timeout(chat_id, user_id, "await_audio_index")
        ctx.user_data["await_audio_index"] = False
        prompt_id = ctx.user_data.pop("await_audio_msg_id", None)
        audio_streams = ctx.user_data.pop("audio_streams", [])
        await UI.delete_message_if_present(ctx, chat_id, msg_id)
        if txt_lower == "q":
            await UI.send_and_track(ctx, chat_id, "Cancelled.")
        elif txt.isdigit():
            i = int(txt) - 1
            if 0 <= i < len(audio_streams):
                ok = await asyncio.to_thread(UI.kodi_api.set_audio_stream, audio_streams[i].get("index"))
                if ok:
                    await UI.send_and_track(ctx, chat_id, f"🗣 Audio set: {UI.av_stream_label(audio_streams[i])}")
                else:
                    await UI.send_and_track(ctx, chat_id, "⚠ Audio could not be changed.")
            else:
                await UI.send_and_track(ctx, chat_id, "That number does not exist.")
        else:
            await UI.send_and_track(ctx, chat_id, "Please enter a number only (or q to cancel).")
        sent = True
        await UI.delete_message_if_present(ctx, chat_id, prompt_id)
        if sent:
            UI.schedule_cleanup(ctx, chat_id, prev_id)
            await UI.update_list_message(ctx, chat_id)
            await UI.update_now_playing_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_subtitle_index"):
        UI.cancel_prompt_timeout(chat_id, user_id, "await_subtitle_index")
        ctx.user_data["await_subtitle_index"] = False
        prompt_id = ctx.user_data.pop("await_subtitle_msg_id", None)
        subtitles = ctx.user_data.pop("subtitle_streams", [])
        await UI.delete_message_if_present(ctx, chat_id, msg_id)
        if txt_lower == "q":
            await UI.send_and_track(ctx, chat_id, "Cancelled.")
        elif txt.isdigit():
            i = int(txt) - 1
            if i == 0:
                ok = await asyncio.to_thread(UI.kodi_api.disable_subtitles)
                if ok:
                    await UI.send_and_track(ctx, chat_id, "💬 Subtitles off.")
                else:
                    await UI.send_and_track(ctx, chat_id, "⚠ Subtitles could not be disabled.")
            elif 0 < i <= len(subtitles):
                selected = subtitles[i - 1]
                ok = await asyncio.to_thread(UI.kodi_api.set_subtitle_stream, selected.get("index"))
                if ok:
                    await UI.send_and_track(ctx, chat_id, f"💬 Subtitles set: {UI.av_stream_label(selected)}")
                else:
                    await UI.send_and_track(ctx, chat_id, "⚠ Subtitles could not be changed.")
            else:
                await UI.send_and_track(ctx, chat_id, "That number does not exist.")
        else:
            await UI.send_and_track(ctx, chat_id, "Please enter a number only (or q to cancel).")
        sent = True
        await UI.delete_message_if_present(ctx, chat_id, prompt_id)
        if sent:
            UI.schedule_cleanup(ctx, chat_id, prev_id)
            await UI.update_list_message(ctx, chat_id)
            await UI.update_now_playing_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_show_index"):
        UI.cancel_prompt_timeout(chat_id, user_id, "await_show_index")
        ctx.user_data["await_show_index"] = False
        prompt_id = ctx.user_data.pop("await_show_msg_id", None)
        shows = ctx.user_data.pop("media_shows", [])
        await UI.delete_message_if_present(ctx, chat_id, msg_id)
        if txt_lower == "q":
            await UI.send_and_track(ctx, chat_id, "Cancelled.")
            sent = True
        elif txt.isdigit():
            i = int(txt) - 1
            if 0 <= i < len(shows):
                show = shows[i]
                episodes = await asyncio.to_thread(
                    UI.kodi_api.list_tvshow_episodes,
                    show.get("tvshowid"),
                    show.get("title") or "",
                )
                if not episodes:
                    await UI.send_and_track(ctx, chat_id, "📺 No episodes found for this series.")
                    sent = True
                else:
                    lines = UI.episode_list_lines(episodes)
                    lines.append(f"{len(episodes) + 1}. Play all episodes")
                    msg_ids = await UI.send_chunked_selection(
                        ctx,
                        chat_id,
                        f"📺 {html.escape(show.get('title') or 'Serie', quote=False)}\n",
                        lines,
                    )
                    ctx.user_data["media_show"] = show
                    ctx.user_data["media_episodes"] = episodes
                    UI.activate_prompt(
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
                await UI.send_and_track(ctx, chat_id, "That number does not exist.")
                sent = True
                skip_cleanup = True
        else:
            await UI.send_and_track(ctx, chat_id, "Please enter a number only (or q to cancel).")
            sent = True
            skip_cleanup = True
        await UI.delete_message_if_present(ctx, chat_id, prompt_id)
        if sent and not skip_cleanup:
            UI.schedule_cleanup(ctx, chat_id, prev_id)
            await UI.update_list_message(ctx, chat_id)
            await UI.update_now_playing_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_episode_index"):
        UI.cancel_prompt_timeout(chat_id, user_id, "await_episode_index")
        ctx.user_data["await_episode_index"] = False
        prompt_id = ctx.user_data.pop("await_episode_msg_id", None)
        show = ctx.user_data.pop("media_show", {})
        episodes = ctx.user_data.pop("media_episodes", [])
        await UI.delete_message_if_present(ctx, chat_id, msg_id)
        if txt_lower == "q":
            await UI.send_and_track(ctx, chat_id, "Cancelled.")
        elif txt.isdigit():
            i = int(txt) - 1
            if 0 <= i < len(episodes):
                episode = episodes[i]
                msg = await UI.send_and_track(
                    ctx,
                    chat_id,
                    "📺 Start episode\n1. From Beginning\n2. Continue\nq = cancel",
                )
                ctx.user_data["media_episode"] = episode
                UI.activate_prompt(
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
                    UI.kodi_api.play_all_episodes,
                    [episode.get("episodeid") for episode in episodes],
                )
                if ok:
                    UI.queue_state.clear_bot_playback_state()
                    await UI.send_and_track(ctx, chat_id, f"📺 Playing all episodes: {show.get('title')}")
                else:
                    await UI.send_and_track(ctx, chat_id, "⚠ Episodes could not be played.")
            else:
                await UI.send_and_track(ctx, chat_id, "That number does not exist.")
        else:
            await UI.send_and_track(ctx, chat_id, "Please enter a number only (or q to cancel).")
        sent = True
        await UI.delete_message_if_present(ctx, chat_id, prompt_id)
        if sent and not skip_cleanup:
            UI.schedule_cleanup(ctx, chat_id, prev_id)
            await UI.update_list_message(ctx, chat_id)
            await UI.update_now_playing_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_episode_start_mode"):
        UI.cancel_prompt_timeout(chat_id, user_id, "await_episode_start_mode")
        ctx.user_data["await_episode_start_mode"] = False
        prompt_id = ctx.user_data.pop("await_episode_start_mode_msg_id", None)
        episode = ctx.user_data.pop("media_episode", {})
        await UI.delete_message_if_present(ctx, chat_id, msg_id)
        if txt_lower == "q":
            await UI.send_and_track(ctx, chat_id, "Cancelled.")
        elif txt in ("1", "2"):
            resume = txt == "2"
            ok = await asyncio.to_thread(UI.kodi_api.play_episode, episode.get("episodeid"), resume)
            if ok:
                UI.queue_state.clear_bot_playback_state()
                await UI.send_and_track(ctx, chat_id, f"📺 Playing: {episode.get('title')}")
            else:
                await UI.send_and_track(ctx, chat_id, "⚠ Episode could not be played.")
        else:
            await UI.send_and_track(ctx, chat_id, "Please enter 1 or 2 (or q to cancel).")
        sent = True
        await UI.delete_message_if_present(ctx, chat_id, prompt_id)
        if sent:
            UI.schedule_cleanup(ctx, chat_id, prev_id)
            await UI.update_list_message(ctx, chat_id)
            await UI.update_now_playing_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_playlist_save_name"):
        UI.cancel_prompt_timeout(chat_id, user_id, "await_playlist_save_name")
        ctx.user_data["await_playlist_save_name"] = False
        prompt_id = ctx.user_data.pop("await_playlist_save_msg_id", None)
        if txt_lower == "q":
            await UI.send_and_track(ctx, chat_id, "Cancelled.")
            sent = True
            if prompt_id:
                try:
                    await UI.telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=prompt_id)
                except Exception:
                    pass
            if sent:
                UI.schedule_cleanup(ctx, chat_id, prev_id)
                await UI.update_list_message(ctx, chat_id)
            return
        with UI.queue_state.LOCK:
            items = list(UI.queue_state.QUEUE)
        path = UI.playlist_store.playlist_path_for_name(UI.CFG.playlist_dir, txt)
        if os.path.exists(path):
            msg = await UI.send_and_track(ctx, chat_id, "Playlist already exists. Replace? (y/n, q = cancel)")
            ctx.user_data["playlist_overwrite_name"] = txt
            ctx.user_data["playlist_overwrite_items"] = items
            UI.activate_prompt(
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
                await UI.telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
        else:
            ok, res = UI.playlist_store.save_playlist_to_disk(UI.CFG.playlist_dir, txt, items)
            if ok:
                await UI.send_and_track(ctx, chat_id, f"💾 Saved as {os.path.splitext(res)[0]}")
            else:
                await UI.send_and_track(ctx, chat_id, f"⚠ {res}")
            sent = True
        if prompt_id:
            try:
                await UI.telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=prompt_id)
            except Exception:
                pass
        if sent and not skip_cleanup:
            UI.schedule_cleanup(ctx, chat_id, prev_id)
            await UI.update_list_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_playlist_overwrite_confirm"):
        UI.cancel_prompt_timeout(chat_id, user_id, "await_playlist_overwrite_confirm")
        if txt_lower in ("y", "yes"):
            ctx.user_data["await_playlist_overwrite_confirm"] = False
            prompt_id = ctx.user_data.pop("await_playlist_overwrite_msg_id", None)
            name = ctx.user_data.pop("playlist_overwrite_name", "")
            items = ctx.user_data.pop("playlist_overwrite_items", [])
            ok, res = UI.playlist_store.save_playlist_to_disk_overwrite(UI.CFG.playlist_dir, name, items)
            if ok:
                await UI.send_and_track(ctx, chat_id, f"💾 Saved as {os.path.splitext(res)[0]}")
            else:
                await UI.send_and_track(ctx, chat_id, f"⚠ {res}")
            sent = True
            if prompt_id:
                try:
                    await UI.telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=prompt_id)
                except Exception:
                    pass
            if sent:
                UI.schedule_cleanup(ctx, chat_id, prev_id)
                await UI.update_list_message(ctx, chat_id)
            return
        if txt_lower in ("n", "no"):
            ctx.user_data["await_playlist_overwrite_confirm"] = False
            prompt_id = ctx.user_data.pop("await_playlist_overwrite_msg_id", None)
            ctx.user_data.pop("playlist_overwrite_name", None)
            ctx.user_data.pop("playlist_overwrite_items", None)
            await UI.send_and_track(ctx, chat_id, "Cancelled.")
            sent = True
            if prompt_id:
                try:
                    await UI.telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=prompt_id)
                except Exception:
                    pass
            if sent:
                UI.schedule_cleanup(ctx, chat_id, prev_id)
                await UI.update_list_message(ctx, chat_id)
            return
        if txt_lower == "q":
            ctx.user_data["await_playlist_overwrite_confirm"] = False
            prompt_id = ctx.user_data.pop("await_playlist_overwrite_msg_id", None)
            ctx.user_data.pop("playlist_overwrite_name", None)
            ctx.user_data.pop("playlist_overwrite_items", None)
            await UI.send_and_track(ctx, chat_id, "Cancelled.")
            sent = True
            if prompt_id:
                try:
                    await UI.telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=prompt_id)
                except Exception:
                    pass
            if sent:
                UI.schedule_cleanup(ctx, chat_id, prev_id)
                await UI.update_list_message(ctx, chat_id)
            return
        prompt_id = ctx.user_data.get("await_playlist_overwrite_msg_id")
        if prompt_id:
            UI.activate_prompt(
                ctx,
                chat_id,
                user_id,
                "await_playlist_overwrite_confirm",
                "await_playlist_overwrite_msg_id",
                prompt_id,
                extra_keys=("playlist_overwrite_name", "playlist_overwrite_items"),
            )
        await UI.send_and_track(ctx, chat_id, "Please answer with y or n (or q to cancel).")
        sent = True
        return

    if ctx.user_data.get("await_playlist_load_index"):
        UI.cancel_prompt_timeout(chat_id, user_id, "await_playlist_load_index")
        ctx.user_data["await_playlist_load_index"] = False
        prompt_id = ctx.user_data.pop("await_playlist_load_msg_id", None)
        files = ctx.user_data.pop("playlist_load_files", [])
        if txt_lower == "q":
            await UI.send_and_track(ctx, chat_id, "Cancelled.")
        elif txt.isdigit():
            i = int(txt) - 1
            if 0 <= i < len(files):
                ok, items = UI.playlist_store.load_playlist_from_disk(UI.CFG.playlist_dir, files[i])
                if ok:
                    UI.queue_state.hard_stop_and_clear()
                    UI.queue_state.clear_queue()
                    with UI.queue_state.LOCK:
                        UI.queue_state.QUEUE.extend(items)
                    UI.queue_state.mark_list_dirty()
                    await UI.send_and_track(ctx, chat_id, f"📂 Loaded {os.path.splitext(files[i])[0]}")
                else:
                    await UI.send_and_track(ctx, chat_id, f"⚠ {items}")
            else:
                await UI.send_and_track(ctx, chat_id, "That number does not exist.")
        else:
            await UI.send_and_track(ctx, chat_id, "Please enter a number only (or q to cancel).")
        sent = True
        if prompt_id:
            try:
                await UI.telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=prompt_id)
            except Exception:
                pass
        if sent:
            UI.schedule_cleanup(ctx, chat_id, prev_id)
            await UI.update_list_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_playlist_delete_index"):
        UI.cancel_prompt_timeout(chat_id, user_id, "await_playlist_delete_index")
        ctx.user_data["await_playlist_delete_index"] = False
        prompt_id = ctx.user_data.pop("await_playlist_delete_msg_id", None)
        files = ctx.user_data.pop("playlist_delete_files", [])
        if txt_lower == "q":
            await UI.send_and_track(ctx, chat_id, "Cancelled.")
        elif txt.isdigit():
            i = int(txt) - 1
            if 0 <= i < len(files):
                ok, res = UI.playlist_store.delete_playlist_from_disk(UI.CFG.playlist_dir, files[i])
                if ok:
                    await UI.send_and_track(ctx, chat_id, f"🗑 Deleted {res}")
                else:
                    await UI.send_and_track(ctx, chat_id, f"⚠ {res}")
            else:
                await UI.send_and_track(ctx, chat_id, "That number does not exist.")
        else:
            await UI.send_and_track(ctx, chat_id, "Please enter a number only (or q to cancel).")
        sent = True
        if prompt_id:
            try:
                await UI.telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=prompt_id)
            except Exception:
                pass
        if sent:
            UI.schedule_cleanup(ctx, chat_id, prev_id)
            await UI.update_list_message(ctx, chat_id)
        return

    if ctx.user_data.get("await_play_index"):
        UI.cancel_prompt_timeout(chat_id, user_id, "await_play_index")
        ctx.user_data["await_play_index"] = False
        prompt_id = ctx.user_data.pop("await_play_msg_id", None)
        if txt_lower == "q":
            await UI.send_and_track(ctx, chat_id, "Cancelled.")
        elif txt.isdigit():
            i = int(txt) - 1
            with UI.queue_state.LOCK:
                in_range = 0 <= i < len(UI.queue_state.QUEUE)
            if not in_range:
                await UI.send_and_track(ctx, chat_id, "That number does not exist.")
            elif UI.queue_state.is_requested_track_already_playing(i):
                await UI.send_and_track(ctx, chat_id, "▶ This track is already playing.")
            else:
                UI.queue_state.play_index(i)
                await UI.send_and_track(ctx, chat_id, f"▶ Playing track {txt}.")
        else:
            await UI.send_and_track(ctx, chat_id, "Please enter a number only (or q to cancel).")
        sent = True
        if prompt_id:
            try:
                await UI.telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=prompt_id)
            except Exception:
                pass
        if sent:
            UI.schedule_cleanup(ctx, chat_id, prev_id)
            await UI.update_list_message(ctx, chat_id)
        return
    if ctx.user_data.get("await_favourite_index"):
        UI.cancel_prompt_timeout(chat_id, user_id, "await_favourite_index")
        ctx.user_data["await_favourite_index"] = False
        prompt_id = ctx.user_data.pop("await_favourite_msg_id", None)
        favourites = ctx.user_data.pop("favourites", [])
        if txt_lower == "q":
            await UI.send_and_track(ctx, chat_id, "Cancelled.")
        elif txt.isdigit():
            i = int(txt) - 1
            if 0 <= i < len(favourites):
                selected = favourites[i]
                ok = await asyncio.to_thread(UI.kodi_api.play_favourite_target, selected.get("target"))
                if ok:
                    UI.queue_state.clear_bot_playback_state()
                    await UI.send_and_track(ctx, chat_id, f"⭐ Playing favourite: {selected.get('title')}")
                else:
                    await UI.send_and_track(ctx, chat_id, "⚠ Favourite could not be played.")
            else:
                await UI.send_and_track(ctx, chat_id, "That number does not exist.")
        else:
            await UI.send_and_track(ctx, chat_id, "Please enter a number only (or q to cancel).")
        sent = True
        if prompt_id:
            try:
                await UI.telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=prompt_id)
            except Exception:
                pass
        if sent:
            UI.schedule_cleanup(ctx, chat_id, prev_id)
            await UI.update_list_message(ctx, chat_id)
            await UI.update_now_playing_message(ctx, chat_id)
        return
    if ctx.user_data.get("await_seek_percent"):
        UI.cancel_prompt_timeout(chat_id, user_id, "await_seek_percent")
        ctx.user_data["await_seek_percent"] = False
        prompt_id = ctx.user_data.pop("await_seek_percent_msg_id", None)
        m = re.match(r"^\s*(\d{1,3})\s*%?\s*$", txt)
        if txt_lower == "q":
            await UI.send_and_track(ctx, chat_id, "Cancelled.")
        elif m:
            val = int(m.group(1))
            if 0 <= val <= 100:
                ok = UI.queue_state.seek_percent(val)
                await UI.send_and_track(ctx, chat_id, "⏩ Seeked." if ok else "⚠ Seek failed.")
            else:
                await UI.send_and_track(ctx, chat_id, "Please enter a percentage from 0 to 100 (or q to cancel).")
        else:
            await UI.send_and_track(ctx, chat_id, "Please enter a percentage from 0 to 100 (or q to cancel).")
        sent = True
        if prompt_id:
            try:
                await UI.telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=prompt_id)
            except Exception:
                pass
        if sent:
            UI.schedule_cleanup(ctx, chat_id, prev_id)
            await UI.update_list_message(ctx, chat_id)
        return
    if ctx.user_data.get("await_delete_index"):
        UI.cancel_prompt_timeout(chat_id, user_id, "await_delete_index")
        ctx.user_data["await_delete_index"] = False
        prompt_id = ctx.user_data.pop("await_delete_msg_id", None)
        if txt_lower == "q":
            await UI.send_and_track(ctx, chat_id, "Cancelled.")
        elif txt.isdigit():
            ok, msg = UI.queue_state.delete_index(int(txt) - 1)
            if ok:
                await UI.send_and_track(ctx, chat_id, "🗑 Track deleted.")
            else:
                await UI.send_and_track(ctx, chat_id, msg)
        else:
            await UI.send_and_track(ctx, chat_id, "Please enter a number only (or q to cancel).")
        sent = True
        if prompt_id:
            try:
                await UI.telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=prompt_id)
            except Exception:
                pass
        if sent:
            UI.schedule_cleanup(ctx, chat_id, prev_id)
            await UI.update_list_message(ctx, chat_id)
        return

    uid = update.effective_user.id
    txt = update.message.text.strip()

    if uid in UI.pending:
        UI.cancel_pending_timeout(uid)
        if txt.lower() == "1":
            await UI.queue_state.queue_video_async(UI.pending[uid]["video"])
            await UI.send_and_track(ctx, chat_id, "✔ Track added to the queue.")
            UI.pending.pop(uid)
        elif txt.lower() == "l":
            count = await UI.queue_state.queue_playlist_async(UI.pending[uid]["list"])
            await UI.send_and_track(ctx, chat_id, f"✔ Playlist with {count} tracks added.")
            UI.pending.pop(uid)
        elif txt_lower == "q":
            UI.pending.pop(uid, None)
            await UI.send_and_track(ctx, chat_id, "Cancelled.")
        else:
            await UI.send_and_track(ctx, chat_id, "Please reply with 1, l or q.")
        sent = True
        if sent:
            UI.schedule_cleanup(ctx, chat_id, prev_id)
            await UI.update_list_message(ctx, chat_id)
        return

    sc_set = UI.kodi_api.SC_SET.search(txt)
    if sc_set and UI.queue_state.is_sc_set_url(sc_set.group(0)):
        count = await UI.queue_state.queue_soundcloud_set_async(sc_set.group(0))
        if count > 0:
            await UI.send_and_track(ctx, chat_id, f"✔ SoundCloud set with {count} tracks added.")
        else:
            await UI.send_and_track(ctx, chat_id, "⚠ This SoundCloud set could not be added.")
        sent = True
        if sent:
            UI.schedule_cleanup(ctx, chat_id, prev_id)
            await UI.update_list_message(ctx, chat_id)
        return
    sc = UI.kodi_api.SC.search(txt)
    if not sc:
        scs = UI.kodi_api.SC_SHORT.search(txt)
        if scs:
            try:
                resolved = await asyncio.to_thread(UI.queue_state.resolve_sc_short, scs.group(0))
            except Exception:
                resolved = None
            if resolved and UI.queue_state.is_sc_set_url(resolved):
                count = await UI.queue_state.queue_soundcloud_set_async(resolved)
                if count > 0:
                    await UI.send_and_track(ctx, chat_id, f"✔ SoundCloud set with {count} tracks added.")
                else:
                    await UI.send_and_track(ctx, chat_id, "⚠ This SoundCloud set could not be added.")
                sent = True
                if sent:
                    UI.schedule_cleanup(ctx, chat_id, prev_id)
                    await UI.update_list_message(ctx, chat_id)
                return
            if resolved and UI.queue_state.is_sc_track_url(resolved):
                txt = resolved
                sc = UI.kodi_api.SC.search(txt)
            if not sc:
                await UI.send_and_track(
                    ctx,
                    chat_id,
                    "❌ SoundCloud link could not be added.\n"
                    "The link points to Discover/Playlist or personal content.\n"
                    "Please send the full track link in this format:\n"
                    "https://soundcloud.com/ARTIST/TRACK"
                )
                sent = True
                if sent:
                    UI.schedule_cleanup(ctx, chat_id, prev_id)
                    await UI.update_list_message(ctx, chat_id)
                return
    if sc:
        try:
            item = UI.queue_state.make_soundcloud(sc.group(0))
            UI.queue_state.queue_item(item)
            await UI.send_and_track(ctx, chat_id, "✔ SoundCloud track added to the queue.")
        except Exception:
            await UI.send_and_track(ctx, chat_id, "⚠ This SoundCloud link is not playable.")
        sent = True
        if sent:
            UI.schedule_cleanup(ctx, chat_id, prev_id)
            await UI.update_list_message(ctx, chat_id)
        return

    vid = UI.kodi_api.YT.search(txt)
    pl = UI.kodi_api.PL.search(txt)

    if vid and pl:
        msg = await UI.send_and_track(ctx, chat_id, "1 = Track, L = Playlist, q = cancel")
        UI.activate_pending_choice(ctx, chat_id, uid, msg.message_id, vid.group(1), pl.group(1))
        sent = True
    elif vid:
        await UI.queue_state.queue_video_async(vid.group(1))
        await UI.send_and_track(ctx, chat_id, "✔ Track added to the queue.")
        sent = True
    elif pl:
        count = await UI.queue_state.queue_playlist_async(pl.group(1))
        await UI.send_and_track(ctx, chat_id, f"✔ Playlist with {count} tracks added.")
        sent = True

    if not sent:
        try:
            item = await UI.media.download_social_video_item(txt)
        except Exception as e:
            UI.log.info("SOCIAL VIDEO DOWNLOAD FAIL chat_id=%s message_id=%s err=%s", chat_id, msg_id, e)
            await UI.send_and_track(ctx, chat_id, "⚠ Video link could not be downloaded.")
            UI.schedule_cleanup(ctx, chat_id, prev_id)
            return
        if item is not None:
            try:
                await asyncio.to_thread(UI.queue_state.clear_bot_playback_state)
                await asyncio.to_thread(UI.queue_state.play_item, item)
            except Exception as e:
                UI.log.info("SOCIAL VIDEO PLAY FAIL chat_id=%s message_id=%s err=%s", chat_id, msg_id, e)
                UI.media.cleanup_temp_media(item.get("url"))
                await UI.send_and_track(ctx, chat_id, "⚠ Video link could not be played.")
                UI.schedule_cleanup(ctx, chat_id, prev_id)
                return
            try:
                await UI.telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
            await UI.update_now_playing_message(ctx, chat_id)
            return

    if sent:
        UI.schedule_cleanup(ctx, chat_id, prev_id)
        await UI.update_list_message(ctx, chat_id)
        return

    await UI.warn_and_cleanup_chat(ctx, chat_id, msg_id)
