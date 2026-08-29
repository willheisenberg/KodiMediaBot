"""Control panel rendering, list/now-playing message updates, and status caches.

Extracted from ui.py to reduce file size.  All functions reference
shared state via ``telegram_state`` (S) and call telegram_rate helpers.
"""

import asyncio
import html
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from kodibot.core import kodi_api
from kodibot.core import queue_state
from kodibot.core import homeassistant as ha
from kodibot.config import CFG
from kodibot.telegram.i18n import LANG, localized_asset, repeat_mode_label, t
from kodibot.telegram import state as S
from kodibot.telegram.rate import (
    telegram_request,
    telegram_request_delete,
    send_and_track,
    delete_message_if_present,
)

log = logging.getLogger(__name__)

# Queue list paging.  Twenty entries at up to TITLE_MAX_LEN visible characters
# stay well inside Telegram's 4096-character message limit.
PAGE_SIZE = 20
TITLE_MAX_LEN = 120

PANEL_STATUS_MIN_WIDTH = 67
PANEL_STATUS_MIN_WIDTH_IDLE = 67
PANEL_STATUS_MIN_WIDTH_NO_PREVIEW = 124

BUTTON_REFERENCE_PATH = localized_asset(
    "panel_button_reference.png",
    "panel_button_reference_de.png",
)
LEGACY_UI_STATE_FILE = "/data/playlists/telegram_ui_state.json"


# ── Error classification ─────────────────────────────────────────────

def should_recreate_after_edit_error(err):
    from telegram.error import BadRequest
    if not isinstance(err, BadRequest):
        return False
    txt = str(err).lower()
    if "message is not modified" in txt:
        return False
    return ("message to edit not found" in txt) or ("message_id_invalid" in txt)


def is_not_modified_error(err):
    from telegram.error import BadRequest
    return isinstance(err, BadRequest) and ("message is not modified" in str(err).lower())


# ── UI state persistence ─────────────────────────────────────────────

def save_ui_state():
    try:
        data = {
            "language": LANG,
            "list_msg_id": S.LIST_MSG_ID,
            "panel_msg_id": S.PANEL_MSG_ID,
            "first_bot_id": S.FIRST_BOT_ID,
            "last_bot_id": S.LAST_BOT_ID,
            "prev_bot_id": S.PREV_BOT_ID,
            "last_cleanup_id": S.LAST_CLEANUP_ID,
        }
        parent = os.path.dirname(CFG.ui_state_file)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = f"{CFG.ui_state_file}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, CFG.ui_state_file)
    except Exception as e:
        log.warning("UI state save fail file=%s err=%s", CFG.ui_state_file, e)


def load_ui_state():
    state_file = CFG.ui_state_file
    migrated_from_legacy = False
    if (
        state_file != LEGACY_UI_STATE_FILE
        and not os.path.exists(state_file)
        and os.path.exists(LEGACY_UI_STATE_FILE)
    ):
        state_file = LEGACY_UI_STATE_FILE
        migrated_from_legacy = True
        log.info("UI state using legacy file for migration file=%s", state_file)
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        log.warning("UI state load fail file=%s err=%s", state_file, e)
        return
    chats = set()
    stored_language = data.get("language")
    if stored_language and stored_language != LANG:
        log.info("UI state language changed stored=%s current=%s", stored_language, LANG)
    for store_name, target in (
        ("list_msg_id", S.LIST_MSG_ID),
        ("panel_msg_id", S.PANEL_MSG_ID),
        ("first_bot_id", S.FIRST_BOT_ID),
        ("last_bot_id", S.LAST_BOT_ID),
        ("prev_bot_id", S.PREV_BOT_ID),
        ("last_cleanup_id", S.LAST_CLEANUP_ID),
    ):
        raw = data.get(store_name, {})
        target.clear()
        for k, v in raw.items():
            target[int(k)] = v
            chats.add(int(k))
    log.info("UI state loaded file=%s chats=%d", state_file, len(chats))
    if migrated_from_legacy:
        save_ui_state()
        log.info("UI state migrated file=%s", CFG.ui_state_file)


# ── Status text helpers ──────────────────────────────────────────────

def resolve_airplay_status_text(status):
    if status == "On":
        return t("airplay_on")
    if status == "Off":
        return t("airplay_off")
    if S.HIFI_STATUS_CACHE == t("hifi_standby_status"):
        return t("airplay_off")
    return t("airplay_unknown")


# ── Control panel markup ─────────────────────────────────────────────

def panel_menu_mode(chat_id=None):
    if chat_id is None:
        return "main"
    return S.PANEL_MENU_MODE.get(chat_id, "main")


def set_panel_menu_mode(chat_id, mode):
    S.PANEL_MENU_MODE[chat_id] = mode if mode in {"main", "controls"} else "main"


def build_main_control_panel(play_label):
    rows = [
        [
            InlineKeyboardButton("⏮", callback_data="back"),
            InlineKeyboardButton(play_label, callback_data="playpause"),
            InlineKeyboardButton("⏭", callback_data="skip"),
        ],
        [
            InlineKeyboardButton("▶ №", callback_data="play:ask"),
            InlineKeyboardButton("⏹", callback_data="stop"),
        ],
        [
            InlineKeyboardButton(t("controls"), callback_data="controls:menu"),
        ],
    ]
    if CFG.panel_show_volume:
        rows.append([
            InlineKeyboardButton("🔉 -5", callback_data="vol:down5"),
            InlineKeyboardButton("🔊 +5", callback_data="vol:up5"),
            InlineKeyboardButton("🔉 -10", callback_data="vol:down10"),
            InlineKeyboardButton("🔊 +10", callback_data="vol:up10"),
        ])
    rows.append([
        InlineKeyboardButton("⭐", callback_data="fav:ask"),
        InlineKeyboardButton("🎬", callback_data="media:ask"),
        InlineKeyboardButton("🗣", callback_data="av:ask"),
    ])
    if CFG.panel_show_hifi:
        rows.append([
            InlineKeyboardButton(t("hifi_on"), callback_data="hifi:on"),
            InlineKeyboardButton(t("hifi_off"), callback_data="hifi:off"),
        ])
    if CFG.panel_show_display:
        rows.append([
            InlineKeyboardButton(t("display_on", label=CFG.display_button_label), callback_data="display:on"),
            InlineKeyboardButton(t("display_off", label=CFG.display_button_label), callback_data="display:off"),
        ])
    # AirPlay Kill is a CEC command and stands on its own; Home Assistant
    # additionally needs a reachable HA instance.
    extras = []
    if CFG.panel_show_airplay:
        extras.append(InlineKeyboardButton(t("airplay_kill"), callback_data="airplay:kill"))
    if CFG.panel_show_ha and ha.ha_available():
        extras.append(InlineKeyboardButton(t("home_assistant"), callback_data="ha:menu"))
    if extras:
        rows.append(extras)
    return rows


def build_controls_panel():
    return [
        [
            InlineKeyboardButton("-10s", callback_data="seek:-10s"),
            InlineKeyboardButton("-30s", callback_data="seek:-30s"),
            InlineKeyboardButton("+10s", callback_data="seek:+10s"),
            InlineKeyboardButton("+30s", callback_data="seek:+30s"),
        ],
        [
            InlineKeyboardButton("-1m", callback_data="seek:-1m"),
            InlineKeyboardButton("-5m", callback_data="seek:-5m"),
            InlineKeyboardButton("-10m", callback_data="seek:-10m"),
            InlineKeyboardButton("+1m", callback_data="seek:+1m"),
            InlineKeyboardButton("+5m", callback_data="seek:+5m"),
            InlineKeyboardButton("+10m", callback_data="seek:+10m"),
        ],
        [
            InlineKeyboardButton(t("percent_seek"), callback_data="seek:percent"),
            InlineKeyboardButton(t("repeat"), callback_data="repeat"),
        ],
        [
            InlineKeyboardButton("🗑 №", callback_data="delete:ask"),
            InlineKeyboardButton(t("delete_first"), callback_data="delete:first"),
            InlineKeyboardButton(t("delete_last"), callback_data="delete:last"),
            InlineKeyboardButton(t("delete_all"), callback_data="deleteall"),
        ],
        [
            InlineKeyboardButton("🎶 💾", callback_data="plist:save"),
            InlineKeyboardButton("🎶 - 🗑", callback_data="plist:delete"),
            InlineKeyboardButton("🎶 📂", callback_data="plist:load"),
        ],
        [
            InlineKeyboardButton("📻 🔍", callback_data="radio:ask"),
            InlineKeyboardButton("📻/📺+⭐", callback_data="radio:favorite"),
            InlineKeyboardButton("⭐ - 🗑", callback_data="radio:delete:ask"),
        ],
        [
            InlineKeyboardButton("📺 🔍", callback_data="tv:ask"),
        ],
        [
            InlineKeyboardButton(t("buttons"), callback_data="help:show"),
        ],
        [
            InlineKeyboardButton(t("back"), callback_data="controls:back"),
        ],
    ]


def control_panel(chat_id=None, *, mode=None):
    """Build the inline keyboard control panel markup."""
    play_label = "⏸" if kodi_api.WS_STATE == "playing" else "▶"
    resolved_mode = mode or panel_menu_mode(chat_id)
    rows = build_controls_panel() if resolved_mode == "controls" else build_main_control_panel(play_label)
    return InlineKeyboardMarkup(rows)


def build_panel_status_parts(progress_text=None, min_width=PANEL_STATUS_MIN_WIDTH):
    status_parts = []
    hifi_text = S.HIFI_STATUS_CACHE
    if CFG.panel_show_hifi:
        status_parts.append(hifi_text)
    if CFG.panel_show_airplay:
        status_parts.append(S.AIRPLAY_STATUS_CACHE)
    status_parts.append(t("repeat_status", mode=repeat_mode_label(queue_state.REPEAT_MODE)))
    if CFG.panel_show_volume and hifi_text != t("hifi_standby_status"):
        status_parts.append(S.DENON_VOLUME_CACHE)
    if progress_text:
        status_parts.append(f"⏱ {progress_text}")
    status_flag_disabled = not (
        CFG.panel_show_hifi
        and CFG.panel_show_airplay
        and CFG.panel_show_volume
    )
    if status_flag_disabled:
        status_width = sum(len(part) for part in status_parts) + 3 * (len(status_parts) - 1)
        filler_width = min_width - status_width - 3
        if filler_width > 0:
            status_parts.append("─" * filler_width)
    return status_parts


def panel_status_min_width(text, progress_text=None):
    if progress_text:
        return PANEL_STATUS_MIN_WIDTH_NO_PREVIEW
    if text == t("nothing_playing_plain"):
        return PANEL_STATUS_MIN_WIDTH_IDLE
    if "<a href=" in text:
        return PANEL_STATUS_MIN_WIDTH
    return PANEL_STATUS_MIN_WIDTH_NO_PREVIEW


def button_reference_markup():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t("hide"), callback_data="help:hide")]]
    )


async def hide_button_reference(ctx, chat_id):
    """Remove the button reference image if one is on screen."""
    msg_id = S.HELP_MSG_ID.pop(chat_id, None)
    if not msg_id:
        return False
    await delete_message_if_present(ctx, chat_id, msg_id)
    return True


async def show_button_reference(ctx, chat_id):
    """Send the button reference image with its own hide button.

    Any image still on screen is dropped first.  Pressing the button twice
    therefore moves the image back down next to the panel instead of stacking
    a second copy, and it recovers when the message was deleted by hand.
    """
    await hide_button_reference(ctx, chat_id)
    photo = S.HELP_PHOTO_FILE_ID
    try:
        if photo:
            msg = await telegram_request(
                ctx.bot.send_photo,
                chat_id=chat_id,
                photo=photo,
                reply_markup=button_reference_markup(),
            )
        else:
            with open(BUTTON_REFERENCE_PATH, "rb") as fh:
                msg = await telegram_request(
                    ctx.bot.send_photo,
                    chat_id=chat_id,
                    photo=fh,
                    reply_markup=button_reference_markup(),
                )
    except FileNotFoundError:
        log.warning("Button reference image missing at %s", BUTTON_REFERENCE_PATH)
        return False
    except Exception as e:
        log.warning("Button reference send failed chat_id=%s err=%s", chat_id, e)
        return False
    S.HELP_MSG_ID[chat_id] = msg.message_id
    # Remember the uploaded file so later presses cost one id instead of 1.5 MB.
    if not S.HELP_PHOTO_FILE_ID and getattr(msg, "photo", None):
        S.HELP_PHOTO_FILE_ID = msg.photo[-1].file_id
    return True


def cancel_markup():
    return InlineKeyboardMarkup([[InlineKeyboardButton(t("cancel"), callback_data="prompt:cancel")]])


def delete_confirm_markup(token=None):
    yes_callback = f"delete_confirm:{token}:yes" if token else "delete_confirm:yes"
    no_callback = f"delete_confirm:{token}:no" if token else "delete_confirm:no"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(t("yes"), callback_data=yes_callback),
        InlineKeyboardButton(t("no"), callback_data=no_callback),
    ]])


# ── Formatting helpers ───────────────────────────────────────────────

def visible_length(text):
    """Length of text the way Telegram counts it: entities parsed, tags gone.

    The 4096-character limit applies to the rendered text, not to the HTML we
    send.  Measuring the raw string would count every ``<a href="...">`` and cut
    pages far shorter than necessary.
    """
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


def truncate_title(title):
    """Cap a single title so one pathological entry cannot blow the page."""
    if len(title) <= TITLE_MAX_LEN:
        return title
    return title[:TITLE_MAX_LEN - 1] + "…"


def page_count(total):
    """Number of pages for a queue of ``total`` items (never less than one)."""
    if total <= 0:
        return 1
    return (total + PAGE_SIZE - 1) // PAGE_SIZE


def resolve_page(chat_id, total):
    """Return the page to render, clamped, and remember it for the chat.

    Until someone pages manually the page follows the playing track, so the
    marker stays visible.  Clamping matters because the queue shrinks: sitting
    on page 8 when 100 tracks are removed would otherwise render an empty page.
    """
    pages = page_count(total)
    if S.LIST_PAGE_PINNED.get(chat_id):
        page = S.LIST_PAGE.get(chat_id, 0)
    else:
        page = (queue_state.DISPLAY_INDEX or 0) // PAGE_SIZE
    page = max(0, min(page, pages - 1))
    S.LIST_PAGE[chat_id] = page
    return page


def format_item_line(i, it):
    """Format a single queue item as a display line.

    ``i`` is the absolute queue index, never the position within a page: the
    marker test below compares it against DISPLAY_INDEX, and a per-page index
    would paint the marker on every page.
    """
    mark = "▶ " if i == queue_state.DISPLAY_INDEX else ""
    title = html.escape(truncate_title(it.get("title", "")), quote=False)
    link = it.get("link")
    if link:
        safe_link = html.escape(link, quote=True)
        return f'{mark}{i+1}. <a href="{safe_link}">{title}</a>'
    return f"{mark}{i+1}. {title}"


def build_list_text(chat_id):
    """Build one page of the queue list for display."""
    with queue_state.LOCK:
        if not queue_state.QUEUE:
            return t("queue_empty")
        total = len(queue_state.QUEUE)
        pages = page_count(total)
        page = resolve_page(chat_id, total)
        start = page * PAGE_SIZE
        lines = [
            format_item_line(i, it)
            for i, it in enumerate(
                queue_state.QUEUE[start:start + PAGE_SIZE], start=start
            )
        ]
        header = t("playlist") if pages == 1 else t("playlist_page", page=page + 1, pages=pages)
        return header + "\n\n" + "\n".join(lines)


def list_nav_markup(chat_id):
    """Paging buttons for the list message, or None while it fits on one page.

    All three buttons are always present so the row does not reflow as the page
    changes.  At the first and last page the edge button is inert: page_step()
    clamps, the render is identical, and the cache check skips the edit.
    """
    with queue_state.LOCK:
        total = len(queue_state.QUEUE)
    if total == 0 or page_count(total) <= 1:
        return None
    row = [
        InlineKeyboardButton("◀", callback_data="list:prev"),
        InlineKeyboardButton(t("list_current"), callback_data="list:current"),
        InlineKeyboardButton("▶", callback_data="list:next"),
    ]
    return InlineKeyboardMarkup([row])


def list_render_key(chat_id, text):
    """Cache key for the list message.

    Text alone is not enough: paging away from the marker page and back leaves
    the text identical while the keyboard has to gain a "current" button.
    """
    return (
        text,
        S.LIST_PAGE.get(chat_id, 0),
        bool(S.LIST_PAGE_PINNED.get(chat_id)),
    )


def page_step(chat_id, delta):
    """Move the list one page and pin it, so playback stops moving it."""
    with queue_state.LOCK:
        total = len(queue_state.QUEUE)
    pages = page_count(total)
    page = S.LIST_PAGE.get(chat_id, 0) + delta
    S.LIST_PAGE[chat_id] = max(0, min(page, pages - 1))
    S.LIST_PAGE_PINNED[chat_id] = True


def unpin_page(chat_id):
    """Return the list to auto-follow: the next render jumps to the marker."""
    S.LIST_PAGE_PINNED.pop(chat_id, None)


def format_link_line(i, title, link):
    safe_title = html.escape(title, quote=False)
    if link:
        safe_link = html.escape(link, quote=True)
        return f'{i+1}. <a href="{safe_link}">{safe_title}</a>'
    return f"{i+1}. {safe_title}"


def chunk_selection_text(header, lines, footer=None, max_len=3800):
    """Split lines into multiple messages if total text exceeds max_len."""
    chunks = []
    current = header + "\n\n"
    for line in lines:
        candidate = current + line + "\n"
        if len(candidate) > max_len:
            chunks.append(current.rstrip())
            current = line + "\n"
        else:
            current = candidate
    if footer:
        current += "\n" + footer
    chunks.append(current.rstrip())
    return chunks


async def send_chunked_selection(ctx, chat_id, header, lines, footer=None, extra_buttons=None):
    """Send chunked selection text and return list of message IDs."""
    chunks = chunk_selection_text(header, lines, footer)
    msg_ids = []
    for i, chunk in enumerate(chunks):
        markup = None
        if i == len(chunks) - 1:
            rows = []
            if extra_buttons:
                for btn in extra_buttons:
                    if isinstance(btn, InlineKeyboardButton):
                        rows.append([btn])
                    else:
                        label, callback_data = btn
                        rows.append([InlineKeyboardButton(label, callback_data=callback_data)])
            rows.append([InlineKeyboardButton(t("cancel"), callback_data="prompt:cancel")])
            markup = InlineKeyboardMarkup(rows)
        msg = await send_and_track(ctx, chat_id, chunk, parse_mode="HTML", reply_markup=markup)
        msg_ids.append(msg.message_id)
    return msg_ids


async def send_button_selection(ctx, chat_id, text, items, callback_prefix, items_per_row=1):
    """Send a message with items as buttons. items is a list of (label, data_suffix)."""
    rows = []
    for i in range(0, len(items), items_per_row):
        row = []
        for label, suffix in items[i:i + items_per_row]:
            row.append(InlineKeyboardButton(label, callback_data=f"{callback_prefix}:{suffix}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(t("cancel"), callback_data="prompt:cancel")])
    msg = await send_and_track(ctx, chat_id, text, reply_markup=InlineKeyboardMarkup(rows))
    return msg.message_id


async def send_delete_confirmation(ctx, chat_id, text, token=None):
    msg = await send_and_track(ctx, chat_id, text, reply_markup=delete_confirm_markup(token))
    return msg.message_id


async def send_toast_message(ctx, chat_id, text, delay=2):
    """Send a short-lived message that auto-deletes after `delay` seconds (fake toast for text flows)."""
    msg = await telegram_request(
        ctx.bot.send_message,
        chat_id=chat_id,
        text=text,
        disable_web_page_preview=True,
    )

    async def _auto_delete():
        await asyncio.sleep(delay)
        try:
            await telegram_request_delete(ctx.bot.delete_message, chat_id=chat_id, message_id=msg.message_id)
        except Exception as e:
            log.info("TOAST DELETE FAIL chat_id=%s message_id=%s err=%s", chat_id, msg.message_id, e)

    if hasattr(ctx, "application"):
        ctx.application.create_task(_auto_delete())
    else:
        asyncio.get_running_loop().create_task(_auto_delete())
    return msg


def movie_list_lines(movies):
    lines = []
    thirty_days_ago = datetime.now() - timedelta(days=30)
    for i, movie in enumerate(movies):
        title = movie.get("label") or movie.get("title") or t("unknown")
        year = movie.get("year")
        if year:
            title = f"{title} ({year})"
        
        ctime = movie.get("ctime")
        if ctime:
            dt = datetime.fromtimestamp(ctime)
            if dt > thirty_days_ago:
                title = f"🆕 {title}"
        else:
            dateadded_str = movie.get("dateadded")
            if dateadded_str:
                try:
                    dateadded_clean = dateadded_str.replace(" ", "T")
                    dt = datetime.fromisoformat(dateadded_clean[:19])
                    if dt > thirty_days_ago:
                        title = f"🆕 {title}"
                except Exception:
                    pass
                
        lines.append(format_link_line(i, title, kodi_api.build_imdb_link(movie)))
    return lines


def show_list_lines(shows):
    lines = []
    thirty_days_ago = datetime.now() - timedelta(days=30)
    for i, show in enumerate(shows):
        title = show.get("label") or show.get("title") or t("unknown")
        year = show.get("year")
        if year:
            title = f"{title} ({year})"
            
        ctime = show.get("ctime")
        if ctime:
            dt = datetime.fromtimestamp(ctime)
            if dt > thirty_days_ago:
                title = f"🆕 {title}"
        else:
            dateadded_str = show.get("dateadded")
            if dateadded_str:
                try:
                    dateadded_clean = dateadded_str.replace(" ", "T")
                    dt = datetime.fromisoformat(dateadded_clean[:19])
                    if dt > thirty_days_ago:
                        title = f"🆕 {title}"
                except Exception:
                    pass
                
        lines.append(format_link_line(i, title, kodi_api.build_imdb_link(show)))
    return lines


def episode_list_lines(episodes):
    lines = []
    thirty_days_ago = datetime.now() - timedelta(days=30)
    for i, ep in enumerate(episodes):
        season = ep.get("season", "?")
        number = ep.get("episode", "?")
        prefix = ""
        if isinstance(season, int) and isinstance(number, int):
            prefix = f"S{season:02d}E{number:02d} "
        else:
            prefix = f"S{season}E{number} "
        title = f"{prefix}{ep.get('label') or ep.get('title') or t('unknown')}".strip()
        
        ctime = ep.get("ctime")
        if ctime:
            dt = datetime.fromtimestamp(ctime)
            if dt > thirty_days_ago:
                title = f"🆕 {title}"
        else:
            dateadded_str = ep.get("dateadded")
            if dateadded_str:
                try:
                    dateadded_clean = dateadded_str.replace(" ", "T")
                    dt = datetime.fromisoformat(dateadded_clean[:19])
                    if dt > thirty_days_ago:
                        title = f"🆕 {title}"
                except Exception:
                    pass
                
        lines.append(format_link_line(i, title, kodi_api.build_imdb_link(ep)))
    return lines


from kodibot.telegram.languages import LANG_MAP


def resolve_single_lang(code):
    """Resolve a single language code to its flag and friendly name."""
    code = code.strip().lower()
    if code in LANG_MAP:
        return LANG_MAP[code]
    # Try splitting by - or _ for sub-tags (e.g. en-ca -> en)
    subparts = re.split(r'[-_]', code)
    if subparts and subparts[0] in LANG_MAP:
        return LANG_MAP[subparts[0]]
    return None


def format_av_track(idx, name, lang, codec=None, channels=None):
    """Format an audio or subtitle track with flag and clear language name."""
    name = (name or "").strip()
    lang = (lang or "").strip()
    
    # Try to extract language from name if empty (e.g. "DD+5.1(ger)")
    if not lang and name:
        match = re.search(r'[([（]([a-zA-Z]{2,3})[)\]）]', name)
        if match:
            lang = match.group(1)
            
    lang_display = ""
    lang_names = []
    if lang:
        # Split by / or + or | (multiple language audio tracks)
        parts = re.split(r'[/+|]', lang)
        resolved_parts = []
        for p in parts:
            p = p.strip()
            if not p:
                continue
            res = resolve_single_lang(p)
            if res:
                resolved_parts.append(res)
            else:
                resolved_parts.append(("🏳", p.upper()))
                
        # Deduplicate identical adjacent languages (e.g. de-de -> Deutsch)
        unique_resolved = []
        for flag, lang_name in resolved_parts:
            if not unique_resolved or unique_resolved[-1] != (flag, lang_name):
                unique_resolved.append((flag, lang_name))
                
        if unique_resolved:
            lang_display = " / ".join(f"{flag} {lang_name}" for flag, lang_name in unique_resolved)
            lang_names = [lang_name for _, lang_name in unique_resolved]
            
    display_name = name
    if display_name and lang_display:
        lower_name = display_name.lower()
        # Redundant if the track name is just the language code or name
        is_redundant = (
            lower_name == lang.lower() or 
            any(lower_name == ln.lower() for ln in lang_names)
        )
        if is_redundant:
            display_name = ""
            
    parts = []
    if lang_display:
        parts.append(lang_display)
    if display_name:
        parts.append(display_name)
    if codec:
        parts.append(codec)
    if channels:
        parts.append(f"{channels}ch")
        
    label = " · ".join(parts) or t("track_fallback", index=idx)
    return label


def av_stream_label(stream):
    """Format an audio/subtitle stream for display."""
    idx = stream.get("index")
    label = format_av_track(
        idx,
        stream.get("name"),
        stream.get("language"),
        stream.get("codec"),
        stream.get("channels")
    )
    return f"{idx}. {label}"


def current_subtitle_label(av_state):
    if not av_state.get("subtitleenabled"):
        return t("off")
    sub = av_state.get("currentsubtitle") or {}
    idx = sub.get("index")
    if idx is None:
        return t("off")
    return format_av_track(idx, sub.get("name"), sub.get("language"))


# ── List/panel update ────────────────────────────────────────────────

async def send_info_list_panel(ctx, chat_id):
    """Send the queue list and control panel messages."""
    out = build_list_text(chat_id)
    list_msg = await send_and_track(
        ctx, chat_id, out, parse_mode="HTML", reply_markup=list_nav_markup(chat_id)
    )
    S.LIST_MSG_ID[chat_id] = list_msg.message_id
    set_panel_menu_mode(chat_id, "main")
    panel_msg = await send_and_track(ctx, chat_id, t("now_playing_title"), reply_markup=control_panel(chat_id))
    S.PANEL_MSG_ID[chat_id] = panel_msg.message_id
    save_ui_state()


async def update_list_message(ctx, chat_id):
    msg_id = S.LIST_MSG_ID.get(chat_id)
    text = build_list_text(chat_id)
    markup = list_nav_markup(chat_id)
    key = list_render_key(chat_id, text)
    if S.LIST_RENDER_CACHE.get(chat_id) == key and msg_id:
        queue_state.LIST_DIRTY = False
        return
    if not msg_id:
        if S.PANEL_MSG_ID.get(chat_id):
            list_msg = await send_and_track(
                ctx, chat_id, text, parse_mode="HTML", reply_markup=markup
            )
            S.LIST_MSG_ID[chat_id] = list_msg.message_id
            S.LIST_RENDER_CACHE[chat_id] = key
            save_ui_state()
        else:
            await send_info_list_panel(ctx, chat_id)
        return
    try:
        await telegram_request(
            ctx.bot.edit_message_text,
            chat_id=chat_id,
            message_id=msg_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=markup,
        )
    except Exception as e:
        if is_not_modified_error(e):
            S.LIST_RENDER_CACHE[chat_id] = key
            queue_state.LIST_DIRTY = False
            return
        if not should_recreate_after_edit_error(e):
            # Clear the flag even though the edit failed.  Leaving it set makes
            # the worker retry the identical render on its next pass, which
            # turns one rejected message into an unbounded retry loop.
            log.info("List edit fail chat_id=%s message_id=%s err=%s", chat_id, msg_id, e)
            queue_state.LIST_DIRTY = False
            return
        list_msg = await send_and_track(
            ctx, chat_id, text, parse_mode="HTML", reply_markup=markup
        )
        S.LIST_MSG_ID[chat_id] = list_msg.message_id
        S.LIST_RENDER_CACHE[chat_id] = key
        save_ui_state()
        queue_state.LIST_DIRTY = False
    else:
        S.LIST_RENDER_CACHE[chat_id] = key
        queue_state.LIST_DIRTY = False


async def get_now_playing_text():
    """Assemble the now-playing display text. Returns (html_text, progress_text)."""
    name = None
    link = None
    qitem = None
    with queue_state.LOCK:
        if not queue_state.EXTERNAL_PLAYBACK and queue_state.DISPLAY_INDEX is not None and 0 <= queue_state.DISPLAY_INDEX < len(queue_state.QUEUE):
            qitem = queue_state.QUEUE[queue_state.DISPLAY_INDEX]
            name = qitem.get("title") or None
            link = qitem.get("link")

    # Optimistic early return: the bot just triggered a track change. Skip all
    # Kodi HTTP calls and show the queue info immediately. Two conditions cover
    # the two media types:
    #  • BOT_EXPECTING_WS > 0  → YouTube (plugin is slow, WS hasn't fired yet)
    #  • within PLAY_INDEX_OPTIMISTIC_WINDOW → SoundCloud / fast plugins where
    #    the WS event fires before we even reach this function, so
    #    BOT_EXPECTING_WS is already 0 but we still want the fast display.
    _optimistic = (
        queue_state.get_expecting_ws() > 0
        or (time.time() - queue_state.PLAY_INDEX_TS < queue_state.PLAY_INDEX_OPTIMISTIC_WINDOW)
    )
    if _optimistic and name:
        safe_name = html.escape(name, quote=False)
        if link:
            safe_link = html.escape(link, quote=True)
            return f'▶ <a href="{safe_link}">{safe_name}</a>', None
        return f"▶ {safe_name}", None

    players = await kodi_api.kodi_call_async("Player.GetActivePlayers")
    players = (players or {}).get("result", [])
    if not players:
        if kodi_api.WS_PLAYING and name:
            safe_name = html.escape(name, quote=False)
            if link:
                safe_link = html.escape(link, quote=True)
                return f'▶ <a href="{safe_link}">{safe_name}</a>', None
            return f"▶ {safe_name}", None
        if kodi_api.WS_PLAYING and not name:
            return t("playing_plain"), None
        queue_state.EXTERNAL_PLAYBACK = False
        if name:
            safe_name = html.escape(name, quote=False)
            if link:
                safe_link = html.escape(link, quote=True)
                return f'▶ <a href="{safe_link}">{safe_name}</a>', None
            return f"▶ {safe_name}", None
        return t("nothing_playing_plain"), None

    pid = None
    if kodi_api.LAST_WS_PLAYERID is not None:
        for p in players:
            if p.get("playerid") == kodi_api.LAST_WS_PLAYERID:
                pid = kodi_api.LAST_WS_PLAYERID
                break
    if pid is None:
        pid = kodi_api.pick_playerid(players)
    if pid is None:
        queue_state.EXTERNAL_PLAYBACK = False
        return t("nothing_playing_plain"), None

    # Fetch properties and item in parallel to halve the Kodi round-trip time.
    need_item = bool(qitem or not name)
    item = {}
    if need_item:
        props_resp, item_resp = await asyncio.gather(
            kodi_api.kodi_call_async(
                "Player.GetProperties",
                {"playerid": pid, "properties": ["time", "totaltime"]},
            ),
            kodi_api.kodi_call_async(
                "Player.GetItem",
                {
                    "playerid": pid,
                    "properties": [
                        "title", "artist", "file", "showtitle", "season",
                        "episode", "album", "channel", "imdbnumber",
                        "uniqueid", "year", "originaltitle",
                    ],
                },
            ),
        )
        props = (props_resp or {}).get("result", {})
        item = (item_resp or {}).get("result", {}).get("item", {})
        kodi_api.maybe_cache_soundcloud_url(item.get("file"))

        ws_id = kodi_api.LAST_WS_ITEM.get("id")
        ws_type = kodi_api.LAST_WS_ITEM.get("type")
        ws_title = kodi_api.LAST_WS_ITEM.get("title")
        if ws_id is not None and ws_type:
            lib_item = kodi_api.fetch_library_item(ws_type, ws_id)
            if lib_item:
                lib_item["type"] = ws_type
                if item:
                    item = {**item, **lib_item}
                else:
                    item = lib_item
        if not item and ws_title:
            item = {"type": ws_type, "title": ws_title}

        item_has_identity = bool(
            item and any(item.get(key) for key in ("file", "title", "label", "channel"))
        )
        if qitem and item_has_identity:
            if kodi_api.kodi_item_matches_queue(item, qitem):
                queue_state.EXTERNAL_PLAYBACK = False
                name = qitem.get("title") or None
                link = qitem.get("link")
            else:
                queue_state.clear_bot_playback_state()
                name = None
                link = None

        if not name:
            name, link = kodi_api.external_item_display(item)
        if name == "Unknown":
            name = t("unknown")
        if not name:
            name = t("unknown")
    else:
        # Name is known (e.g. external playback already identified), but we
        # still need time/totaltime for the progress display.
        props = (await kodi_api.kodi_call_async(
            "Player.GetProperties",
            {"playerid": pid, "properties": ["time", "totaltime"]},
        )).get("result", {})

    cur = kodi_api.format_kodi_time(props.get("time"))
    total = kodi_api.format_kodi_time(props.get("totaltime"))
    queue_state.LAST_PROGRESS_TS = time.time()
    queue_state.LAST_PROGRESS_TIME = props.get("time")
    queue_state.LAST_PROGRESS_TOTAL = props.get("totaltime")
    queue_state.LAST_PROGRESS_INDEX = queue_state.DISPLAY_INDEX
    safe_name = html.escape(name, quote=False)
    progress_text = f"{cur} / {total}"
    if link:
        safe_link = html.escape(link, quote=True)
        return f'▶ <a href="{safe_link}">{safe_name}</a>', progress_text
    return f"▶ {safe_name}", progress_text


async def update_now_playing_message(ctx, chat_id):
    """Update or create the now-playing panel message."""
    msg_id = S.PANEL_MSG_ID.get(chat_id)
    text, progress_text = await get_now_playing_text()
    min_width = panel_status_min_width(text, progress_text)
    status_parts = build_panel_status_parts(progress_text, min_width=min_width)
    full_text = f"{t('now_playing_title')}\n{text}\n{' | '.join(status_parts)}"
    panel_markup = control_panel(chat_id)
    render_sig = (
        full_text,
        tuple(
            tuple((btn.text, btn.callback_data) for btn in row)
            for row in panel_markup.inline_keyboard
        ),
    )
    if S.PANEL_RENDER_CACHE.get(chat_id) == render_sig and msg_id:
        return
    if not msg_id:
        panel_msg = await send_and_track(
            ctx, chat_id, full_text,
            reply_markup=panel_markup, parse_mode="HTML",
        )
        S.PANEL_MSG_ID[chat_id] = panel_msg.message_id
        S.PANEL_RENDER_CACHE[chat_id] = render_sig
        save_ui_state()
        return
    try:
        await telegram_request(
            ctx.bot.edit_message_text,
            chat_id=chat_id, message_id=msg_id,
            text=full_text, parse_mode="HTML",
            reply_markup=panel_markup,
        )
    except Exception as e:
        if is_not_modified_error(e):
            S.PANEL_RENDER_CACHE[chat_id] = render_sig
            return
        if not should_recreate_after_edit_error(e):
            log.info("Panel edit fail chat_id=%s message_id=%s err=%s", chat_id, msg_id, e)
            return
        panel_msg = await send_and_track(
            ctx, chat_id, full_text,
            reply_markup=panel_markup, parse_mode="HTML",
        )
        S.PANEL_MSG_ID[chat_id] = panel_msg.message_id
        S.PANEL_RENDER_CACHE[chat_id] = render_sig
        save_ui_state()
    else:
        S.PANEL_RENDER_CACHE[chat_id] = render_sig


# ── Status cache refreshers ──────────────────────────────────────────

async def refresh_hifi_status_cache(force=False):
    now = time.time()
    if not force and now - S.HIFI_STATUS_TS < 300:
        return
    status = await asyncio.to_thread(kodi_api.get_hifi_power_status)
    if status == "On":
        S.HIFI_STATUS_CACHE = t("hifi_on_status")
    elif status == "Standby" or (status is None and bool(CFG.denon_host)):
        S.HIFI_STATUS_CACHE = t("hifi_standby_status")
        S.AIRPLAY_STATUS_CACHE = t("airplay_off")
    else:
        S.HIFI_STATUS_CACHE = t("hifi_unknown_status")
    S.HIFI_STATUS_TS = now


async def refresh_airplay_status_cache(force=False):
    now = time.time()
    if not force and now - S.AIRPLAY_STATUS_TS < 15:
        return
    if S.HIFI_STATUS_CACHE == t("hifi_standby_status"):
        S.AIRPLAY_STATUS_CACHE = t("airplay_off")
        S.AIRPLAY_STATUS_TS = now
        return
    status = await asyncio.to_thread(kodi_api.get_airplay_status)
    S.AIRPLAY_STATUS_CACHE = resolve_airplay_status_text(status)
    S.AIRPLAY_STATUS_TS = now


async def refresh_denon_volume_cache(force=False):
    now = time.time()
    if not force and now - S.DENON_VOLUME_TS < 60:
        return
    if S.HIFI_STATUS_CACHE == t("hifi_standby_status"):
        S.DENON_VOLUME_CACHE = "🔊 --"
        S.DENON_VOLUME_TS = now
        return
    vol = await asyncio.to_thread(kodi_api.get_denon_mainzone_volume)
    if vol is None:
        S.DENON_VOLUME_CACHE = "🔊 --"
    else:
        try:
            rel = float(vol)
            abs_vol = max(0.0, min(98.0, rel + 80.0))
            if abs(abs_vol - round(abs_vol)) < 1e-6:
                S.DENON_VOLUME_CACHE = f"🔊 {int(round(abs_vol))}"
            else:
                S.DENON_VOLUME_CACHE = f"🔊 {abs_vol:.1f}"
        except Exception:
            S.DENON_VOLUME_CACHE = f"🔊 {vol}"
    S.DENON_VOLUME_TS = now
