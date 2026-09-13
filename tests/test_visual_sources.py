"""Tests for the Party Video source lists and the upload cleanup guard."""

import dataclasses
import os
import sys

os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "test:token")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from kodibot.core import kodi_api as _kodi_api  # noqa: F401  (bricht den Importkreis)
from kodibot.core import kodi_library
from kodibot.telegram import media


def upload_dir(tmp_path, monkeypatch):
    """Point both halves of the path mapping at temporary directories."""
    local = tmp_path / "uploads"
    local.mkdir()
    # CFG is a frozen dataclass, so swap in a modified copy.
    monkeypatch.setattr(
        media,
        "CFG",
        dataclasses.replace(media.CFG, upload_dir=str(local), kodi_upload_dir="/storage/uploads"),
    )
    return local


class TestUploadListing:
    def test_lists_videos_with_their_kodi_path(self, tmp_path, monkeypatch):
        local = upload_dir(tmp_path, monkeypatch)
        (local / "clip.mp4").write_bytes(b"x" * 10)

        entries = media.list_upload_videos()

        assert len(entries) == 1
        assert entries[0]["name"] == "clip.mp4"
        assert entries[0]["kodi_path"] == "/storage/uploads/clip.mp4"
        assert entries[0]["size"] == 10

    def test_ignores_non_video_files(self, tmp_path, monkeypatch):
        local = upload_dir(tmp_path, monkeypatch)
        for name in ("a.mp4", "b.jpg", "c.txt", "d.mp3", "e.MKV"):
            (local / name).write_bytes(b"x")

        names = {entry["name"] for entry in media.list_upload_videos()}

        assert names == {"a.mp4", "e.MKV"}

    def test_ignores_directories_and_sorts_by_name(self, tmp_path, monkeypatch):
        local = upload_dir(tmp_path, monkeypatch)
        (local / "sub.mp4").mkdir()
        (local / "b.mp4").write_bytes(b"x")
        (local / "a.mp4").write_bytes(b"x")

        assert [entry["name"] for entry in media.list_upload_videos()] == ["a.mp4", "b.mp4"]

    def test_missing_directory_gives_empty_list(self, tmp_path, monkeypatch):
        monkeypatch.setattr(media, "CFG", dataclasses.replace(media.CFG, upload_dir=str(tmp_path / "gone")))
        assert media.list_upload_videos() == []


class TestUploadCleanup:
    def test_removes_videos_and_reports_the_count(self, tmp_path, monkeypatch):
        local = upload_dir(tmp_path, monkeypatch)
        (local / "a.mp4").write_bytes(b"x")
        (local / "b.mkv").write_bytes(b"x")

        removed, freed = media.cleanup_upload_videos()

        assert removed == 2
        assert freed == 2
        assert list(local.iterdir()) == []

    def test_keeps_the_file_currently_used_as_visual(self, tmp_path, monkeypatch):
        local = upload_dir(tmp_path, monkeypatch)
        (local / "keep.mp4").write_bytes(b"x")
        (local / "drop.mp4").write_bytes(b"x")

        removed, _ = media.cleanup_upload_videos(keep="/storage/uploads/keep.mp4")

        assert removed == 1
        assert (local / "keep.mp4").exists()
        assert not (local / "drop.mp4").exists()

    def test_leaves_other_file_types_alone(self, tmp_path, monkeypatch):
        local = upload_dir(tmp_path, monkeypatch)
        (local / "photo.jpg").write_bytes(b"x")
        (local / "clip.mp4").write_bytes(b"x")

        media.cleanup_upload_videos()

        assert (local / "photo.jpg").exists()
        assert not (local / "clip.mp4").exists()

    def test_never_follows_a_symlink_out_of_the_upload_directory(self, tmp_path, monkeypatch):
        """A movie reachable through a symlink must survive — the guard is the point."""
        local = upload_dir(tmp_path, monkeypatch)
        movies = tmp_path / "MOVIES"
        movies.mkdir()
        treasure = movies / "important.mkv"
        treasure.write_bytes(b"do not delete")
        (local / "link.mkv").symlink_to(treasure)

        media.cleanup_upload_videos()

        assert treasure.exists(), "cleanup followed a symlink out of the upload directory"
        assert treasure.read_bytes() == b"do not delete"

    def test_never_deletes_through_a_directory_symlink(self, tmp_path, monkeypatch):
        local = upload_dir(tmp_path, monkeypatch)
        movies = tmp_path / "MOVIES"
        movies.mkdir()
        (movies / "important.mkv").write_bytes(b"keep")
        (local / "shortcut").symlink_to(movies, target_is_directory=True)

        media.cleanup_upload_videos()

        assert (movies / "important.mkv").exists()

    def test_missing_directory_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(media, "CFG", dataclasses.replace(media.CFG, upload_dir=str(tmp_path / "gone")))
        assert media.cleanup_upload_videos() == (0, 0)


class TestMovieListing:
    def test_marks_hevc_and_keeps_the_file_path(self, monkeypatch):
        answer = {
            "result": {
                "movies": [
                    {
                        "title": "Akira",
                        "file": "/media/MOVIES/akira.mkv",
                        "streamdetails": {"video": [{"codec": "h264"}]},
                    },
                    {
                        "title": "Dune",
                        "file": "/media/MOVIES/dune.mkv",
                        "streamdetails": {"video": [{"codec": "hevc"}]},
                    },
                ]
            }
        }
        monkeypatch.setattr(kodi_library.KA, "kodi_call", lambda *a, **k: answer)

        movies = kodi_library.list_movies_for_visual()

        assert movies[0]["title"] == "Akira"
        assert movies[0]["file"] == "/media/MOVIES/akira.mkv"
        assert movies[0]["slow"] is False
        assert movies[1]["slow"] is True

    def test_skips_entries_without_a_file(self, monkeypatch):
        answer = {"result": {"movies": [{"title": "Ghost"}, {"title": "Ok", "file": "/x.mkv"}]}}
        monkeypatch.setattr(kodi_library.KA, "kodi_call", lambda *a, **k: answer)

        assert [m["title"] for m in kodi_library.list_movies_for_visual()] == ["Ok"]

    def test_error_answer_gives_empty_list(self, monkeypatch):
        monkeypatch.setattr(kodi_library.KA, "kodi_call", lambda *a, **k: {"error": {"message": "nope"}})
        assert kodi_library.list_movies_for_visual() == []

    def test_unknown_codec_is_not_marked_slow(self, monkeypatch):
        answer = {"result": {"movies": [{"title": "X", "file": "/x.mkv", "streamdetails": {}}]}}
        monkeypatch.setattr(kodi_library.KA, "kodi_call", lambda *a, **k: answer)
        assert kodi_library.list_movies_for_visual()[0]["slow"] is False


class TestVisualListLines:
    def test_movie_lines_are_numbered_and_mark_slow_codecs(self):
        from kodibot.telegram import panel

        lines = panel.visual_movie_lines([
            {"title": "Akira", "file": "/a.mkv", "codec": "h264", "slow": False},
            {"title": "Dune", "file": "/d.mkv", "codec": "hevc", "slow": True},
        ])

        assert lines[0].startswith("1.")
        assert "Akira" in lines[0]
        assert "⚠" not in lines[0]
        assert lines[1].startswith("2.")
        assert "⚠" in lines[1]

    def test_upload_lines_show_name_and_size(self):
        from kodibot.telegram import panel

        lines = panel.visual_upload_lines([
            {"name": "clip.mp4", "kodi_path": "/storage/uploads/clip.mp4", "size": 5 * 1024 * 1024},
        ])

        assert lines[0].startswith("1.")
        assert "clip.mp4" in lines[0]
        assert "5" in lines[0] and "MB" in lines[0]

    def test_human_size_rounds_sensibly(self):
        from kodibot.telegram import panel

        assert panel.human_size(0) == "0 MB"
        assert panel.human_size(1024 * 1024) == "1 MB"
        assert panel.human_size(1536 * 1024 * 1024) == "1.5 GB"


class TestTempMediaGuard:
    """cleanup_temp_media must not delete the file the visual is using."""

    def register(self, tmp_path, monkeypatch, name="clip.mp4"):
        local = upload_dir(tmp_path, monkeypatch)
        path = local / name
        path.write_bytes(b"x")
        key = f"https://t.me/{name}"
        media.register_temp_entry([key], title=name, kind="video", cleanup_paths=(str(path),))
        return key, path

    def test_deletes_when_no_visual_is_running(self, tmp_path, monkeypatch):
        monkeypatch.setattr(media, "visual_source", lambda: "")
        key, path = self.register(tmp_path, monkeypatch)

        assert media.cleanup_temp_media(key) is True
        assert not path.exists()

    def test_keeps_the_file_that_is_running_as_visual(self, tmp_path, monkeypatch):
        monkeypatch.setattr(media, "visual_source", lambda: "/storage/uploads/clip.mp4")
        key, path = self.register(tmp_path, monkeypatch)

        media.cleanup_temp_media(key)

        assert path.exists(), "cleanup removed the file the visual is playing"

    def test_deletes_a_different_file_while_a_visual_runs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(media, "visual_source", lambda: "/storage/uploads/other.mp4")
        key, path = self.register(tmp_path, monkeypatch)

        media.cleanup_temp_media(key)

        assert not path.exists()

    def test_a_broken_status_lookup_does_not_block_cleanup(self, tmp_path, monkeypatch):
        def boom():
            raise RuntimeError("addon unreachable")

        monkeypatch.setattr(media, "visual_source", boom)
        key, path = self.register(tmp_path, monkeypatch)

        media.cleanup_temp_media(key)

        assert not path.exists()
