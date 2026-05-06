"""Home Assistant REST API integration for light control.

Provides synchronous helpers that are called from the Telegram UI via
``asyncio.to_thread()``.  All HTTP calls use ``requests`` (already a
project dependency).
"""

import json
import logging
import os

import requests

from kodibot.config import CFG

log = logging.getLogger(__name__)

_TIMEOUT = 8  # seconds for HA HTTP requests


# ── Availability ─────────────────────────────────────────────────────

def ha_available() -> bool:
    """Return True when Home Assistant integration is configured."""
    return bool(CFG.ha_host and CFG.ha_token and CFG.ha_light_id)


def resolve_ha_webapp_url() -> str:
    """Return the HTTPS URL for the HA color Mini App, or an empty string."""
    explicit = (CFG.ha_webapp_url or "").strip().rstrip("/")
    if explicit:
        return explicit if explicit.startswith("https://") else ""
    base_url = CFG.resolve_media_base_url().rstrip("/")
    if not base_url.startswith("https://"):
        return ""
    return f"{base_url}/app/ha-color"


def ha_webapp_available() -> bool:
    """Return True when the HA Mini App can be launched."""
    return ha_available() and bool(resolve_ha_webapp_url())


# ── Internal helpers ─────────────────────────────────────────────────

def _headers() -> dict:
    return {
        "Authorization": f"Bearer {CFG.ha_token}",
        "Content-Type": "application/json",
    }


def _url(path: str) -> str:
    return f"{CFG.ha_base_url}{path}"


# ── Brightness helpers ───────────────────────────────────────────────

def brightness_percent_from_ha(brightness) -> int | None:
    """Convert a Home Assistant brightness value (0-255) to percent."""
    if brightness is None:
        return None
    try:
        raw = max(0, min(255, int(brightness)))
    except (TypeError, ValueError):
        return None
    return int(round((raw / 255) * 100))


def brightness_percent_to_ha(percent) -> int | None:
    """Convert a percent brightness value (0-100) to HA's 0-255 scale."""
    if percent is None:
        return None
    try:
        pct = max(0, min(100, int(percent)))
    except (TypeError, ValueError):
        return None
    if pct <= 0:
        return 0
    return max(1, min(255, int(round((pct / 100) * 255))))


# ── Light state ──────────────────────────────────────────────────────

def get_light_state() -> dict | None:
    """Fetch the current state of the configured light entity.

    Returns a dict with keys ``state`` ("on"/"off"), ``rgb_color``,
    ``brightness``, ``friendly_name`` etc., or *None* on error.
    """
    if not ha_available():
        return None
    try:
        r = requests.get(
            _url(f"/api/states/{CFG.ha_light_id}"),
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        attrs = data.get("attributes", {})
        return {
            "state": data.get("state"),
            "rgb_color": attrs.get("rgb_color"),
            "brightness": attrs.get("brightness"),
            "friendly_name": attrs.get("friendly_name", CFG.ha_light_id),
        }
    except Exception as e:
        log.warning("HA get_light_state fail entity=%s err=%s", CFG.ha_light_id, e)
        return None


# ── Toggle ───────────────────────────────────────────────────────────

def toggle_light() -> tuple[bool, str]:
    """Toggle the light on/off.

    Returns ``(success, new_state)`` where *new_state* is "on" or "off".
    """
    if not ha_available():
        return False, "not configured"
    try:
        state = get_light_state()
        current = (state or {}).get("state", "off")
        service = "turn_off" if current == "on" else "turn_on"
        r = requests.post(
            _url(f"/api/services/light/{service}"),
            headers=_headers(),
            json={"entity_id": CFG.ha_light_id},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        new_state = "off" if service == "turn_off" else "on"
        log.info("HA toggle entity=%s %s -> %s", CFG.ha_light_id, current, new_state)
        return True, new_state
    except Exception as e:
        log.warning("HA toggle_light fail entity=%s err=%s", CFG.ha_light_id, e)
        return False, "error"


# ── Set color ────────────────────────────────────────────────────────

def set_light_color(r: int, g: int, b: int, brightness_pct: int | None = None) -> bool:
    """Set the light to the given RGB color, optionally with brightness."""
    if not ha_available():
        return False
    try:
        payload = {
            "entity_id": CFG.ha_light_id,
            "rgb_color": [r, g, b],
        }
        if brightness_pct is not None:
            brightness = brightness_percent_to_ha(brightness_pct)
            if brightness is None:
                return False
            if brightness <= 0:
                resp = requests.post(
                    _url("/api/services/light/turn_off"),
                    headers=_headers(),
                    json={"entity_id": CFG.ha_light_id},
                    timeout=_TIMEOUT,
                )
                resp.raise_for_status()
                log.info(
                    "HA set_color entity=%s rgb=(%d,%d,%d) brightness_pct=%d -> off",
                    CFG.ha_light_id,
                    r,
                    g,
                    b,
                    brightness_pct,
                )
                return True
            payload["brightness"] = brightness
        resp = requests.post(
            _url("/api/services/light/turn_on"),
            headers=_headers(),
            json=payload,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        log.info(
            "HA set_color entity=%s rgb=(%d,%d,%d) brightness_pct=%s",
            CFG.ha_light_id,
            r,
            g,
            b,
            brightness_pct if brightness_pct is not None else "-",
        )
        return True
    except Exception as e:
        log.warning(
            "HA set_color fail entity=%s rgb=(%d,%d,%d) brightness_pct=%s err=%s",
            CFG.ha_light_id,
            r,
            g,
            b,
            brightness_pct if brightness_pct is not None else "-",
            e,
        )
        return False


def set_light_brightness(percent: int) -> bool:
    """Set light brightness in percent. ``0`` turns the light off."""
    if not ha_available():
        return False
    brightness = brightness_percent_to_ha(percent)
    if brightness is None:
        return False
    try:
        if brightness <= 0:
            resp = requests.post(
                _url("/api/services/light/turn_off"),
                headers=_headers(),
                json={"entity_id": CFG.ha_light_id},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            log.info("HA set_brightness entity=%s percent=%d -> off", CFG.ha_light_id, percent)
            return True

        resp = requests.post(
            _url("/api/services/light/turn_on"),
            headers=_headers(),
            json={
                "entity_id": CFG.ha_light_id,
                "brightness": brightness,
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        log.info("HA set_brightness entity=%s percent=%d", CFG.ha_light_id, percent)
        return True
    except Exception as e:
        log.warning("HA set_brightness fail entity=%s percent=%d err=%s", CFG.ha_light_id, percent, e)
        return False


def set_light_effect(effect_name: str) -> bool:
    """Enable a named Home Assistant light effect."""
    if not ha_available():
        return False
    effect_name = (effect_name or "").strip()
    if not effect_name:
        return False
    try:
        resp = requests.post(
            _url("/api/services/light/turn_on"),
            headers=_headers(),
            json={
                "entity_id": CFG.ha_light_id,
                "effect": effect_name,
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        log.info("HA set_effect entity=%s effect=%s", CFG.ha_light_id, effect_name)
        return True
    except Exception as e:
        log.warning("HA set_effect fail entity=%s effect=%s err=%s", CFG.ha_light_id, effect_name, e)
        return False


# ── Saved colors persistence ────────────────────────────────────────

def _load_colors_file() -> list[dict]:
    try:
        with open(CFG.ha_colors_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("HA load colors fail file=%s err=%s", CFG.ha_colors_file, e)
    return []


def _save_colors_file(colors: list[dict]) -> bool:
    try:
        d = os.path.dirname(CFG.ha_colors_file)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = f"{CFG.ha_colors_file}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(colors, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CFG.ha_colors_file)
        return True
    except Exception as e:
        log.warning("HA save colors fail file=%s err=%s", CFG.ha_colors_file, e)
        return False


def load_saved_colors() -> list[dict]:
    """Return list of saved colors: ``[{"name": "...", "r": int, "g": int, "b": int}, ...]``."""
    return _load_colors_file()


def save_color(name: str, r: int, g: int, b: int) -> bool:
    """Save a named color to the JSON file."""
    colors = _load_colors_file()
    # Replace existing entry with the same name
    colors = [c for c in colors if c.get("name", "").lower() != name.lower()]
    colors.append({"name": name, "r": r, "g": g, "b": b})
    return _save_colors_file(colors)


def delete_saved_color(name: str) -> bool:
    """Delete a saved color by name."""
    colors = _load_colors_file()
    filtered = [c for c in colors if c.get("name", "").lower() != name.lower()]
    if len(filtered) == len(colors):
        return False  # not found
    return _save_colors_file(filtered)


# ── Hex parsing helper ───────────────────────────────────────────────

def parse_hex_color(text: str) -> tuple[int, int, int] | None:
    """Parse a hex color string like ``#FF5500`` or ``FF5500``.

    Returns ``(r, g, b)`` or *None* if the input is invalid.
    """
    text = text.strip().lstrip("#")
    if len(text) != 6:
        return None
    try:
        r = int(text[0:2], 16)
        g = int(text[2:4], 16)
        b = int(text[4:6], 16)
        return r, g, b
    except ValueError:
        return None
