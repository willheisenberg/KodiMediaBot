import logging
from urllib.parse import urlparse

from kodibot.config import CFG
from kodibot.core import homeassistant as ha
from kodibot.telegram.ha_webapp import (
    build_ha_color_webapp_html,
    parse_brightness_percent,
    parse_rgb_triplet,
    validate_webapp_init_data,
)

log = logging.getLogger(__name__)


def _request_origin(self) -> str:
    forwarded_proto = (self.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0].strip().lower()
    forwarded_host = (self.headers.get("X-Forwarded-Host") or "").split(",", 1)[0].strip()
    host = forwarded_host or (self.headers.get("Host") or "").split(",", 1)[0].strip()
    if host and forwarded_proto in {"http", "https"}:
        return f"{forwarded_proto}://{host}"
    if host:
        fallback_scheme = "https" if str(self.server.server_port) == "443" else "http"
        return f"{fallback_scheme}://{host}"
    resolved = ha.resolve_ha_webapp_url()
    parsed = urlparse(resolved or "")
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def _ha_webapp_base_url(self) -> str:
    origin = self._request_origin().rstrip("/")
    if origin:
        return f"{origin}/app/ha-color"
    return (ha.resolve_ha_webapp_url() or "").rstrip("/")


def _validate_webapp_payload(self):
    if not ha.ha_available():
        log.warning("HA webapp request rejected: Home Assistant not configured")
        self._send_json(503, {"ok": False, "error": "Home Assistant is not configured."})
        return None, None
    payload = self._read_json_body()
    if not isinstance(payload, dict):
        log.warning("HA webapp request rejected: invalid JSON payload path=%s", self.path)
        self._send_json(400, {"ok": False, "error": "Invalid JSON payload."})
        return None, None
    init_data = payload.get("init_data") or ""
    parsed = validate_webapp_init_data(
        init_data,
        CFG.tg_token,
        max_age_s=CFG.ha_webapp_max_age,
    )
    if parsed is None:
        log.warning("HA webapp request rejected: initData validation failed path=%s", self.path)
        self._send_json(403, {"ok": False, "error": "Telegram Mini App validation failed."})
        return None, None
    return payload, parsed


def _handle_ha_color_page(self):
    if not ha.ha_available():
        self._send_html(
            503,
            "<!doctype html><html><body><p>Home Assistant is not configured.</p></body></html>",
        )
        return
    app_base_url = self._ha_webapp_base_url()
    log.info("HA webapp page served base_url=%s path=%s", app_base_url or "-", self.path)
    self._send_html(200, build_ha_color_webapp_html(app_base_url))


def _handle_ha_color_state(self):
    payload, _init = self._validate_webapp_payload()
    if payload is None:
        return
    log.info("HA webapp state request accepted")
    light_state = ha.get_light_state()
    self._send_json(
        200,
        {
            "ok": True,
            "light_state": light_state or {},
            "saved_colors": ha.load_saved_colors(),
        },
    )


def _handle_ha_color_apply(self):
    payload, _init = self._validate_webapp_payload()
    if payload is None:
        return
    rgb = parse_rgb_triplet(payload)
    if rgb is None:
        log.warning("HA webapp apply rejected: invalid RGB payload=%s", payload)
        self._send_json(400, {"ok": False, "error": "Invalid RGB values."})
        return
    brightness_provided = payload.get("brightness_pct") not in (None, "")
    brightness_pct = parse_brightness_percent(payload)
    if brightness_provided and brightness_pct is None:
        log.warning("HA webapp apply rejected: invalid brightness payload=%s", payload)
        self._send_json(400, {"ok": False, "error": "Invalid brightness value."})
        return
    log.info("HA webapp apply rgb=%s brightness_pct=%s", rgb, brightness_pct if brightness_pct is not None else "-")
    ok = ha.set_light_color(*rgb, brightness_pct=brightness_pct)
    if not ok:
        log.warning("HA webapp apply failed rgb=%s brightness_pct=%s", rgb, brightness_pct if brightness_pct is not None else "-")
        self._send_json(502, {"ok": False, "error": "Color could not be applied."})
        return
    fallback_state = {
        "state": "off" if brightness_pct == 0 else "on",
        "rgb_color": list(rgb),
        "friendly_name": CFG.ha_light_id,
    }
    if brightness_pct is not None:
        fallback_state["brightness"] = ha.brightness_percent_to_ha(brightness_pct)
    self._send_json(
        200,
        {
            "ok": True,
            "light_state": ha.get_light_state() or fallback_state,
            "saved_colors": ha.load_saved_colors(),
        },
    )


def _handle_ha_color_save(self):
    payload, _init = self._validate_webapp_payload()
    if payload is None:
        return
    rgb = parse_rgb_triplet(payload)
    if rgb is None:
        log.warning("HA webapp save rejected: invalid RGB payload=%s", payload)
        self._send_json(400, {"ok": False, "error": "Invalid RGB values."})
        return
    name = str(payload.get("name") or "").strip()
    if not name:
        log.warning("HA webapp save rejected: missing preset name")
        self._send_json(400, {"ok": False, "error": "Preset name is required."})
        return
    log.info("HA webapp save name=%s rgb=%s", name, rgb)
    ok = ha.save_color(name, *rgb)
    if not ok:
        log.warning("HA webapp save failed name=%s rgb=%s", name, rgb)
        self._send_json(502, {"ok": False, "error": "Preset could not be saved."})
        return
    self._send_json(
        200,
        {
            "ok": True,
            "saved_colors": ha.load_saved_colors(),
        },
    )
