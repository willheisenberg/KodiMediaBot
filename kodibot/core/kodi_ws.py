import asyncio
import json
import time

import websockets

from kodibot.core import kodi_api as KA
from kodibot.core import partyvideo, spotify_connect


async def cleanup_image_session_after_stop_delay(stopped_file, delay_s=2.0):
    await asyncio.sleep(delay_s)
    if not KA.media.is_active_image_session_media(stopped_file):
        return
    picture_active = await asyncio.to_thread(KA.is_picture_player_active)
    if not picture_active:
        await asyncio.to_thread(KA.media.cleanup_temp_media, stopped_file)


async def resolve_player_params(player_params):
    """Replace playerid -1 with the player that is actually active.

    Kodi reports -1 for items a Python add-on starts via xbmc.Player().play()
    (e.g. the Spotify Connect stream of service.soloist), and Player.GetItem
    on -1 returns nothing.
    """
    pid = player_params.get("playerid")
    if not isinstance(pid, int) or pid >= 0:
        return player_params
    players = ((await KA.kodi_call_async("Player.GetActivePlayers")) or {}).get("result") or []
    resolved = KA.pick_playerid(players)
    if resolved is None:
        return player_params
    return {**player_params, "playerid": resolved}


def is_youtube_playback_file(file_url):
    """True when Kodi's playing file comes from the YouTube add-on.

    Depending on the stream type Kodi reports the plugin URL itself, the
    add-on's local MPD manifest, or the resolved googlevideo address.
    """
    file_url = file_url or ""
    return (
        file_url.startswith("plugin://plugin.video.youtube/")
        or "/youtube/manifest/" in file_url
        or "googlevideo.com/" in file_url
    )


async def mute_youtube_subtitles(playerid, item):
    """Switch subtitles off when a YouTube video has opened its streams.

    The YouTube add-on attaches a track in Kodi's preferred subtitle language
    (machine-translated when the video only has automatic captions), and Kodi
    shows a matching track on its own. The tracks stay attached, so the
    subtitle menu can still turn one on.
    """
    if playerid is None or not is_youtube_playback_file((item or {}).get("file")):
        return False
    res = await KA.kodi_call_async(
        "Player.SetSubtitle",
        {"playerid": playerid, "subtitle": "off", "enable": False},
    )
    return "error" not in (res or {})


async def kodi_ws_listener():
    ws_url = KA.CFG.kodi_ws_url
    backoff = 3
    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
                KA.WS_CONNECTED = True
                backoff = 3
                if KA.CFG.partyvideo_enabled:
                    await asyncio.to_thread(partyvideo.request_status)
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    method = msg.get("method")
                    if method:
                        KA.log.debug("WS event: method=%s msg=%s", method, msg)
                    if method == "Other.playback_init":
                        data = msg.get("params", {}).get("data", {}) or {}
                        vid = data.get("video_id") or ""
                        playing_file = data.get("playing_file") or ""
                        if vid:
                            KA.LAST_WS_YT_ID = vid
                        if playing_file:
                            KA.LAST_WS_PLAYING_FILE = playing_file
                    if spotify_connect.is_takeover_event(method, msg.get("params")):
                        if KA._ws_on_spotify_takeover:
                            KA._ws_on_spotify_takeover()
                    if method == "Other.partyvideo_status":
                        # Visualisierungs-Addon; Other.partyvideo_cmd ist intern und wird ignoriert.
                        partyvideo.handle_status_event(msg.get("params", {}).get("data", {}) or {})
                    if method in ("Player.OnPlay", "Player.OnAVStart"):
                        KA.WS_PLAYING = True
                        KA.WS_STATE = "playing"
                        KA.WS_LAST_EVENT_TS = time.time()
                        data = msg.get("params", {}).get("data", {}) or {}
                        player_params = data.get("player", {}) or {}
                        item_params = data.get("item", {}) or {}
                        item = None
                        player_params = await resolve_player_params(player_params)
                        if "playerid" in player_params:
                            KA.LAST_WS_PLAYERID = player_params.get("playerid")
                            item = (await KA.kodi_call_async(
                                "Player.GetItem",
                                {
                                    "playerid": KA.LAST_WS_PLAYERID,
                                    "properties": KA.PLAYER_GETITEM_PROPERTIES,
                                },
                            )).get("result", {}).get("item", {})
                            playing_file = (item or {}).get("file") or ""
                            if playing_file:
                                KA.LAST_WS_PLAYING_FILE = playing_file
                        if any(k in item_params for k in ("id", "type", "title")):
                            KA.LAST_WS_ITEM.clear()
                            for k in ("id", "type", "title"):
                                if k in item_params:
                                    KA.LAST_WS_ITEM[k] = item_params.get(k)
                        if item is None:
                            pid = player_params.get("playerid")
                            if pid is not None:
                                item = (await KA.kodi_call_async(
                                    "Player.GetItem",
                                    {"playerid": pid, "properties": KA.PLAYER_GETITEM_PROPERTIES},
                                )).get("result", {}).get("item", {})
                        if KA._ws_on_play:
                            KA._ws_on_play(item=item, item_params=item_params)
                        # Only OnAVStart: at OnPlay the streams are not open
                        # yet and Kodi picks its default track afterwards.
                        if method == "Player.OnAVStart":
                            await mute_youtube_subtitles(
                                player_params.get("playerid"), item
                            )
                        if KA._ws_on_playback_refresh:
                            KA._ws_on_playback_refresh()
                    elif method == "Player.OnPause":
                        KA.WS_PLAYING = False
                        KA.WS_STATE = "paused"
                        KA.WS_LAST_EVENT_TS = time.time()
                        if KA._ws_on_pause:
                            KA._ws_on_pause()
                    elif method == "Player.OnResume":
                        KA.WS_PLAYING = True
                        KA.WS_STATE = "playing"
                        KA.WS_LAST_EVENT_TS = time.time()
                        if KA._ws_on_resume:
                            KA._ws_on_resume()
                    elif method == "Player.OnStop":
                        KA.WS_PLAYING = False
                        KA.WS_STATE = "stopped"
                        KA.WS_LAST_EVENT_TS = time.time()
                        data = msg.get("params", {}).get("data", {}) or {}
                        item_params = data.get("item", {}) or {}
                        player_params = data.get("player", {}) or {}
                        stopped_file = item_params.get("file") or KA.LAST_WS_PLAYING_FILE
                        if KA.media.is_active_image_session_media(stopped_file):
                            asyncio.create_task(cleanup_image_session_after_stop_delay(stopped_file))
                        else:
                            KA.media.cleanup_temp_media(stopped_file)
                        KA.LAST_WS_PLAYING_FILE = ""
                        if KA._ws_on_stop:
                            KA._ws_on_stop(item_params=item_params, player_params=player_params)
        except Exception as e:
            KA.WS_CONNECTED = False
            KA.WS_STATE = "unknown"
            KA.log.warning("WS disconnected or error: %s. Reconnecting in %ds", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
