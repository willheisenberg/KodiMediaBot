import asyncio
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from kodibot.core import homeassistant as ha
from kodibot.config import CFG
from kodibot.telegram.i18n import state_label, t
from kodibot.telegram import state as S
from kodibot.telegram.panel import (
    should_recreate_after_edit_error,
    is_not_modified_error,
)
from kodibot.telegram.rate import (
    telegram_request,
    send_and_track,
    delete_message_if_present,
)

log = logging.getLogger(__name__)

HA_MENU_MSG_ID = S.HA_MENU_MSG_ID
HA_MENU_TIMEOUT_SECONDS = S.HA_MENU_TIMEOUT_SECONDS
HA_MENU_TIMEOUT_TASKS = S.HA_MENU_TIMEOUT_TASKS


def cancel_ha_menu_timeout(chat_id):
    task = HA_MENU_TIMEOUT_TASKS.pop(chat_id, None)
    if task is not None and not task.done():
        task.cancel()


async def _expire_ha_menu_timeout(ctx, chat_id, expected_message_id):
    try:
        await asyncio.sleep(HA_MENU_TIMEOUT_SECONDS)
        if HA_MENU_MSG_ID.get(chat_id) != expected_message_id:
            return
        HA_MENU_MSG_ID.pop(chat_id, None)
        await delete_message_if_present(ctx, chat_id, expected_message_id)
    except asyncio.CancelledError:
        return
    finally:
        if HA_MENU_TIMEOUT_TASKS.get(chat_id) is asyncio.current_task():
            HA_MENU_TIMEOUT_TASKS.pop(chat_id, None)


def arm_ha_menu_timeout(ctx, chat_id, message_id):
    HA_MENU_MSG_ID[chat_id] = message_id
    cancel_ha_menu_timeout(chat_id)
    HA_MENU_TIMEOUT_TASKS[chat_id] = ctx.application.create_task(
        _expire_ha_menu_timeout(ctx, chat_id, message_id)
    )


def touch_ha_menu_timeout(ctx, chat_id, message_id):
    if HA_MENU_MSG_ID.get(chat_id) != message_id:
        return
    arm_ha_menu_timeout(ctx, chat_id, message_id)


async def close_ha_menu_message(ctx, chat_id, message_id=None):
    tracked_id = HA_MENU_MSG_ID.get(chat_id)
    target_id = message_id or tracked_id
    if tracked_id == target_id:
        HA_MENU_MSG_ID.pop(chat_id, None)
        cancel_ha_menu_timeout(chat_id)
    if target_id:
        await delete_message_if_present(ctx, chat_id, target_id)


def format_ha_state_text(state):
    if not state:
        return ""
    status = state_label(state.get("state", "unknown"))
    name = CFG.ha_light_id or state.get("friendly_name") or t("light_fallback")
    rgb = state.get("rgb_color")
    brightness_pct = ha.brightness_percent_from_ha(state.get("brightness"))
    color_hex = ""
    brightness_text = ""
    if rgb and len(rgb) == 3:
        color_hex = f" | #{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"
    if brightness_pct is not None:
        brightness_text = f" | {brightness_pct}%"
    return t("ha_light_state", name=name, state=status, color_hex=color_hex, brightness=brightness_text)


def saved_color_name(color, index):
    fallback = t("color_fallback", index=index + 1)
    return (color.get("name") or fallback).strip() or fallback


def saved_color_button_label(color, index):
    name = saved_color_name(color, index)
    return name if len(name) <= 20 else f"{name[:17]}..."


def build_main_mini_app_url(bot_username, start_param="", mode="compact"):
    bot_username = (bot_username or "").strip().lstrip("@")
    start_param = (start_param or "").strip()
    mode = (mode or "").strip()
    if not bot_username:
        return ""
    if start_param and not re.fullmatch(r"[A-Za-z0-9_-]+", start_param):
        return ""
    if mode and mode not in {"compact", "fullscreen"}:
        return ""

    params = ["startapp" if not start_param else f"startapp={start_param}"]
    if mode:
        params.append(f"mode={mode}")
    return f"https://t.me/{bot_username}?{'&'.join(params)}"


def build_ha_main_menu_markup(*, live_color_button=None, extra_rows=None):
    load_color_button = InlineKeyboardButton(t("load_color"), callback_data="ha:loadcolor")
    brightness_button = InlineKeyboardButton(t("brightness"), callback_data="ha:brightness")
    rows = []
    if live_color_button is not None:
        rows.append([
            InlineKeyboardButton(t("toggle"), callback_data="ha:toggle"),
            live_color_button,
        ])
        rows.append([
            load_color_button,
            InlineKeyboardButton(t("set_hex"), callback_data="ha:sethex"),
        ])
        rows.append([
            brightness_button,
            InlineKeyboardButton(t("save_color"), callback_data="ha:savecolor"),
        ])
    else:
        rows.append([
            InlineKeyboardButton(t("toggle"), callback_data="ha:toggle"),
            load_color_button,
        ])
        rows.append([
            InlineKeyboardButton(t("set_hex"), callback_data="ha:sethex"),
            brightness_button,
        ])
        rows.append([
            InlineKeyboardButton(t("save_color"), callback_data="ha:savecolor"),
        ])
    rows.extend(extra_rows or ())
    rows.append([
        InlineKeyboardButton(t("cancel").replace("❌ ", ""), callback_data="ha:close"),
    ])
    return InlineKeyboardMarkup(rows)


def build_ha_preset_menu_markup(saved_colors):
    rows = [
        [
            InlineKeyboardButton(t("red"), callback_data="ha:color:FF0000"),
            InlineKeyboardButton(t("green"), callback_data="ha:color:00FF00"),
            InlineKeyboardButton(t("blue"), callback_data="ha:color:0000FF"),
        ],
        [
            InlineKeyboardButton(t("yellow"), callback_data="ha:color:FFD700"),
            InlineKeyboardButton(t("purple"), callback_data="ha:color:8B00FF"),
            InlineKeyboardButton(t("orange"), callback_data="ha:color:FF8C00"),
        ],
        [
            InlineKeyboardButton(t("warm_white"), callback_data="ha:color:FFE4B5"),
            InlineKeyboardButton(t("cool_white"), callback_data="ha:color:F0F8FF"),
        ],
        [
            InlineKeyboardButton(t("cyan"), callback_data="ha:color:00FFFF"),
            InlineKeyboardButton(t("pink"), callback_data="ha:color:FF69B4"),
            InlineKeyboardButton(t("brown"), callback_data="ha:color:8B4513"),
        ],
        [
            InlineKeyboardButton(t("disco"), callback_data="ha:effect:colorloop"),
        ],
    ]

    if saved_colors:
        rows.append([
            InlineKeyboardButton(t("saved_colors"), callback_data="ha:noop"),
        ])
        for i, color in enumerate(saved_colors):
            rows.append([
                InlineKeyboardButton(
                    saved_color_button_label(color, i),
                    callback_data=f"ha:savedcolor:{i}",
                ),
            ])
        rows.append([
            InlineKeyboardButton(t("delete_color"), callback_data="ha:deletecolor:ask"),
        ])
    else:
        rows.append([
            InlineKeyboardButton(t("no_saved_colors_yet"), callback_data="ha:noop"),
        ])

    rows.append([
        InlineKeyboardButton(t("back"), callback_data="ha:back"),
        InlineKeyboardButton(t("cancel").replace("❌ ", ""), callback_data="ha:close"),
    ])
    return InlineKeyboardMarkup(rows)


async def _edit_or_send_ha_message(ctx, chat_id, text, reply_markup, *, edit_message_id=None):
    if edit_message_id:
        try:
            await telegram_request(
                ctx.bot.edit_message_text,
                chat_id=chat_id,
                message_id=edit_message_id,
                text=text,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        except Exception as e:
            if is_not_modified_error(e):
                arm_ha_menu_timeout(ctx, chat_id, edit_message_id)
                return edit_message_id
            if not should_recreate_after_edit_error(e):
                log.info("HA menu edit fail chat_id=%s message_id=%s err=%s", chat_id, edit_message_id, e)
                return None
        else:
            arm_ha_menu_timeout(ctx, chat_id, edit_message_id)
            return edit_message_id

    tracked_id = HA_MENU_MSG_ID.get(chat_id)
    if tracked_id and tracked_id != edit_message_id:
        await close_ha_menu_message(ctx, chat_id, tracked_id)

    msg = await send_and_track(
        ctx,
        chat_id,
        text,
        reply_markup=reply_markup,
    )
    arm_ha_menu_timeout(ctx, chat_id, msg.message_id)
    return msg.message_id


async def show_ha_menu(ctx, chat_id, *, chat_type, bot_username, state, edit_message_id=None):
    webapp_url = ha.resolve_ha_webapp_url()
    log.info("HA webapp menu resolved_url=%s explicit_url=%s", webapp_url or "-", CFG.ha_webapp_url or "-")
    main_mini_app_url = build_main_mini_app_url(bot_username, "ha_color", "compact")
    extra_rows = []
    live_color_button = None
    if webapp_url and chat_type == "private":
        live_color_button = InlineKeyboardButton(t("live_color"), web_app=WebAppInfo(url=webapp_url))
        webapp_hint = ""
    elif webapp_url:
        webapp_hint = ""
        if main_mini_app_url:
            extra_rows.append([
                InlineKeyboardButton(t("open_live_color"), url=main_mini_app_url),
            ])
    else:
        webapp_hint = t("ha_mini_app_disabled")

    text = f"{t('ha_light_title')}{format_ha_state_text(state)}{webapp_hint}"
    markup = build_ha_main_menu_markup(
        live_color_button=live_color_button,
        extra_rows=extra_rows,
    )
    await _edit_or_send_ha_message(
        ctx,
        chat_id,
        text,
        markup,
        edit_message_id=edit_message_id,
    )


async def show_ha_preset_menu(ctx, chat_id, *, edit_message_id=None):
    saved_colors = await asyncio.to_thread(ha.load_saved_colors)
    await _edit_or_send_ha_message(
        ctx,
        chat_id,
        t("load_color_text"),
        build_ha_preset_menu_markup(saved_colors),
        edit_message_id=edit_message_id,
    )
