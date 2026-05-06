import asyncio
import json
import time

import websockets

from kodibot.core import kodi_api as KA


async def cleanup_image_session_after_stop_delay(stopped_file, delay_s=2.0):
    await asyncio.sleep(delay_s)
    if not KA.media.is_active_image_session_media(stopped_file):
        return
    picture_active = await asyncio.to_thread(KA.is_picture_player_active)
    if not picture_active:
        await asyncio.to_thread(KA.media.cleanup_temp_media, stopped_file)


async def kodi_ws_listener():
    ws_url = KA.CFG.kodi_ws_url
    backoff = 3
    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
                KA.WS_CONNECTED = True
                backoff = 3
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
                    if method in ("Player.OnPlay", "Player.OnAVStart"):
                        KA.WS_PLAYING = True
                        KA.WS_STATE = "playing"
                        KA.WS_LAST_EVENT_TS = time.time()
                        data = msg.get("params", {}).get("data", {}) or {}
                        player_params = data.get("player", {}) or {}
                        item_params = data.get("item", {}) or {}
                        item = None
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
                        stopped_file = item_params.get("file") or KA.LAST_WS_PLAYING_FILE
                        if KA.media.is_active_image_session_media(stopped_file):
                            asyncio.create_task(cleanup_image_session_after_stop_delay(stopped_file))
                        else:
                            KA.media.cleanup_temp_media(stopped_file)
                        KA.LAST_WS_PLAYING_FILE = ""
                        if KA._ws_on_stop:
                            KA._ws_on_stop()
        except Exception as e:
            KA.WS_CONNECTED = False
            KA.WS_STATE = "unknown"
            KA.log.warning("WS disconnected or error: %s. Reconnecting in %ds", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
