"""Tests for the paginated queue list message."""

import asyncio
import os
import sys

os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "test:token")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from kodibot.core import queue_state
from kodibot.telegram import panel
from kodibot.telegram import state as S

CHAT = -1001234567890


def make_queue(n, title="Track"):
    return [{"title": f"{title} {i}", "link": f"https://example.com/{i}"} for i in range(n)]


class PaginationBase:
    def setup_method(self):
        self.old_queue = queue_state.QUEUE
        self.old_display = queue_state.DISPLAY_INDEX
        queue_state.QUEUE = []
        queue_state.DISPLAY_INDEX = None
        queue_state.LIST_DIRTY = False
        S.LIST_PAGE.clear()
        S.LIST_PAGE_PINNED.clear()
        S.LIST_MSG_ID.clear()
        S.LIST_RENDER_CACHE.clear()

    def teardown_method(self):
        queue_state.QUEUE = self.old_queue
        queue_state.DISPLAY_INDEX = self.old_display
        queue_state.LIST_DIRTY = False
        S.LIST_PAGE.clear()
        S.LIST_PAGE_PINNED.clear()
        S.LIST_MSG_ID.clear()
        S.LIST_RENDER_CACHE.clear()


class TestMarkerPlacement(PaginationBase):
    """The marker must land on exactly one row, on the right page."""

    def test_marker_on_page_holding_display_index(self):
        queue_state.QUEUE = make_queue(150)
        queue_state.DISPLAY_INDEX = 48

        text = panel.build_list_text(CHAT)

        assert "▶ 49." in text

    def test_no_marker_on_other_pages(self):
        queue_state.QUEUE = make_queue(150)
        queue_state.DISPLAY_INDEX = 48
        S.LIST_PAGE_PINNED[CHAT] = True
        S.LIST_PAGE[CHAT] = 0

        text = panel.build_list_text(CHAT)

        assert "▶" not in text

    def test_marker_appears_exactly_once_across_all_pages(self):
        queue_state.QUEUE = make_queue(150)
        queue_state.DISPLAY_INDEX = 48
        S.LIST_PAGE_PINNED[CHAT] = True

        total = 0
        pages = panel.page_count(len(queue_state.QUEUE))
        for page in range(pages):
            S.LIST_PAGE[CHAT] = page
            total += panel.build_list_text(CHAT).count("▶")

        assert total == 1

    def test_line_numbers_stay_absolute(self):
        queue_state.QUEUE = make_queue(150)
        queue_state.DISPLAY_INDEX = None
        S.LIST_PAGE_PINNED[CHAT] = True
        S.LIST_PAGE[CHAT] = 2

        text = panel.build_list_text(CHAT)

        assert "41." in text
        assert "60." in text
        assert "1. Track 0" not in text


class TestPageSelection(PaginationBase):
    def test_auto_follow_uses_page_of_display_index(self):
        queue_state.QUEUE = make_queue(150)
        queue_state.DISPLAY_INDEX = 48

        panel.build_list_text(CHAT)

        assert S.LIST_PAGE[CHAT] == 2

    def test_auto_follow_moves_across_page_boundary(self):
        queue_state.QUEUE = make_queue(150)
        queue_state.DISPLAY_INDEX = 19
        panel.build_list_text(CHAT)
        assert S.LIST_PAGE[CHAT] == 0

        queue_state.DISPLAY_INDEX = 20
        text = panel.build_list_text(CHAT)

        assert S.LIST_PAGE[CHAT] == 1
        assert "▶ 21." in text

    def test_pinned_page_ignores_display_index(self):
        queue_state.QUEUE = make_queue(150)
        queue_state.DISPLAY_INDEX = 48
        S.LIST_PAGE_PINNED[CHAT] = True
        S.LIST_PAGE[CHAT] = 0

        panel.build_list_text(CHAT)

        assert S.LIST_PAGE[CHAT] == 0

    def test_page_clamped_when_queue_shrinks(self):
        queue_state.QUEUE = make_queue(150)
        S.LIST_PAGE_PINNED[CHAT] = True
        S.LIST_PAGE[CHAT] = 7

        queue_state.QUEUE = make_queue(25)
        text = panel.build_list_text(CHAT)

        assert S.LIST_PAGE[CHAT] == 1
        assert "Track 20" in text


class TestHeader(PaginationBase):
    def test_single_page_header_has_no_counter(self):
        queue_state.QUEUE = make_queue(5)

        text = panel.build_list_text(CHAT)

        assert text.startswith("🎵 Playlist:")

    def test_multi_page_header_shows_one_based_counter(self):
        queue_state.QUEUE = make_queue(150)
        queue_state.DISPLAY_INDEX = 48

        text = panel.build_list_text(CHAT)

        assert text.startswith("🎵 Playlist (3/8):")

    def test_empty_queue_unchanged(self):
        text = panel.build_list_text(CHAT)

        assert text == "Queue empty."


class TestLengthLimits(PaginationBase):
    def test_long_titles_do_not_exceed_telegram_limit(self):
        queue_state.QUEUE = [
            {"title": "T" * 400, "link": "https://example.com/" + "u" * 200}
            for _ in range(150)
        ]
        queue_state.DISPLAY_INDEX = 0

        text = panel.build_list_text(CHAT)

        assert panel.visible_length(text) <= 4096

    def test_overlong_title_is_truncated(self):
        queue_state.QUEUE = [{"title": "T" * 500, "link": None}]

        text = panel.build_list_text(CHAT)

        assert "T" * 500 not in text
        assert "…" in text


class TestNavMarkup(PaginationBase):
    def test_single_page_gets_no_buttons(self):
        queue_state.QUEUE = make_queue(5)
        panel.build_list_text(CHAT)

        assert panel.list_nav_markup(CHAT) is None

    def test_all_three_buttons_on_first_page(self):
        queue_state.QUEUE = make_queue(150)
        queue_state.DISPLAY_INDEX = 0
        panel.build_list_text(CHAT)

        data = [b.callback_data for row in panel.list_nav_markup(CHAT).inline_keyboard for b in row]

        assert data == ["list:prev", "list:current", "list:next"]

    def test_all_three_buttons_on_last_page(self):
        queue_state.QUEUE = make_queue(150)
        queue_state.DISPLAY_INDEX = 149
        panel.build_list_text(CHAT)

        data = [b.callback_data for row in panel.list_nav_markup(CHAT).inline_keyboard for b in row]

        assert data == ["list:prev", "list:current", "list:next"]

    def test_all_three_buttons_when_not_pinned(self):
        queue_state.QUEUE = make_queue(150)
        queue_state.DISPLAY_INDEX = 48
        panel.build_list_text(CHAT)

        data = [b.callback_data for row in panel.list_nav_markup(CHAT).inline_keyboard for b in row]

        assert data == ["list:prev", "list:current", "list:next"]

    def test_button_layout_is_stable_across_pages(self):
        queue_state.QUEUE = make_queue(150)
        S.LIST_PAGE_PINNED[CHAT] = True

        layouts = set()
        for page in range(panel.page_count(len(queue_state.QUEUE))):
            S.LIST_PAGE[CHAT] = page
            panel.build_list_text(CHAT)
            mk = panel.list_nav_markup(CHAT)
            layouts.add(tuple(b.callback_data for row in mk.inline_keyboard for b in row))

        assert len(layouts) == 1

    def test_edge_button_is_inert_at_first_page(self):
        queue_state.QUEUE = make_queue(150)
        S.LIST_PAGE_PINNED[CHAT] = True
        S.LIST_PAGE[CHAT] = 0

        panel.page_step(CHAT, -1)

        assert S.LIST_PAGE[CHAT] == 0


class FakeBot:
    async def edit_message_text(self, **kwargs):
        return None


class FakeCtx:
    def __init__(self):
        self.bot = FakeBot()


class TestUpdateListMessageErrorPath(PaginationBase):
    """A failed edit must never leave LIST_DIRTY set: that is the retry loop."""

    def test_message_too_long_clears_list_dirty(self, monkeypatch):
        from telegram.error import BadRequest

        queue_state.QUEUE = make_queue(150)
        S.LIST_MSG_ID[CHAT] = 17
        queue_state.LIST_DIRTY = True

        async def boom(*args, **kwargs):
            raise BadRequest("Message_too_long")

        monkeypatch.setattr(panel, "telegram_request", boom)

        asyncio.run(panel.update_list_message(FakeCtx(), CHAT))

        assert queue_state.LIST_DIRTY is False

    def test_unexpected_error_clears_list_dirty(self, monkeypatch):
        queue_state.QUEUE = make_queue(10)
        S.LIST_MSG_ID[CHAT] = 17
        queue_state.LIST_DIRTY = True

        async def boom(*args, **kwargs):
            raise RuntimeError("something new")

        monkeypatch.setattr(panel, "telegram_request", boom)

        asyncio.run(panel.update_list_message(FakeCtx(), CHAT))

        assert queue_state.LIST_DIRTY is False


class TestRenderCache(PaginationBase):
    def test_markup_change_still_triggers_edit(self, monkeypatch):
        queue_state.QUEUE = make_queue(150)
        queue_state.DISPLAY_INDEX = 48
        S.LIST_MSG_ID[CHAT] = 17

        calls = []

        async def record(fn, **kwargs):
            calls.append(kwargs)

        monkeypatch.setattr(panel, "telegram_request", record)

        # Auto-follow render of page 2.
        queue_state.LIST_DIRTY = True
        asyncio.run(panel.update_list_message(FakeCtx(), CHAT))
        assert len(calls) == 1

        # Same page, same text, but now pinned: the "current" button appears,
        # so the message must still be edited.
        S.LIST_PAGE_PINNED[CHAT] = True
        queue_state.LIST_DIRTY = True
        asyncio.run(panel.update_list_message(FakeCtx(), CHAT))

        assert len(calls) == 2


class TestPageStep(PaginationBase):
    """Paging helpers the callbacks delegate to."""

    def test_next_advances_and_pins(self):
        queue_state.QUEUE = make_queue(150)
        queue_state.DISPLAY_INDEX = 0
        panel.build_list_text(CHAT)

        panel.page_step(CHAT, 1)

        assert S.LIST_PAGE[CHAT] == 1
        assert S.LIST_PAGE_PINNED[CHAT] is True

    def test_prev_goes_back(self):
        queue_state.QUEUE = make_queue(150)
        S.LIST_PAGE_PINNED[CHAT] = True
        S.LIST_PAGE[CHAT] = 3

        panel.page_step(CHAT, -1)

        assert S.LIST_PAGE[CHAT] == 2

    def test_step_clamps_at_last_page(self):
        queue_state.QUEUE = make_queue(150)
        S.LIST_PAGE_PINNED[CHAT] = True
        S.LIST_PAGE[CHAT] = 7

        panel.page_step(CHAT, 1)

        assert S.LIST_PAGE[CHAT] == 7

    def test_step_clamps_at_first_page(self):
        queue_state.QUEUE = make_queue(150)
        S.LIST_PAGE_PINNED[CHAT] = True
        S.LIST_PAGE[CHAT] = 0

        panel.page_step(CHAT, -1)

        assert S.LIST_PAGE[CHAT] == 0

    def test_unpin_returns_to_marker_page(self):
        queue_state.QUEUE = make_queue(150)
        queue_state.DISPLAY_INDEX = 48
        S.LIST_PAGE_PINNED[CHAT] = True
        S.LIST_PAGE[CHAT] = 0

        panel.unpin_page(CHAT)
        text = panel.build_list_text(CHAT)

        assert S.LIST_PAGE_PINNED.get(CHAT) is not True
        assert S.LIST_PAGE[CHAT] == 2
        assert "▶ 49." in text


class TestPageStateWiring(PaginationBase):
    def test_ui_reexports_page_state(self):
        from kodibot.telegram import ui as UI

        assert UI.LIST_PAGE is S.LIST_PAGE
        assert UI.LIST_PAGE_PINNED is S.LIST_PAGE_PINNED
