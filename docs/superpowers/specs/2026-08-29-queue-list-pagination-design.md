# Paginated queue list

Date: 2026-08-29
Branch: `refactor/modularize`

## Problem

`build_list_text()` renders every queue entry into a single string with no
length limit (`panel.py:338-344`). A 150-track YouTube playlist exceeds
Telegram's 4096-character message limit, so `edit_message_text` raises
`BadRequest: Message_too_long`.

The failure then compounds. `should_recreate_after_edit_error()`
(`panel.py:44-51`) only recognises "message to edit not found" and
"message_id_invalid", so `Message_too_long` takes the branch at
`panel.py:673-675`, which logs and returns **without clearing
`queue_state.LIST_DIRTY`**. The worker loop at `ui.py:441` sees the flag still
set on its next pass and calls `update_list_message()` again. The result is an
unbounded retry loop hammering the Telegram API roughly every two seconds.

Observed in production on 2026-08-29: the loop ran from 14:40:47 until the
container was restarted, over 100 attempts. Because the edit never succeeded,
the queue list message kept showing its previous, shorter content — the tracks
were in the queue (`/health` reported `queue_length: 150`) but invisible. Panel
buttons appeared unresponsive for the same reason: the UI could no longer
repaint.

## Goal

The whole queue stays reachable regardless of length, and no edit failure can
put the bot into a retry loop.

## Approach

Paginate the list message and give it its own navigation buttons. The page
follows the playing track automatically until someone browses, then stays put
until they return to the marker.

Separately and independently, the error path clears `LIST_DIRTY` for every
failure it does not recreate on. This is not a pagination concern — it is what
turns a single failed edit into an outage, and it must hold for error classes
we have not thought of.

Rejected alternatives:

- **Truncate with a "… and N more" footer.** Simplest, but the `▶` marker sits
  at an absolute index (`panel.py:329`). Once playback passes the cut-off the
  marker leaves the visible window and the list silently shows no position at
  all — worse than the current state, because it fails quietly.
- **Split across several messages via `chunk_selection_text()`.** Breaks the
  invariant that a chat has exactly one tracked list message (`LIST_MSG_ID`),
  which cleanup, state persistence and `ui_commands.py:119-166` all rely on.

## Components

### `kodibot/telegram/state.py`

Two new dicts, following the convention that shared telegram-layer state lives
here:

```python
LIST_PAGE = {}          # chat_id -> page index
LIST_PAGE_PINNED = {}   # chat_id -> bool, True once someone paged manually
```

Neither is persisted by `save_ui_state()`. After a restart the list returns to
auto-follow, which is the desired state and avoids migrating the existing
`telegram_ui_state.json`.

### `kodibot/telegram/panel.py`

`PAGE_SIZE = 20`, a module constant.

`build_list_text(chat_id)` gains a parameter and becomes page-aware:

1. Empty queue → `"Queue empty."` unchanged.
2. Resolve the page: if `LIST_PAGE_PINNED.get(chat_id)` is falsy, the page is
   `DISPLAY_INDEX // PAGE_SIZE` (0 when `DISPLAY_INDEX` is `None`), so the
   marker stays visible. Otherwise use the stored `LIST_PAGE`.
3. Clamp the page into `range(0, ceil(len(QUEUE) / PAGE_SIZE))` and write the
   clamped value back to `LIST_PAGE`. Required because the queue shrinks:
   deleting 100 tracks while someone sits on page 8 would otherwise render an
   empty page.
4. Header `🎵 Playlist (3/8):`. Pages are stored zero-based and displayed
   one-based, so stored page `2` renders as `(3/8)`. The counter is omitted for
   a single page, so short queues look exactly as they do today.
5. Lines via `enumerate(QUEUE[start:start + PAGE_SIZE], start=start)`.

**The `start=start` argument is the crux.** `format_item_line()` marks the
current track with `mark = "▶ " if i == queue_state.DISPLAY_INDEX else ""`. With
a zero-based `enumerate` per page, `i == DISPLAY_INDEX` would match some row on
*every* page, painting the marker eight times at wrong positions. With absolute
indices the row exists exactly once across the whole queue, and page numbering
reads `47.` rather than restarting at `1.`.

Individual titles are truncated to 120 characters of visible text (ellipsis
included) before rendering. With `PAGE_SIZE = 20` that caps a page at roughly
2500 visible characters, comfortably inside Telegram's 4096. A single
pathological title must not be able to blow the page limit on its own, or we
would have reintroduced the same bug at lower frequency.

`list_nav_markup(chat_id)` (new) returns the keyboard, or `None` when the queue
fits on one page — in that case the message looks and behaves as it does now.

Above one page the row always holds the same three buttons, in this order:

- `◀` — callback `list:prev`
- `▶ Aktuelle` — callback `list:current`
- `▶` — callback `list:next`

None of them is ever omitted, so the row does not reflow as the page changes
and the buttons stay where the thumb expects them. At the first and last page
the corresponding edge button is inert rather than absent: `page_step()` clamps,
so the page does not move. Telegram has no disabled-button state, which is why
"always present, sometimes inert" is the only way to keep the layout stable.

`send_info_list_panel()` (`panel.py:628-641`) currently rebuilds the list inline
(lines 630-635) instead of calling `build_list_text()`, and omits the
`🎵 Playlist:` header in the process. Replace that block with a
`build_list_text(chat_id)` call and pass `reply_markup=list_nav_markup(chat_id)`.
Without this the first list a chat ever receives would be unpaginated.

`update_list_message()` passes `reply_markup=list_nav_markup(chat_id)` on both
the edit and the recreate path.

### Render cache

`LIST_RENDER_CACHE` compares rendered text to skip redundant edits
(`panel.py:647`). Text alone is no longer a complete description of the message,
because the keyboard can change while the text does not: paging away from the
marker page and back leaves identical text but must now show `▶ Aktuelle`. The
cache entry becomes the tuple `(text, page, pinned)`.

### `kodibot/telegram/ui_callbacks.py`

Three callbacks, following the existing `cmd == "..."` dispatch style:

- `list:prev` / `list:next` — adjust `LIST_PAGE`, set `LIST_PAGE_PINNED = True`
- `list:current` — clear `LIST_PAGE_PINNED`; the page then resolves from
  `DISPLAY_INDEX` on the next render

All three answer the callback query, then call `update_list_message()`.

### Error path

`panel.py:673-675` clears `queue_state.LIST_DIRTY` before returning. The flag
means "the list needs repainting", and after a failed attempt that we are not
going to retry differently, leaving it set only guarantees an identical failure
on the next pass.

The log line stays at `INFO` and keeps naming the error, so a recurring failure
is still visible without being an outage.

## Data flow

```
queue changes / track changes
  └─> queue_state.LIST_DIRTY = True        (queue_state.py:236)
        └─> worker loop                     (ui.py:441)
              └─> update_list_message(ctx, chat_id)
                    ├─ build_list_text(chat_id)  → page resolved, clamped
                    ├─ list_nav_markup(chat_id)  → keyboard or None
                    ├─ cache hit? → clear LIST_DIRTY, return
                    └─ edit_message_text(...)
                         ├─ ok            → cache, clear LIST_DIRTY
                         ├─ not modified  → cache, clear LIST_DIRTY
                         ├─ recreate      → send new, cache, clear LIST_DIRTY
                         └─ other error   → log, clear LIST_DIRTY   ← the fix
```

Button presses set page state and call the same entry point, so there is one
rendering path.

## Scope note

The page is per chat, not per user — everyone in the group sees the same page.
Per-user paging would require one list message per user, which the single
tracked `LIST_MSG_ID` design does not support. Out of scope.

## Testing

In `tests/`, following the existing preamble convention (env defaults set before
importing `kodibot`; there is no `conftest.py`).

Marker correctness, the failure mode most likely to survive review unnoticed:

- with a 150-item queue and `DISPLAY_INDEX = 48`, the `▶` appears on the page
  containing index 48 and on no other page
- rendering every page of that queue yields exactly one `▶` in total
- line numbering on page 3 starts at `47.`, not `1.`

Paging behaviour:

- not pinned: the rendered page follows `DISPLAY_INDEX` across a page boundary
- pinned: the page stays put when `DISPLAY_INDEX` moves
- `list:current` clears the pin and returns to the marker's page
- page clamps when the queue shrinks below the stored page
- a queue fitting on one page yields `list_nav_markup() is None` and a header
  without a page counter

Loop protection:

- `update_list_message()` clears `LIST_DIRTY` when `edit_message_text` raises
  `BadRequest("Message_too_long")` — the regression test for the outage
- the same holds for an arbitrary unexpected exception

Rendering limits:

- a queue of 150 long-titled items renders each page within Telegram's limit,
  measured on visible text (HTML tags do not count toward Telegram's limit, so
  measuring raw string length would truncate far too early)
- an individual title exceeding the per-title budget is truncated
