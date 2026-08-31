"""Tests for playlist_store.py"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from kodibot.core import playlist_store


class TestSanitizePlaylistName:
    def test_removes_path_separators(self):
        result = playlist_store.sanitize_playlist_name("../../evil")
        assert "/" not in result
        assert ".." not in result

    def test_strips_whitespace(self):
        result = playlist_store.sanitize_playlist_name("  test  ")
        assert result == "test"

    def test_empty_string(self):
        result = playlist_store.sanitize_playlist_name("")
        assert result == "playlist"  # falls back to default name


class TestPlaylistPersistence:
    def test_save_load_delete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            items = [
                {"title": "Song A", "url": "http://a.com", "kind": "audio"},
                {"title": "Song B", "url": "http://b.com", "kind": "video"},
            ]
            ok, res = playlist_store.save_playlist_to_disk(tmpdir, "test_playlist", items)
            assert ok, f"Save failed: {res}"

            files = playlist_store.list_playlist_files(tmpdir)
            assert len(files) == 1

            ok2, loaded = playlist_store.load_playlist_from_disk(tmpdir, files[0])
            assert ok2
            assert len(loaded) == 2
            assert loaded[0]["title"] == "Song A"

            ok3, del_res = playlist_store.delete_playlist_from_disk(tmpdir, files[0])
            assert ok3

            assert len(playlist_store.list_playlist_files(tmpdir)) == 0

    def test_list_playlist_files_ignores_internal_ui_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "fav.json"), "w", encoding="utf-8") as f:
                f.write("{}")
            with open(os.path.join(tmpdir, "telegram_ui_state.json"), "w", encoding="utf-8") as f:
                f.write("{}")

            assert playlist_store.list_playlist_files(tmpdir) == ["fav.json"]
