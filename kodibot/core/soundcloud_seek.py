"""Temporary workaround: make SoundCloud tracks seekable with a local copy.

SoundCloud now only serves HLS with fMP4 segments, and Kodi's demuxer ends
playback about 20 s after a seek in such a stream.  ffmpeg packs the segments
into a plain .m4a without re-encoding; Kodi seeks in that file normally.

Remove this module and its callers once Kodi handles these streams.
"""

import logging
import os
import subprocess
import time

log = logging.getLogger(__name__)

REMUX_TIMEOUT_SEC = 600

_copy_url = {"link": None, "url": None}


def remember_copy(link, url):
    """Note the media URL of the copy Kodi is about to play for ``link``."""
    _copy_url.update(link=link, url=url)


def copy_url(link):
    """Media URL of the current copy of ``link``, or None."""
    return _copy_url["url"] if link and _copy_url["link"] == link else None


def remux(stream_url, path, cancel_event):
    """Write the stream to ``path`` without re-encoding; True when complete."""
    part = f"{path}.part.m4a"
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", stream_url, "-vn", "-c", "copy", "-movflags", "+faststart", part,
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    except OSError as e:
        log.warning("SoundCloud remux could not start ffmpeg err=%s", e)
        return False
    deadline = time.monotonic() + REMUX_TIMEOUT_SEC
    try:
        while True:
            if cancel_event.is_set() or time.monotonic() >= deadline:
                proc.kill()
                proc.communicate()
                if not cancel_event.is_set():
                    log.warning("SoundCloud remux timed out after %ss", REMUX_TIMEOUT_SEC)
                return False
            try:
                _, err = proc.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                continue
        if proc.returncode != 0:
            log.warning("SoundCloud remux failed rc=%s err=%s", proc.returncode, (err or "")[-300:])
            return False
        os.replace(part, path)
        return True
    finally:
        if os.path.exists(part):
            os.remove(part)
