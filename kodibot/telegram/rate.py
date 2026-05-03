"""Rate-limited Telegram API wrappers and message-tracking helpers.

Provides ``telegram_request`` / ``telegram_request_delete`` for serialised,
rate-limited Telegram API calls, plus convenience helpers like
``send_and_track`` and ``delete_message_if_present``.
"""

import asyncio
import logging
import time

from telegram.error import RetryAfter, TimedOut

from kodibot.telegram import state as S

log = logging.getLogger(__name__)


async def telegram_request(call, *args, **kwargs):
    """Serialise a Telegram API call with rate-limiting and retry."""
    for _ in range(S.TG_MAX_RETRIES):
        async with S.TG_RATE_LOCK:
            now = time.time()
            extra = S.TG_DYNAMIC_DELAY if now < S.TG_DYNAMIC_UNTIL else 0.0
            wait = S.TG_MIN_INTERVAL + extra - (now - S.TG_LAST_TS)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                res = await call(*args, **kwargs)
                S.TG_LAST_TS = time.time()
                return res
            except RetryAfter as e:
                S.TG_LAST_TS = time.time()
                S.TG_DYNAMIC_DELAY = max(S.TG_DYNAMIC_DELAY, min(e.retry_after, 2.0))
                S.TG_DYNAMIC_UNTIL = time.time() + 60.0
                await asyncio.sleep(e.retry_after)
            except TimedOut:
                S.TG_LAST_TS = time.time()
                await asyncio.sleep(1.5)
            except Exception:
                S.TG_LAST_TS = time.time()
                raise
    # Final attempt without catching
    async with S.TG_RATE_LOCK:
        now = time.time()
        extra = S.TG_DYNAMIC_DELAY if now < S.TG_DYNAMIC_UNTIL else 0.0
        wait = S.TG_MIN_INTERVAL + extra - (now - S.TG_LAST_TS)
        if wait > 0:
            await asyncio.sleep(wait)
        res = await call(*args, **kwargs)
        S.TG_LAST_TS = time.time()
        return res


async def telegram_request_delete(call, *args, **kwargs):
    """Serialise a Telegram delete API call with separate rate-limiting."""
    for _ in range(S.TG_MAX_RETRIES):
        async with S.TG_DELETE_RATE_LOCK:
            now = time.time()
            extra = S.TG_DYNAMIC_DELAY if now < S.TG_DYNAMIC_UNTIL else 0.0
            wait = S.TG_DELETE_MIN_INTERVAL + extra - (now - S.TG_DELETE_LAST_TS)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                res = await call(*args, **kwargs)
                S.TG_DELETE_LAST_TS = time.time()
                return res
            except RetryAfter as e:
                S.TG_DELETE_LAST_TS = time.time()
                S.TG_DYNAMIC_DELAY = max(S.TG_DYNAMIC_DELAY, min(e.retry_after, 2.0))
                S.TG_DYNAMIC_UNTIL = time.time() + 60.0
                await asyncio.sleep(e.retry_after)
            except TimedOut:
                S.TG_DELETE_LAST_TS = time.time()
                await asyncio.sleep(1.5)
            except Exception:
                S.TG_DELETE_LAST_TS = time.time()
                raise
    async with S.TG_DELETE_RATE_LOCK:
        now = time.time()
        extra = S.TG_DYNAMIC_DELAY if now < S.TG_DYNAMIC_UNTIL else 0.0
        wait = S.TG_DELETE_MIN_INTERVAL + extra - (now - S.TG_DELETE_LAST_TS)
        if wait > 0:
            await asyncio.sleep(wait)
        res = await call(*args, **kwargs)
        S.TG_DELETE_LAST_TS = time.time()
        return res


async def send_and_track(ctx, chat_id, text, **kwargs):
    """Send a message and track its message_id in shared state."""
    if "disable_web_page_preview" not in kwargs:
        kwargs["disable_web_page_preview"] = True
    msg = await telegram_request(ctx.bot.send_message, chat_id=chat_id, text=text, **kwargs)
    if chat_id not in S.FIRST_BOT_ID:
        S.FIRST_BOT_ID[chat_id] = msg.message_id
    S.PREV_BOT_ID[chat_id] = S.LAST_BOT_ID.get(chat_id)
    S.LAST_BOT_ID[chat_id] = msg.message_id
    log.debug("Bot msg chat_id=%s message_id=%s", chat_id, msg.message_id)
    return msg


async def delete_message_if_present(ctx, chat_id, message_id):
    """Silently delete a message (or list of message ids)."""
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
