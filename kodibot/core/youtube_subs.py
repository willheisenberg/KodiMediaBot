"""Automatic YouTube captions without the roll-up effect.

YouTube times its automatic captions so each line stays up until the *next*
one ends. Kodi then shows two lines at once and the lower one slides up as a
new line arrives. Here every line is cut off where the next one begins, and
the lines are then joined in pairs: two lines that are replaced together.

This module stays free of Telegram imports.
"""

import logging
import re

from yt_dlp import YoutubeDL

log = logging.getLogger(__name__)

TIMEOUT = 10.0
MAX_SUBTITLE_BYTES = 4 * 1024 * 1024
# Longest pause between two lines that may still share one two-line cue.
PAIR_MAX_GAP_MS = 1000

_SRT_TIMING_RE = re.compile(
    r"^(\d{1,2}:\d{2}:\d{2}[,.]\d{3})[ \t]*-->[ \t]*(\d{1,2}:\d{2}:\d{2}[,.]\d{3})(.*)$",
    re.MULTILINE,
)


def _to_ms(stamp):
    hours, minutes, rest = stamp.split(":")
    seconds, millis = re.split(r"[,.]", rest)
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(millis)


def remove_cue_overlap(srt_text):
    """End every SRT cue no later than the next one starts."""
    matches = list(_SRT_TIMING_RE.finditer(srt_text))
    out = []
    pos = 0
    for current, following in zip(matches, matches[1:] + [None], strict=True):
        start, end, tail = current.group(1), current.group(2), current.group(3)
        if following is not None and _to_ms(start) < _to_ms(following.group(1)) < _to_ms(end):
            end = following.group(1)
        out.append(srt_text[pos:current.start()])
        out.append(f"{start} --> {end}{tail}")
        pos = current.end()
    out.append(srt_text[pos:])
    return "".join(out)


def pair_cues(srt_text):
    """Join neighbouring one-line cues into two-line cues.

    A single line of automatic captions is gone after two or three seconds.
    Two lines shown together, then replaced by the next two, stay up for the
    time both take to be spoken. Lines separated by a pause are not joined:
    the second would appear long before anyone says it.
    """
    cues = []
    for block in re.split(r"\n[ \t]*\n", srt_text.replace("\r\n", "\n")):
        timing = _SRT_TIMING_RE.search(block)
        if not timing:
            continue
        text = block[timing.end():].strip("\n")
        if text.strip():
            cues.append((timing.group(1), timing.group(2), text))

    out = []
    i = 0
    while i < len(cues):
        start, end, text = cues[i]
        if i + 1 < len(cues):
            next_start, next_end, next_text = cues[i + 1]
            gap = _to_ms(next_start) - _to_ms(end)
            if "\n" not in text and "\n" not in next_text and 0 <= gap <= PAIR_MAX_GAP_MS:
                end, text = next_end, f"{text}\n{next_text}"
                i += 1
        out.append(f"{len(out) + 1}\n{start} --> {end}\n{text}\n")
        i += 1
    return "\n".join(out)


def _original_auto_track_url(info, language):
    """SRT address of the automatic track in the video's spoken language.

    None when ``language`` has a real, uploaded track (nothing to repair), or
    is only reachable as a machine translation -- YouTube rate-limits those
    hard, so they are left to the YouTube add-on.
    """
    if language in (info.get("subtitles") or {}):
        return None
    for entry in (info.get("automatic_captions") or {}).get(f"{language}-orig") or []:
        if entry.get("ext") == "srt" and entry.get("url"):
            return entry["url"]
    return None


def fetch_clean_auto_captions(video_id, language):
    """Return the video's automatic captions as overlap-free SRT bytes, or None."""
    if not video_id or not language:
        return None
    opts = {"quiet": True, "skip_download": True, "noplaylist": True, "socket_timeout": TIMEOUT}
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            url = _original_auto_track_url(info or {}, language)
            if not url:
                return None
            raw = ydl.urlopen(url).read(MAX_SUBTITLE_BYTES + 1)
    except Exception as e:
        log.warning("YouTube caption fetch failed vid=%s lang=%s err=%s", video_id, language, e)
        return None
    if not raw or len(raw) > MAX_SUBTITLE_BYTES:
        return None
    text = raw.decode("utf-8", errors="replace")
    return pair_cues(remove_cue_overlap(text)).encode("utf-8")
