import mimetypes
import os
import posixpath
import re
import threading
import time
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlparse


UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/data/uploads")
MEDIA_SERVER_HOST = os.environ.get("MEDIA_SERVER_HOST", "0.0.0.0")
MEDIA_SERVER_PORT = int(os.environ.get("MEDIA_SERVER_PORT", "8765"))
MEDIA_SERVER_SCHEME = os.environ.get("MEDIA_SERVER_SCHEME", "http")
MEDIA_BASE_URL = (os.environ.get("MEDIA_BASE_URL") or "").rstrip("/")

_SERVER_LOCK = threading.Lock()
_SERVER_STARTED = False
_TEMP_MEDIA_LOCK = threading.Lock()
_TEMP_MEDIA = {}


def ensure_upload_dir():
    os.makedirs(UPLOAD_DIR, exist_ok=True)


def resolve_media_base_url():
    if MEDIA_BASE_URL:
        return MEDIA_BASE_URL
    public_host = (
        os.environ.get("MEDIA_SERVER_PUBLIC_HOST")
        or os.environ.get("HOST_IP")
        or os.environ.get("CEC_HOST")
        or os.environ.get("KODI_HOST")
        or "127.0.0.1"
    )
    return f"{MEDIA_SERVER_SCHEME}://{public_host}:{MEDIA_SERVER_PORT}"


def build_media_url(filename: str):
    return f"{resolve_media_base_url()}/media/{quote(filename)}"


def normalize_media_url(url: str):
    parsed = urlparse(url or "")
    if not parsed.scheme or not parsed.netloc:
        return url or ""
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def sanitize_stem(name: str):
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip())
    safe = safe.strip("._-")
    return safe or "upload"


def choose_extension(file_name: str | None, mime_type: str | None, fallback: str):
    if file_name:
        _, ext = os.path.splitext(file_name)
        if ext:
            return ext.lower()
    guessed = mimetypes.guess_extension(mime_type or "")
    if guessed:
        return guessed.lower()
    return fallback


def build_storage_name(prefix: str, file_name: str | None, mime_type: str | None, fallback_ext: str):
    stem = sanitize_stem(prefix)
    ext = choose_extension(file_name, mime_type, fallback_ext)
    ts = int(time.time() * 1000)
    return f"{stem}_{ts}{ext}"


def classify_message(msg):
    caption = (msg.caption or "").strip()

    if msg.voice:
        dt = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        title = caption or f"Voice message {dt}"
        return {
            "file_id": msg.voice.file_id,
            "kind": "audio",
            "title": title,
            "storage_name": build_storage_name("voice", None, msg.voice.mime_type, ".ogg"),
        }

    if msg.audio:
        title = caption or msg.audio.title or msg.audio.file_name or "Audio upload"
        prefix = msg.audio.file_name or title
        return {
            "file_id": msg.audio.file_id,
            "kind": "audio",
            "title": title,
            "storage_name": build_storage_name(prefix, msg.audio.file_name, msg.audio.mime_type, ".mp3"),
        }

    if msg.video:
        title = caption or msg.video.file_name or "Video upload"
        prefix = msg.video.file_name or title
        return {
            "file_id": msg.video.file_id,
            "kind": "video",
            "title": title,
            "storage_name": build_storage_name(prefix, msg.video.file_name, msg.video.mime_type, ".mp4"),
        }

    if msg.video_note:
        dt = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        title = caption or f"Video note {dt}"
        return {
            "file_id": msg.video_note.file_id,
            "kind": "video",
            "title": title,
            "storage_name": build_storage_name("video_note", None, "video/mp4", ".mp4"),
        }

    if msg.document:
        mime_type = (msg.document.mime_type or "").lower()
        if mime_type.startswith("video/"):
            kind = "video"
            fallback_ext = ".mp4"
            default_title = "Video file"
        elif mime_type.startswith("audio/") or mime_type == "application/ogg":
            kind = "audio"
            fallback_ext = ".ogg"
            default_title = "Audio file"
        else:
            return None
        title = caption or msg.document.file_name or default_title
        prefix = msg.document.file_name or title
        return {
            "file_id": msg.document.file_id,
            "kind": kind,
            "title": title,
            "storage_name": build_storage_name(prefix, msg.document.file_name, mime_type, fallback_ext),
        }

    return None


async def download_media_item(bot, msg):
    media = classify_message(msg)
    if not media:
        return None
    ensure_upload_dir()
    target_path = os.path.join(UPLOAD_DIR, media["storage_name"])
    tg_file = await bot.get_file(media["file_id"])
    await tg_file.download_to_drive(custom_path=target_path)
    media_url = build_media_url(media["storage_name"])
    with _TEMP_MEDIA_LOCK:
        _TEMP_MEDIA[normalize_media_url(media_url)] = {
            "path": target_path,
            "title": media["title"],
        }
    return {
        "title": media["title"],
        "url": media_url,
        "kind": media["kind"],
        "link": media_url,
    }


def get_temp_media_title(url: str):
    with _TEMP_MEDIA_LOCK:
        entry = _TEMP_MEDIA.get(normalize_media_url(url))
    if not entry:
        return None
    return entry.get("title")


def cleanup_temp_media(url: str):
    norm_url = normalize_media_url(url)
    with _TEMP_MEDIA_LOCK:
        entry = _TEMP_MEDIA.pop(norm_url, None)
    if not entry:
        return False
    path = entry.get("path")
    try:
        if path and os.path.exists(path):
            os.remove(path)
        print(f"TEMP MEDIA cleaned url={norm_url} path={path}", flush=True)
        return True
    except Exception as e:
        print(f"TEMP MEDIA cleanup fail url={norm_url} path={path} err={e}", flush=True)
        return False


def cleanup_stale_temp_media():
    ensure_upload_dir()
    removed = 0
    failed = 0
    try:
        names = os.listdir(UPLOAD_DIR)
    except Exception as e:
        print(f"TEMP MEDIA startup scan fail dir={UPLOAD_DIR} err={e}", flush=True)
        return
    for name in names:
        path = os.path.join(UPLOAD_DIR, name)
        try:
            if os.path.isfile(path):
                os.remove(path)
                removed += 1
        except Exception as e:
            failed += 1
            print(f"TEMP MEDIA startup cleanup fail path={path} err={e}", flush=True)
    with _TEMP_MEDIA_LOCK:
        _TEMP_MEDIA.clear()
    print(
        f"TEMP MEDIA startup cleanup dir={UPLOAD_DIR} removed={removed} failed={failed}",
        flush=True,
    )


class _MediaRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=UPLOAD_DIR, **kwargs)

    def translate_path(self, path):
        parsed = urlparse(path)
        clean = posixpath.normpath(parsed.path)
        if clean.startswith("/media/"):
            rel_path = clean[len("/media/"):]
        else:
            rel_path = clean.lstrip("/")
        rel_path = rel_path.lstrip("/")
        base_dir = os.path.abspath(UPLOAD_DIR)
        full_path = os.path.abspath(os.path.join(base_dir, rel_path))
        if os.path.commonpath([base_dir, full_path]) != base_dir:
            return base_dir
        return full_path

    def do_GET(self):
        if not self.path.startswith("/media/"):
            self.send_error(404)
            return
        super().do_GET()

    def log_message(self, format, *args):
        return


def start_media_server():
    global _SERVER_STARTED
    with _SERVER_LOCK:
        if _SERVER_STARTED:
            return
        ensure_upload_dir()
        cleanup_stale_temp_media()
        server = ThreadingHTTPServer((MEDIA_SERVER_HOST, MEDIA_SERVER_PORT), _MediaRequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _SERVER_STARTED = True
        print(
            f"MEDIA SERVER listening host={MEDIA_SERVER_HOST} port={MEDIA_SERVER_PORT} "
            f"base_url={resolve_media_base_url()} dir={UPLOAD_DIR}",
            flush=True,
        )
