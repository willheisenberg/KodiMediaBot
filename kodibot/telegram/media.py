import json
import logging
import mimetypes
import os
import posixpath
import re
import shutil
import subprocess
import threading
import time
from asyncio import to_thread
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlparse

from yt_dlp import YoutubeDL

from kodibot.config import CFG
from kodibot.core import homeassistant as ha
from kodibot.telegram.ha_webapp_routes import (
    _request_origin,
    _ha_webapp_base_url,
    _validate_webapp_payload,
    _handle_ha_color_page,
    _handle_ha_color_state,
    _handle_ha_color_apply,
    _handle_ha_color_save,
)

log = logging.getLogger(__name__)
SOCIAL_VIDEO_DOMAINS = (
    "tiktok.com",
    "instagram.com",
    "facebook.com",
    "fb.watch",
    "x.com",
    "twitter.com",
)

_SERVER_LOCK = threading.Lock()
_SERVER_STARTED = False
_TEMP_MEDIA_LOCK = threading.Lock()
_TEMP_MEDIA = {}
_TEMP_MEDIA_ENTRIES = {}
_IMAGE_SESSION_LOCK = threading.Lock()
_IMAGE_SESSION = None


class MediaDownloadError(Exception):
    def __init__(self, user_message: str, detail: str | None = None):
        super().__init__(detail or user_message)
        self.user_message = user_message
        self.detail = detail or user_message


def ensure_upload_dir():
    os.makedirs(CFG.upload_dir, exist_ok=True)


def resolve_kodi_media_path(local_path: str):
    abs_local = os.path.abspath(local_path)
    abs_upload = os.path.abspath(CFG.upload_dir)
    try:
        rel = os.path.relpath(abs_local, abs_upload)
    except Exception:
        return local_path
    if rel.startswith(".."):
        return local_path
    return os.path.normpath(os.path.join(CFG.kodi_upload_dir, rel))


def resolve_media_base_url():
    return CFG.resolve_media_base_url()


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


def format_bytes(size: int | None):
    if size is None:
        return "unknown size"
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024


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


def register_temp_media(path: str, title: str):
    file_name = os.path.basename(path)
    media_url = build_media_url(file_name)
    register_temp_entry(
        keys=(normalize_media_url(media_url), path),
        title=title,
        kind="video",
        cleanup_paths=(path,),
    )
    return {
        "title": title,
        "url": media_url,
        "kind": "video",
        "link": media_url,
    }


def register_temp_entry(keys, title: str, kind: str, cleanup_paths=(), cleanup_dirs=()):
    entry = {
        "title": title,
        "kind": kind,
        "keys": set(),
        "cleanup_paths": tuple(cleanup_paths or ()),
        "cleanup_dirs": tuple(cleanup_dirs or ()),
    }
    entry_id = id(entry)
    with _TEMP_MEDIA_LOCK:
        _TEMP_MEDIA_ENTRIES[entry_id] = entry
        add_temp_entry_keys(entry_id, keys)
    return entry


def add_temp_entry_keys(entry_id, keys):
    entry = _TEMP_MEDIA_ENTRIES.get(entry_id)
    if not entry:
        return
    for key in keys:
        if not key:
            continue
        norm = normalize_media_key(key)
        _TEMP_MEDIA[norm] = entry_id
        entry["keys"].add(norm)


def normalize_media_key(key: str):
    if not key:
        return ""
    if "://" in key:
        return normalize_media_url(key)
    return os.path.normpath(key)


def _create_image_session_dir():
    ensure_upload_dir()
    ts = int(time.time() * 1000)
    local_dir = os.path.join(CFG.upload_dir, f"slideshow_{ts}")
    os.makedirs(local_dir, exist_ok=True)
    return {
        "local_dir": local_dir,
        "kodi_dir": resolve_kodi_media_path(local_dir),
        "count": 0,
        "image_paths": [],
        "title": "Photo slideshow",
    }


def _stage_image_into_session(session, item):
    src_path = item.get("path") or ""
    if not src_path or not os.path.exists(src_path):
        raise FileNotFoundError(f"Image source missing path={src_path}")
    next_index = session["count"] + 1
    base = os.path.basename(src_path)
    name, ext = os.path.splitext(base)
    dst_name = f"{next_index:03d}_{sanitize_stem(name)}{ext.lower()}"
    dst_path = os.path.join(session["local_dir"], dst_name)
    os.replace(src_path, dst_path)
    session["count"] = next_index
    session["image_paths"].append(dst_path)
    if item.get("title") and session["title"] == "Photo slideshow":
        session["title"] = item["title"]
    return dst_path


def start_image_session(item):
    cleanup_active_image_session()
    session = _create_image_session_dir()
    try:
        _stage_image_into_session(session, item)
    except Exception:
        shutil.rmtree(session["local_dir"], ignore_errors=True)
        raise
    entry = register_temp_entry(
        keys=(session["local_dir"], session["kodi_dir"], session["image_paths"][0], resolve_kodi_media_path(session["image_paths"][0])),
        title=session["title"],
        kind="image",
        cleanup_dirs=(session["local_dir"],),
    )
    session["entry_id"] = id(entry)
    with _IMAGE_SESSION_LOCK:
        global _IMAGE_SESSION
        _IMAGE_SESSION = session
    return get_image_session()


def add_image_to_session(item):
    with _IMAGE_SESSION_LOCK:
        session = _IMAGE_SESSION
    if session is None:
        return start_image_session(item)
    dst_path = _stage_image_into_session(session, item)
    kodi_path = resolve_kodi_media_path(dst_path)
    with _TEMP_MEDIA_LOCK:
        add_temp_entry_keys(session["entry_id"], (dst_path, kodi_path))
        entry = _TEMP_MEDIA_ENTRIES.get(session["entry_id"])
        if entry:
            entry["title"] = session["title"]
    return get_image_session()


def get_image_session():
    with _IMAGE_SESSION_LOCK:
        if _IMAGE_SESSION is None:
            return None
        return {
            "local_dir": _IMAGE_SESSION["local_dir"],
            "kodi_dir": _IMAGE_SESSION["kodi_dir"],
            "count": _IMAGE_SESSION["count"],
            "image_paths": list(_IMAGE_SESSION["image_paths"]),
            "title": _IMAGE_SESSION["title"],
            "entry_id": _IMAGE_SESSION["entry_id"],
        }


def cleanup_active_image_session():
    with _IMAGE_SESSION_LOCK:
        session = _IMAGE_SESSION
    if not session:
        return False
    return cleanup_temp_media(session["kodi_dir"])


def extract_first_url(text: str):
    if not text:
        return None
    m = re.search(r"https?://\S+", text)
    if not m:
        return None
    return m.group(0).rstrip("),.!?]}>\"'")


def is_supported_social_video_url(url: str):
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return False
    host = host.split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return any(host == domain or host.endswith(f".{domain}") for domain in SOCIAL_VIDEO_DOMAINS)


def maybe_faststart_mp4(path: str, kind: str):
    if kind != "video":
        return path
    if not path.lower().endswith(".mp4"):
        return path
    faststart_path = f"{path}.faststart.mp4"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        path,
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        faststart_path,
    ]
    try:
        res = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        log.debug("faststart skipped: ffmpeg not found")
        return path
    except Exception as e:
        log.debug("faststart skipped path=%s err=%s", path, e)
        return path

    if res.returncode != 0 or not os.path.exists(faststart_path):
        log.info(
            "FASTSTART failed path=%s rc=%s stderr=%s",
            path,
            res.returncode,
            (res.stderr or "").strip(),
        )
        try:
            if os.path.exists(faststart_path):
                os.remove(faststart_path)
        except Exception:
            pass
        return path

    try:
        os.replace(faststart_path, path)
        log.debug("faststart ok path=%s", path)
    except Exception as e:
        log.warning("faststart replace failed path=%s err=%s", path, e)
        try:
            os.remove(faststart_path)
        except Exception:
            pass
    return path


def classify_message(msg):
    caption = (msg.caption or "").strip()

    if msg.voice:
        dt = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        title = caption or f"Voice message {dt}"
        return {
            "file_id": msg.voice.file_id,
            "file_size": getattr(msg.voice, "file_size", None),
            "kind": "audio",
            "title": title,
            "storage_name": build_storage_name("voice", None, msg.voice.mime_type, ".ogg"),
        }

    if msg.audio:
        title = caption or msg.audio.title or msg.audio.file_name or "Audio upload"
        prefix = msg.audio.file_name or title
        return {
            "file_id": msg.audio.file_id,
            "file_size": getattr(msg.audio, "file_size", None),
            "kind": "audio",
            "title": title,
            "storage_name": build_storage_name(prefix, msg.audio.file_name, msg.audio.mime_type, ".mp3"),
        }

    if msg.video:
        title = caption or msg.video.file_name or "Video upload"
        prefix = msg.video.file_name or title
        return {
            "file_id": msg.video.file_id,
            "file_size": getattr(msg.video, "file_size", None),
            "kind": "video",
            "title": title,
            "storage_name": build_storage_name(prefix, msg.video.file_name, msg.video.mime_type, ".mp4"),
        }

    if msg.video_note:
        dt = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        title = caption or f"Video note {dt}"
        return {
            "file_id": msg.video_note.file_id,
            "file_size": getattr(msg.video_note, "file_size", None),
            "kind": "video",
            "title": title,
            "storage_name": build_storage_name("video_note", None, "video/mp4", ".mp4"),
        }

    if msg.photo:
        dt = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        title = caption or f"Photo {dt}"
        largest = msg.photo[-1]
        return {
            "file_id": largest.file_id,
            "file_size": getattr(largest, "file_size", None),
            "kind": "image",
            "title": title,
            "storage_name": build_storage_name("photo", None, "image/jpeg", ".jpg"),
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
        elif mime_type.startswith("image/"):
            kind = "image"
            fallback_ext = ".jpg"
            default_title = "Image file"
        else:
            return None
        title = caption or msg.document.file_name or default_title
        prefix = msg.document.file_name or title
        return {
            "file_id": msg.document.file_id,
            "file_size": getattr(msg.document, "file_size", None),
            "kind": kind,
            "title": title,
            "storage_name": build_storage_name(prefix, msg.document.file_name, mime_type, fallback_ext),
        }

    return None


async def download_media_item(bot, msg):
    media = classify_message(msg)
    if not media:
        return None
    file_size = media.get("file_size")
    if not CFG.telegram_local_mode and file_size and file_size > CFG.telegram_download_size_limit:
        raise MediaDownloadError(
            (
                f"⚠ Upload is {format_bytes(file_size)}. "
                "The standard Telegram Bot API can only download files up to 20 MB. "
                "For larger uploads, run a local telegram-bot-api server and set "
                "`CFG.telegram_local_mode=1` plus `TELEGRAM_BASE_URL`/`TELEGRAM_BASE_FILE_URL`."
            ),
            detail=(
                f"telegram download limit exceeded size={file_size} "
                f"limit={CFG.telegram_download_size_limit} local_mode={CFG.telegram_local_mode}"
            ),
        )
    ensure_upload_dir()
    target_path = os.path.join(CFG.upload_dir, media["storage_name"])
    try:
        tg_file = await bot.get_file(
            media["file_id"],
            read_timeout=CFG.telegram_get_file_read_timeout,
            write_timeout=CFG.telegram_get_file_write_timeout,
            connect_timeout=CFG.telegram_get_file_connect_timeout,
            pool_timeout=CFG.telegram_get_file_pool_timeout,
        )
        file_path = getattr(tg_file, "file_path", None)
        if CFG.telegram_local_mode and file_path and os.path.isabs(file_path):
            if not os.path.exists(file_path):
                raise MediaDownloadError(
                    "⚠ Upload could not be processed. The local Telegram Bot API file store is not mounted in the bot container.",
                    detail=f"local telegram file missing path={file_path}",
                )
            await to_thread(shutil.copyfile, file_path, target_path)
        else:
            await tg_file.download_to_drive(custom_path=target_path)
    except Exception as e:
        err_txt = str(e).lower()
        if "file is too big" in err_txt:
            raise MediaDownloadError(
                (
                    f"⚠ Upload is {format_bytes(file_size)}. "
                    "Telegram rejected the download because the bot is using the standard Bot API. "
                    "For larger uploads, run a local telegram-bot-api server and set "
                    "`CFG.telegram_local_mode=1` plus `TELEGRAM_BASE_URL`/`TELEGRAM_BASE_FILE_URL`."
                ),
                detail=f"telegram get_file failed size={file_size} err={e}",
            ) from e
        raise
    target_path = await to_thread(maybe_faststart_mp4, target_path, media["kind"])
    if media["kind"] == "image":
        return {
            "title": media["title"],
            "kind": "image",
            "path": target_path,
            "kodi_path": resolve_kodi_media_path(target_path),
        }
    item = register_temp_media(target_path, media["title"])
    item["kind"] = media["kind"]
    return item


def _download_social_video(url: str):
    ensure_upload_dir()
    temp_name = build_storage_name("social_video", None, "video/mp4", ".mp4")
    temp_path = os.path.join(CFG.upload_dir, temp_name)
    base_path, _ = os.path.splitext(temp_path)
    ydl_opts = {
        "quiet": True,
        "noplaylist": True,
        "format": "mp4/bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": f"{base_path}.%(ext)s",
        "restrictfilenames": True,
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        downloaded = ydl.prepare_filename(info)
    final_path = downloaded
    requested = info.get("requested_downloads") or []
    if requested:
        candidate = requested[0].get("filepath")
        if candidate:
            final_path = candidate
    if not os.path.exists(final_path):
        mp4_candidate = f"{base_path}.mp4"
        if os.path.exists(mp4_candidate):
            final_path = mp4_candidate
    if not os.path.exists(final_path):
        raise FileNotFoundError(f"Downloaded file not found for url={url}")
    title = info.get("title") or url
    final_path = maybe_faststart_mp4(final_path, "video")
    return register_temp_media(final_path, title)


async def download_social_video_item(text: str):
    url = extract_first_url(text)
    if not url or not is_supported_social_video_url(url):
        return None
    return await to_thread(_download_social_video, url)


def get_temp_media_title(url: str):
    with _TEMP_MEDIA_LOCK:
        entry_id = _TEMP_MEDIA.get(normalize_media_key(url))
        entry = _TEMP_MEDIA_ENTRIES.get(entry_id)
    if not entry:
        return None
    return entry.get("title")


def is_active_image_session_media(url: str):
    with _TEMP_MEDIA_LOCK:
        entry_id = _TEMP_MEDIA.get(normalize_media_key(url))
    if entry_id is None:
        return False
    with _IMAGE_SESSION_LOCK:
        return bool(_IMAGE_SESSION and _IMAGE_SESSION.get("entry_id") == entry_id)


def cleanup_temp_media(url: str):
    norm_url = normalize_media_key(url)
    with _TEMP_MEDIA_LOCK:
        entry_id = _TEMP_MEDIA.pop(norm_url, None)
        entry = _TEMP_MEDIA_ENTRIES.pop(entry_id, None) if entry_id is not None else None
        if entry_id is not None:
            stale_keys = [key for key, value in _TEMP_MEDIA.items() if value == entry_id]
            for key in stale_keys:
                _TEMP_MEDIA.pop(key, None)
    if entry_id is not None:
        with _IMAGE_SESSION_LOCK:
            global _IMAGE_SESSION
            if _IMAGE_SESSION and _IMAGE_SESSION.get("entry_id") == entry_id:
                _IMAGE_SESSION = None
    if entry_id is None or not entry:
        return False
    cleanup_paths = entry.get("cleanup_paths") or ()
    cleanup_dirs = entry.get("cleanup_dirs") or ()
    try:
        for path in cleanup_paths:
            if path and os.path.exists(path):
                os.remove(path)
        for path in cleanup_dirs:
            if path and os.path.exists(path):
                shutil.rmtree(path, ignore_errors=False)
        log.info(
            "TEMP MEDIA cleaned key=%s paths=%s dirs=%s",
            norm_url,
            list(cleanup_paths),
            list(cleanup_dirs),
        )
        return True
    except Exception as e:
        log.info(
            "TEMP MEDIA cleanup fail key=%s paths=%s dirs=%s err=%s",
            norm_url,
            list(cleanup_paths),
            list(cleanup_dirs),
            e,
        )
        return False


def cleanup_stale_temp_media():
    ensure_upload_dir()
    removed = 0
    failed = 0
    try:
        names = os.listdir(CFG.upload_dir)
    except Exception as e:
        log.warning("temp media startup scan fail dir=%s err=%s", CFG.upload_dir, e)
        return
    for name in names:
        path = os.path.join(CFG.upload_dir, name)
        try:
            if os.path.isfile(path):
                os.remove(path)
                removed += 1
            elif os.path.isdir(path):
                shutil.rmtree(path)
                removed += 1
        except Exception as e:
            failed += 1
            log.warning("temp media startup cleanup fail path=%s err=%s", path, e)
    with _TEMP_MEDIA_LOCK:
        _TEMP_MEDIA.clear()
        _TEMP_MEDIA_ENTRIES.clear()
    with _IMAGE_SESSION_LOCK:
        global _IMAGE_SESSION
        _IMAGE_SESSION = None
    log.info(
        "TEMP MEDIA startup cleanup dir=%s removed=%s failed=%s",
        CFG.upload_dir,
        removed,
        failed,
    )


class _MediaRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=CFG.upload_dir, **kwargs)

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, html: str):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self, max_bytes: int = 32768):
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return None
        if length <= 0 or length > max_bytes:
            return None
        try:
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    _request_origin = _request_origin
    _ha_webapp_base_url = _ha_webapp_base_url
    _validate_webapp_payload = _validate_webapp_payload
    _handle_ha_color_page = _handle_ha_color_page
    _handle_ha_color_state = _handle_ha_color_state
    _handle_ha_color_apply = _handle_ha_color_apply
    _handle_ha_color_save = _handle_ha_color_save

    def translate_path(self, path):
        parsed = urlparse(path)
        clean = posixpath.normpath(parsed.path)
        if clean.startswith("/media/"):
            rel_path = clean[len("/media/"):]
        else:
            rel_path = clean.lstrip("/")
        rel_path = rel_path.lstrip("/")
        base_dir = os.path.abspath(CFG.upload_dir)
        full_path = os.path.abspath(os.path.join(base_dir, rel_path))
        if os.path.commonpath([base_dir, full_path]) != base_dir:
            return base_dir
        return full_path

    def _parse_range_header(self, size: int):
        raw = self.headers.get("Range") or ""
        if not raw:
            return None
        if not raw.startswith("bytes=") or "," in raw:
            return "invalid"
        spec = raw[6:].strip()
        start_txt, sep, end_txt = spec.partition("-")
        if not sep:
            return "invalid"
        if start_txt == "":
            try:
                length = int(end_txt)
            except Exception:
                return "invalid"
            if length <= 0:
                return "invalid"
            start = max(size - length, 0)
            end = size - 1
            return start, end
        try:
            start = int(start_txt)
        except Exception:
            return "invalid"
        if start < 0 or start >= size:
            return "invalid"
        if end_txt == "":
            end = size - 1
        else:
            try:
                end = int(end_txt)
            except Exception:
                return "invalid"
            if end < start:
                return "invalid"
            end = min(end, size - 1)
        return start, end

    def _send_media_file(self, send_body: bool):
        if not self.path.startswith("/media/"):
            self.send_error(404)
            return
        path = self.translate_path(self.path)
        if not os.path.exists(path):
            self.send_error(404)
            return
        if not os.path.isfile(path):
            self.send_error(403)
            return

        size = os.path.getsize(path)
        range_info = self._parse_range_header(size)
        if range_info == "invalid":
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            return

        start = 0
        end = size - 1
        status = 200
        if range_info is not None:
            start, end = range_info
            status = 206

        content_type = self.guess_type(path)
        content_length = max(end - start + 1, 0)

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        if not send_body:
            return

        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk = f.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_HEAD(self):
        self._send_media_file(send_body=False)

    def do_GET(self):
        try:
            req_path = urlparse(self.path).path
            if req_path == "/health":
                from kodibot.core import kodi_api
                from kodibot.core import queue_state
                body = json.dumps({
                    "status": "ok",
                    "ws_connected": kodi_api.WS_CONNECTED,
                    "ws_state": kodi_api.WS_STATE,
                    "queue_length": len(queue_state.QUEUE),
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if req_path == "/app/ha-color":
                self._handle_ha_color_page()
                return
            self._send_media_file(send_body=True)
        except BrokenPipeError:
            return
        except Exception:
            log.exception("Media server GET failed path=%s", self.path)
            try:
                self._send_json(500, {"ok": False, "error": "Internal server error."})
            except Exception:
                return

    def do_POST(self):
        try:
            req_path = urlparse(self.path).path
            if req_path == "/app/ha-color/state":
                self._handle_ha_color_state()
                return
            if req_path == "/app/ha-color/apply":
                self._handle_ha_color_apply()
                return
            if req_path == "/app/ha-color/save":
                self._handle_ha_color_save()
                return
            self._send_json(404, {"ok": False, "error": "Not found."})
        except BrokenPipeError:
            return
        except Exception:
            log.exception("Media server POST failed path=%s", self.path)
            try:
                self._send_json(500, {"ok": False, "error": "Internal server error."})
            except Exception:
                return

    def log_message(self, format, *args):
        return


def start_media_server():
    global _SERVER_STARTED
    with _SERVER_LOCK:
        if _SERVER_STARTED:
            return
        ensure_upload_dir()
        cleanup_stale_temp_media()
        server = ThreadingHTTPServer((CFG.media_server_host, CFG.media_server_port), _MediaRequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _SERVER_STARTED = True
        log.info(
            "Media server listening host=%s port=%s base_url=%s dir=%s",
            CFG.media_server_host, CFG.media_server_port, resolve_media_base_url(), CFG.upload_dir,
        )
