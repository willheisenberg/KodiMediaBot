import os
import re
import time
import json


def ensure_playlist_dir(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        return True
    except Exception:
        return False


def sanitize_playlist_name(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
    safe = safe.strip("._-")
    return safe or "playlist"


def unique_playlist_path(dir_path: str, base_name: str) -> str:
    base = sanitize_playlist_name(base_name)
    path = os.path.join(dir_path, f"{base}.json")
    if not os.path.exists(path):
        return path
    for i in range(2, 1000):
        path = os.path.join(dir_path, f"{base}-{i}.json")
        if not os.path.exists(path):
            return path
    return os.path.join(dir_path, f"{base}-{int(time.time())}.json")


def playlist_path_for_name(dir_path: str, name: str) -> str:
    base = sanitize_playlist_name(name)
    return os.path.join(dir_path, f"{base}.json")


def save_playlist_to_disk(dir_path: str, name: str, items: list[dict]):
    if not ensure_playlist_dir(dir_path):
        return False, "Playlist directory is not available."
    if not items:
        return False, "Queue empty."
    path = unique_playlist_path(dir_path, name)
    data = {
        "name": name,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "queue": items,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return False, f"Save failed: {e}"
    return True, os.path.basename(path)


def save_playlist_to_disk_overwrite(dir_path: str, name: str, items: list[dict]):
    if not ensure_playlist_dir(dir_path):
        return False, "Playlist directory is not available."
    if not items:
        return False, "Queue empty."
    path = playlist_path_for_name(dir_path, name)
    data = {
        "name": name,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "queue": items,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return False, f"Save failed: {e}"
    return True, os.path.basename(path)


def list_playlist_files(dir_path: str):
    if not ensure_playlist_dir(dir_path):
        return []
    try:
        files = [f for f in os.listdir(dir_path) if f.lower().endswith(".json")]
    except Exception:
        return []
    return sorted(files, key=str.casefold)


def load_playlist_from_disk(dir_path: str, filename: str):
    path = os.path.join(dir_path, filename)
    if not os.path.exists(path):
        return False, "Playlist file not found."
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return False, f"Load failed: {e}"
    items = data.get("queue")
    if not isinstance(items, list):
        return False, "Invalid playlist format."
    return True, items


def delete_playlist_from_disk(dir_path: str, filename: str):
    path = os.path.join(dir_path, filename)
    if not os.path.exists(path):
        return False, "Playlist file not found."
    try:
        os.remove(path)
    except Exception as e:
        return False, f"Delete failed: {e}"
    return True, filename
