"""Tests for the overlap-free copy of YouTube's automatic captions."""
import os
import sys

os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "test:token")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from kodibot.core import kodi_api, kodi_library, youtube_subs
from kodibot.telegram import yt_subtitles

# What YouTube delivers: every line stays up until the next one ends.
ROLLING = (
    "1\n00:00:01,920 --> 00:00:06,240\nJoe Rogan podcast. Check it out.\n\n"
    "2\n00:00:03,919 --> 00:00:08,400\n>> The Joe Rogan Experience.\n\n"
    "3\n00:00:06,240 --> 00:00:12,240\n>> TRAIN BY DAY.\n"
)

YOUTUBE_FILE = "plugin://plugin.video.youtube/play/?video_id=dYPXINFcvmI"


class TestRemoveCueOverlap:
    def test_each_line_ends_where_the_next_begins(self):
        out = youtube_subs.remove_cue_overlap(ROLLING)

        assert "00:00:01,920 --> 00:00:03,919" in out
        assert "00:00:03,919 --> 00:00:06,240" in out
        # The last line has no successor and keeps its own end.
        assert "00:00:06,240 --> 00:00:12,240" in out

    def test_text_and_numbering_are_untouched(self):
        out = youtube_subs.remove_cue_overlap(ROLLING)

        assert out.count("\n\n") == ROLLING.count("\n\n")
        assert ">> The Joe Rogan Experience." in out
        assert out.startswith("1\n")

    def test_cues_with_a_gap_stay_as_they_are(self):
        srt = (
            "1\n00:00:01,000 --> 00:00:02,000\nA\n\n"
            "2\n00:00:05,000 --> 00:00:06,000\nB\n"
        )

        assert youtube_subs.remove_cue_overlap(srt) == srt

    def test_simultaneous_cues_are_not_collapsed_to_nothing(self):
        srt = (
            "1\n00:00:01,000 --> 00:00:04,000\nA\n\n"
            "2\n00:00:01,000 --> 00:00:04,000\nB\n"
        )

        assert youtube_subs.remove_cue_overlap(srt) == srt


class TestPairCues:
    def _paired(self, srt):
        return youtube_subs.pair_cues(youtube_subs.remove_cue_overlap(srt))

    def test_two_lines_are_shown_together_then_replaced(self):
        srt = ROLLING + "\n4\n00:00:08,400 --> 00:00:14,960\nNIGHT. All day.\n"

        assert self._paired(srt) == (
            "1\n00:00:01,920 --> 00:00:06,240\n"
            "Joe Rogan podcast. Check it out.\n>> The Joe Rogan Experience.\n"
            "\n"
            "2\n00:00:06,240 --> 00:00:14,960\n"
            ">> TRAIN BY DAY.\nNIGHT. All day.\n"
        )

    def test_odd_line_out_stays_alone(self):
        out = self._paired(ROLLING)

        assert out.endswith("2\n00:00:06,240 --> 00:00:12,240\n>> TRAIN BY DAY.\n")

    def test_lines_separated_by_a_pause_are_not_joined(self):
        srt = (
            "1\n00:00:01,000 --> 00:00:02,000\nA\n\n"
            "2\n00:00:05,000 --> 00:00:06,000\nB\n"
        )

        assert self._paired(srt) == srt

    def test_cue_that_already_has_two_lines_is_left_alone(self):
        srt = (
            "1\n00:00:01,000 --> 00:00:02,000\nA\nB\n\n"
            "2\n00:00:02,000 --> 00:00:03,000\nC\n"
        )

        assert self._paired(srt) == srt


class TestOriginalAutoTrackUrl:
    INFO = {
        "subtitles": {"fr": [{"ext": "srt", "url": "http://manual/fr"}]},
        "automatic_captions": {
            "en-orig": [{"ext": "vtt", "url": "http://auto/en.vtt"},
                        {"ext": "srt", "url": "http://auto/en.srt"}],
            "en": [{"ext": "srt", "url": "http://auto/en-tlang.srt"}],
            "de": [{"ext": "srt", "url": "http://auto/de-tlang.srt"}],
            "fr": [{"ext": "srt", "url": "http://auto/fr-tlang.srt"}],
        },
    }

    def test_spoken_language_uses_the_untranslated_track(self):
        assert youtube_subs._original_auto_track_url(self.INFO, "en") == "http://auto/en.srt"

    def test_machine_translation_is_left_to_the_addon(self):
        assert youtube_subs._original_auto_track_url(self.INFO, "de") is None

    def test_uploaded_track_needs_no_repair(self):
        assert youtube_subs._original_auto_track_url(self.INFO, "fr") is None


class TestSelectSubtitle:
    def _wire(self, monkeypatch, tmp_path, file_path, content):
        self.tmp_path = tmp_path
        self.streams = [
            {"index": 0, "language": "ger", "name": "[B]German (translation)[/B]"},
            {"index": 1, "language": "eng", "name": "[B]English (auto-generated)[/B]"},
        ]
        self.current = None
        self.selected = []
        self.attached = []
        self.fetches = []
        self.copies_open = True

        def fake_fetch(video_id, language):
            self.fetches.append((video_id, language))
            return content

        def fake_add(path):
            self.attached.append(path)
            # Kodi names the track after the file's stem, minus the language.
            stem = os.path.basename(path).split(".")[0]
            self.streams.append(
                {"index": len(self.streams), "language": "eng", "name": f"{stem} (External)"}
            )
            return True

        def fake_select(index):
            self.selected.append(self._name(index))
            stream = next(s for s in self.streams if s["index"] == index)
            is_copy = "ytclean" in stream["name"]
            # Kodi answers OK either way and drops the subtitle on failure.
            self.current = stream if self.copies_open or not is_copy else None
            return True

        monkeypatch.setattr(yt_subtitles, "CFG", yt_subtitles.CFG.__class__(**{
            **yt_subtitles.CFG.__dict__,
            "upload_dir": str(tmp_path),
            "kodi_upload_dir": "/kodi/uploads",
        }))
        monkeypatch.setattr(
            kodi_library, "now_playing_media_info", lambda: {"file": file_path}
        )
        monkeypatch.setattr(youtube_subs, "fetch_clean_auto_captions", fake_fetch)
        monkeypatch.setattr(
            kodi_api,
            "get_av_settings",
            lambda: {"subtitles": list(self.streams), "currentsubtitle": self.current},
        )
        monkeypatch.setattr(kodi_api, "add_subtitle_file", fake_add)
        monkeypatch.setattr(kodi_api, "set_subtitle_stream", fake_select)
        monkeypatch.setattr(yt_subtitles.time, "sleep", lambda s: None)

    def _name(self, index):
        return next(s["name"] for s in self.streams if s["index"] == index)

    def _stream(self, name_part):
        return next(s for s in self.streams if name_part in s["name"])

    def _renumber_like_kodi(self):
        """A quality switch re-adds YouTube's tracks behind the attached files."""
        self.streams.sort(key=lambda s: "ytclean" not in s["name"])
        for index, stream in enumerate(self.streams):
            stream["index"] = index

    def test_youtube_track_is_replaced_by_the_clean_copy(self, monkeypatch, tmp_path):
        self._wire(monkeypatch, tmp_path, YOUTUBE_FILE, b"clean")

        assert yt_subtitles.select_subtitle(self._stream("auto-generated"))

        assert self.fetches == [("dYPXINFcvmI", "en")]
        assert self.attached == ["/kodi/uploads/subs/dYPXINFcvmI.1/ytclean1.en.srt"]
        assert (tmp_path / "subs" / "dYPXINFcvmI.1" / "ytclean1.en.srt").read_bytes() == b"clean"
        assert self.selected == ["ytclean1 (External)"]

    def test_picking_the_language_again_reuses_the_copy(self, monkeypatch, tmp_path):
        self._wire(monkeypatch, tmp_path, YOUTUBE_FILE, b"clean")

        yt_subtitles.select_subtitle(self._stream("auto-generated"))
        yt_subtitles.select_subtitle(self._stream("auto-generated"))
        # ... and so does picking the copy itself from the list.
        yt_subtitles.select_subtitle(self._stream("ytclean1"))

        assert len(self.attached) == 1
        assert self.selected == ["ytclean1 (External)"] * 3

    def test_the_copy_is_found_again_after_kodi_renumbers_the_tracks(self, monkeypatch, tmp_path):
        self._wire(monkeypatch, tmp_path, YOUTUBE_FILE, b"clean")
        yt_subtitles.select_subtitle(self._stream("auto-generated"))
        self._renumber_like_kodi()
        self.selected.clear()

        # YouTube's rolling track now sits at the index the copy had before.
        assert self._stream("auto-generated")["index"] == 2
        assert yt_subtitles.select_subtitle(self._stream("auto-generated"))

        assert len(self.attached) == 1
        assert self.selected == ["ytclean1 (External)"]

    def test_copy_deleted_by_a_bot_restart_is_attached_anew(self, monkeypatch, tmp_path):
        self._wire(monkeypatch, tmp_path, YOUTUBE_FILE, b"clean")
        yt_subtitles.select_subtitle(self._stream("auto-generated"))
        (tmp_path / "subs" / "dYPXINFcvmI.1" / "ytclean1.en.srt").unlink()
        self.selected.clear()

        # Kodi still lists the dead track; picking it must not end without subtitles.
        assert yt_subtitles.select_subtitle(self._stream("ytclean1"))

        assert self.attached[-1] == "/kodi/uploads/subs/dYPXINFcvmI.2/ytclean2.en.srt"
        assert self.selected == ["ytclean2 (External)"]
        assert self.current["name"] == "ytclean2 (External)"

    def test_rolling_track_is_shown_when_kodi_opens_no_copy(self, monkeypatch, tmp_path):
        self._wire(monkeypatch, tmp_path, YOUTUBE_FILE, b"clean")
        self.copies_open = False

        assert yt_subtitles.select_subtitle(self._stream("auto-generated"))
        assert self.current["name"] == "[B]English (auto-generated)[/B]"
        # Picking the dead copy itself ends on YouTube's track as well ...
        assert yt_subtitles.select_subtitle(self._stream("ytclean1"))
        assert self.current["name"] == "[B]English (auto-generated)[/B]"
        # ... and the retries stop instead of piling up dead tracks.
        for _ in range(5):
            yt_subtitles.select_subtitle(self._stream("auto-generated"))
        assert len(self.attached) == yt_subtitles.MAX_COPIES

    def test_without_a_clean_copy_the_original_track_is_used(self, monkeypatch, tmp_path):
        self._wire(monkeypatch, tmp_path, YOUTUBE_FILE, None)

        assert yt_subtitles.select_subtitle(self._stream("German"))

        assert self.attached == []
        assert self.selected == ["[B]German (translation)[/B]"]

    def test_library_film_is_never_looked_up_on_youtube(self, monkeypatch, tmp_path):
        self._wire(monkeypatch, tmp_path, "/storage/videos/Film (2020)/Film.mkv", b"clean")

        assert yt_subtitles.select_subtitle(self._stream("auto-generated"))

        assert self.fetches == []
        assert self.selected == ["[B]English (auto-generated)[/B]"]
