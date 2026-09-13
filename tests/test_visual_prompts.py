"""Tests for the Party Video text prompts (URL, movie and upload selection)."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "test:token")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from kodibot.telegram import ui as UI  # noqa: F401  (bricht den Importkreis ui <-> ui_text)
from kodibot.telegram import ui_text


@pytest.fixture
def harness(monkeypatch):
    """Stub the UI layer and record what the addon bridge was asked to do."""
    mock = MagicMock()
    mock.delete_message_if_present = AsyncMock()
    mock.send_toast_message = AsyncMock()
    mock.update_list_message = AsyncMock()
    mock.update_now_playing_message = AsyncMock()
    mock.t = lambda key, **kw: key
    monkeypatch.setattr(ui_text, "UI", mock)

    async def run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(ui_text.asyncio, "to_thread", run_inline)

    calls = []
    monkeypatch.setattr(ui_text.partyvideo, "play_url", lambda url: calls.append(("url", url)))
    monkeypatch.setattr(ui_text.partyvideo, "play_path", lambda path: calls.append(("path", path)))
    mock.calls = calls
    return mock


def make_update(text):
    update = MagicMock()
    update.effective_chat.id = 42
    update.effective_user.id = 7
    update.message.message_id = 100
    update.message.text = text
    return update


def make_ctx(**user_data):
    ctx = MagicMock()
    ctx.user_data = dict(user_data)
    return ctx


def toast_keys(mock):
    return [call.args[2] for call in mock.send_toast_message.await_args_list]


class TestUrlPrompt:
    @pytest.mark.asyncio
    async def test_valid_link_is_handed_to_the_addon(self, harness):
        ctx = make_ctx(await_visual_url=True, await_visual_url_msg_id=55)
        await ui_text.handle_text(make_update("https://youtu.be/zbo6jUGrwdk"), ctx)
        assert harness.calls == [("url", "https://youtu.be/zbo6jUGrwdk")]
        assert "visual_set" in toast_keys(harness)

    @pytest.mark.asyncio
    async def test_other_link_is_refused_without_calling_the_addon(self, harness):
        ctx = make_ctx(await_visual_url=True)
        await ui_text.handle_text(make_update("https://soundcloud.com/a/b"), ctx)
        assert harness.calls == []
        assert "visual_invalid_url" in toast_keys(harness)

    @pytest.mark.asyncio
    async def test_q_cancels(self, harness):
        ctx = make_ctx(await_visual_url=True)
        await ui_text.handle_text(make_update("q"), ctx)
        assert harness.calls == []
        assert "cancelled_dot" in toast_keys(harness)

    @pytest.mark.asyncio
    async def test_prompt_is_cleared_so_the_next_link_is_queued_normally(self, harness):
        ctx = make_ctx(await_visual_url=True)
        await ui_text.handle_text(make_update("https://youtu.be/abc123"), ctx)
        assert ctx.user_data["await_visual_url"] is False


class TestMoviePrompt:
    @pytest.mark.asyncio
    async def test_number_selects_the_movie_file(self, harness):
        ctx = make_ctx(
            await_visual_movie=True,
            visual_movies=[
                {"title": "Akira", "file": "/media/MOVIES/akira.mkv"},
                {"title": "Dune", "file": "/media/MOVIES/dune.mkv"},
            ],
        )
        await ui_text.handle_text(make_update("2"), ctx)
        assert harness.calls == [("path", "/media/MOVIES/dune.mkv")]

    @pytest.mark.asyncio
    async def test_number_out_of_range_is_reported(self, harness):
        ctx = make_ctx(await_visual_movie=True, visual_movies=[{"title": "A", "file": "/a.mkv"}])
        await ui_text.handle_text(make_update("9"), ctx)
        assert harness.calls == []
        assert "that_number_missing" in toast_keys(harness)

    @pytest.mark.asyncio
    async def test_non_number_is_reported(self, harness):
        ctx = make_ctx(await_visual_movie=True, visual_movies=[{"title": "A", "file": "/a.mkv"}])
        await ui_text.handle_text(make_update("Akira"), ctx)
        assert harness.calls == []
        assert "enter_number_or_q" in toast_keys(harness)


class TestUploadPrompt:
    @pytest.mark.asyncio
    async def test_number_selects_the_kodi_path(self, harness):
        ctx = make_ctx(
            await_visual_upload=True,
            visual_uploads=[{"name": "clip.mp4", "kodi_path": "/storage/uploads/clip.mp4"}],
        )
        await ui_text.handle_text(make_update("1"), ctx)
        assert harness.calls == [("path", "/storage/uploads/clip.mp4")]

    @pytest.mark.asyncio
    async def test_zero_is_not_accepted(self, harness):
        ctx = make_ctx(
            await_visual_upload=True,
            visual_uploads=[{"name": "clip.mp4", "kodi_path": "/storage/uploads/clip.mp4"}],
        )
        await ui_text.handle_text(make_update("0"), ctx)
        assert harness.calls == []
        assert "that_number_missing" in toast_keys(harness)


@pytest.mark.asyncio
async def test_disabled_feature_clears_pending_prompt(harness):
    harness.CFG.partyvideo_enabled = False
    ctx = make_ctx(await_visual_url=True, await_visual_url_msg_id=55)
    await ui_text.handle_text(make_update("https://youtu.be/zbo6jUGrwdk"), ctx)
    assert harness.calls == []
    assert not ctx.user_data.get("await_visual_url")
    assert "visual_disabled" in toast_keys(harness)


class TestCancelButton:
    def test_url_prompt_text_no_longer_mentions_q(self):
        from kodibot.telegram import i18n

        for lang in ("de", "en"):
            text = i18n.TEXT[lang]["visual_ask_url"]
            assert " q " not in text and "q)" not in text, f"{lang}: {text}"

    def test_cancel_clearing_covers_visual_prompt_keys(self):
        """prompt:cancel must drop the stored lists, not just the await flags."""
        from kodibot.telegram import ui_callbacks

        source = ui_callbacks.on_button.__code__.co_consts
        flat = [c for c in source if isinstance(c, str)]
        assert "visual_" in flat, "prompt:cancel does not clear visual_* keys"


class TestVideoUploadRouting:
    """A video only becomes the visual while the 📤 prompt is waiting for it."""

    def items(self):
        return [{"kind": "video", "title": "clip", "local_path": "/data/uploads/clip.mp4",
                 "url": "http://x/media/clip.mp4"}]

    def test_waiting_prompt_routes_the_video_to_the_visual(self):
        from kodibot.telegram import ui

        user_data = {"await_visual_video": True}
        assert ui.visual_video_target(user_data, self.items()) == "/data/uploads/clip.mp4"
        assert user_data["await_visual_video"] is False

    def test_without_the_prompt_nothing_is_routed(self):
        from kodibot.telegram import ui

        assert ui.visual_video_target({}, self.items()) is None

    def test_prompt_without_a_video_is_kept_waiting(self):
        from kodibot.telegram import ui

        user_data = {"await_visual_video": True}
        images = [{"kind": "image", "local_path": "/data/uploads/a.jpg"}]
        assert ui.visual_video_target(user_data, images) is None
        assert user_data["await_visual_video"] is True

    def test_item_without_a_local_path_is_ignored(self):
        from kodibot.telegram import ui

        user_data = {"await_visual_video": True}
        items = [{"kind": "video", "title": "x", "url": "http://x/y.mp4"}]
        assert ui.visual_video_target(user_data, items) is None
        assert user_data["await_visual_video"] is True
