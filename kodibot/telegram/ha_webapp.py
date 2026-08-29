"""Helpers for Telegram Mini Apps used by the bot."""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from kodibot.telegram.i18n import LANG, t


def validate_webapp_init_data(
    init_data: str,
    bot_token: str,
    *,
    now: float | None = None,
    max_age_s: int = 900,
):
    """Validate Telegram Mini App initData and return parsed fields.

    Returns a dict on success or ``None`` on validation failure.
    """
    if not init_data or not bot_token:
        return None

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        return None

    try:
        auth_date = int(pairs.get("auth_date", "0") or "0")
    except ValueError:
        return None
    if auth_date <= 0:
        return None

    if now is None:
        now = time.time()
    if auth_date > (now + 30):
        return None
    if max_age_s > 0 and (now - auth_date) > max_age_s:
        return None

    out = dict(pairs)
    out["auth_date"] = auth_date
    raw_user = pairs.get("user") or ""
    if raw_user:
        try:
            out["user"] = json.loads(raw_user)
        except Exception:
            return None
    return out


def parse_rgb_triplet(payload):
    """Parse and clamp an RGB triplet from JSON-like payload."""
    try:
        r = max(0, min(255, int(payload.get("r"))))
        g = max(0, min(255, int(payload.get("g"))))
        b = max(0, min(255, int(payload.get("b"))))
    except (TypeError, ValueError, AttributeError):
        return None
    return r, g, b


def parse_brightness_percent(payload, key: str = "brightness_pct"):
    """Parse and clamp a brightness percentage from JSON-like payload."""
    try:
        return max(0, min(100, int(payload.get(key))))
    except (TypeError, ValueError, AttributeError):
        return None


def build_ha_color_webapp_html(app_base_url: str = "") -> str:
    """Return the HTML payload for the Home Assistant color Mini App."""
    app_base_url_json = json.dumps((app_base_url or "").rstrip("/"))
    i18n_json = json.dumps({
        "title": t("ha_webapp_title"),
        "subtitle": t("ha_webapp_subtitle"),
        "choose_color": t("choose_color"),
        "loading_light_state": t("loading_light_state"),
        "color_wheel": t("color_wheel"),
        "brightness": t("brightness").replace("🔆 ", ""),
        "saved_colors": t("saved_colors").replace("💾 ", ""),
        "preset_name": t("preset_name"),
        "save": t("save"),
        "apply": t("apply"),
        "close": t("close"),
        "network_error": t("network_error"),
        "request_failed": t("request_failed"),
        "light_fallback": t("light_fallback"),
        "unknown": t("unknown"),
        "state_on": t("state_on"),
        "state_off": t("state_off"),
        "state_unknown": t("state_unknown"),
        "no_saved_colors_yet": t("no_saved_colors_yet") + ".",
        "preset": t("preset_name").replace("-Name", "").replace(" name", ""),
        "preset_loaded": t("preset_loaded", name="{name}"),
        "telegram_mini_app_required": t("telegram_mini_app_required"),
        "ready": t("ready"),
        "light_state_load_failed": t("light_state_load_failed"),
        "telegram_init_missing": t("telegram_init_missing"),
        "applying_color": t("applying_color"),
        "color_apply_failed": t("color_apply_failed"),
        "color_applied": t("color_applied", hex="{hex}"),
        "enter_preset_name": t("enter_preset_name"),
        "preset_saved": t("preset_saved", name="{name}"),
        "preset_save_failed": t("preset_save_failed"),
    }, ensure_ascii=False)
    return """<!doctype html>
<html lang="__LANG__">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
  <title>__TITLE__</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root {
      color-scheme: light dark;
      --bg: var(--tg-theme-bg-color, #101418);
      --panel: var(--tg-theme-secondary-bg-color, #182029);
      --text: var(--tg-theme-text-color, #f5f7fa);
      --muted: var(--tg-theme-hint-color, #98a2ad);
      --line: color-mix(in srgb, var(--text) 12%, transparent);
      --button: var(--tg-theme-button-color, #2ea6ff);
      --button-text: var(--tg-theme-button-text-color, #ffffff);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top right, rgba(46, 166, 255, 0.14), transparent 26%),
        radial-gradient(circle at top left, rgba(255, 143, 92, 0.16), transparent 24%),
        var(--bg);
      color: var(--text);
    }
    .app {
      max-width: 560px;
      margin: 0 auto;
      padding: 18px 16px 28px;
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 16px;
      box-shadow: 0 18px 48px rgba(0, 0, 0, 0.22);
      backdrop-filter: blur(18px);
    }
    .title {
      font-size: 20px;
      font-weight: 700;
      margin: 0 0 6px;
    }
    .muted {
      color: var(--muted);
      margin: 0;
    }
    .preview-wrap {
      display: grid;
      grid-template-columns: 112px 1fr;
      gap: 14px;
      align-items: center;
      margin: 18px 0 12px;
    }
    .preview {
      width: 112px;
      height: 112px;
      border-radius: 24px;
      border: 1px solid rgba(255, 255, 255, 0.16);
      box-shadow:
        inset 0 1px 0 rgba(255, 255, 255, 0.18),
        0 18px 40px rgba(0, 0, 0, 0.3);
      background: #ffffff;
    }
    .hex-row {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 10px;
    }
    #hexValue {
      font: 700 20px/1.1 ui-monospace, SFMono-Regular, Menlo, monospace;
      letter-spacing: 0.04em;
    }
    input[type="color"] {
      inline-size: 54px;
      block-size: 38px;
      border: 0;
      padding: 0;
      background: transparent;
    }
    .slider {
      margin: 12px 0;
    }
    .slider-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 6px;
      font-weight: 600;
    }
    .slider-value {
      color: var(--muted);
      font: 600 13px/1 ui-monospace, SFMono-Regular, Menlo, monospace;
    }
    input[type="range"] {
      width: 100%;
      accent-color: var(--button);
    }
    .wheel-wrap {
      display: flex;
      justify-content: center;
      margin: 14px 0 8px;
      touch-action: none;
    }
    #colorWheel {
      border-radius: 50%;
      cursor: crosshair;
      touch-action: none;
    }
    .section-title {
      margin: 22px 0 10px;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: var(--muted);
    }
    .preset-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(92px, 1fr));
      gap: 10px;
    }
    .preset {
      border: 1px solid var(--line);
      border-radius: 14px;
      background: rgba(255, 255, 255, 0.04);
      color: var(--text);
      padding: 10px;
      text-align: left;
      cursor: pointer;
    }
    .preset-swatch {
      width: 100%;
      height: 34px;
      border-radius: 10px;
      margin-bottom: 8px;
      border: 1px solid rgba(255, 255, 255, 0.16);
    }
    .preset-name {
      display: block;
      font-size: 13px;
      font-weight: 600;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .preset-hex {
      display: block;
      margin-top: 3px;
      font-size: 11px;
      color: var(--muted);
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    }
    .save-row {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 10px;
      margin-top: 12px;
    }
    .save-row input {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px 13px;
      color: var(--text);
      background: rgba(255, 255, 255, 0.04);
    }
    .save-row button {
      border: 0;
      border-radius: 12px;
      padding: 0 14px;
      background: var(--button);
      color: var(--button-text);
      font-weight: 700;
      cursor: pointer;
    }
    .status {
      min-height: 22px;
      margin-top: 12px;
      color: var(--muted);
      font-size: 13px;
    }
    .status.error { color: #ff8e8e; }
    .status.ok { color: #8de1a5; }
    @media (max-width: 460px) {
      .preview-wrap {
        grid-template-columns: 1fr;
      }
      .preview {
        width: 100%;
        height: 118px;
      }
      .save-row {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <main class="app">
    <section class="card">
      <h1 class="title">__HA_LIGHT_TITLE__</h1>
      <p class="muted">__SUBTITLE__</p>

      <div class="preview-wrap">
        <div id="preview" class="preview"></div>
        <div>
          <div class="hex-row">
            <strong id="hexValue">#FFFFFF</strong>
            <input id="colorPicker" type="color" value="#ffffff" aria-label="__CHOOSE_COLOR__">
          </div>
          <p id="lightMeta" class="muted">__LOADING_LIGHT_STATE__</p>
        </div>
      </div>

      <div class="wheel-wrap">
        <canvas id="colorWheel" width="300" height="300" aria-label="__COLOR_WHEEL__"></canvas>
      </div>
      <div class="slider">
        <div class="slider-head"><span>__BRIGHTNESS__</span><span class="slider-value" id="valueBrightness">100%</span></div>
        <input id="sliderBrightness" type="range" min="0" max="100" value="100">
      </div>

      <div class="section-title">__SAVED_COLORS__</div>
      <div id="presetGrid" class="preset-grid"></div>

      <div class="save-row">
        <input id="presetName" type="text" maxlength="64" placeholder="__PRESET_NAME__">
        <button id="savePreset" type="button">__SAVE__</button>
      </div>

      <div id="status" class="status"></div>
    </section>
  </main>

  <script>
    const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
    const APP_BASE_URL = __APP_BASE_URL__;
    const I18N = __I18N__;
    const state = { r: 255, g: 255, b: 255, brightnessPct: 100, presets: [], hs: { h: 0, s: 0 } };

    const preview = document.getElementById("preview");
    const colorPicker = document.getElementById("colorPicker");
    const hexValue = document.getElementById("hexValue");
    const lightMeta = document.getElementById("lightMeta");
    const presetGrid = document.getElementById("presetGrid");
    const presetName = document.getElementById("presetName");
    const savePreset = document.getElementById("savePreset");
    const statusEl = document.getElementById("status");
    const sliderBrightness = document.getElementById("sliderBrightness");
    const valueBrightness = document.getElementById("valueBrightness");
    const wheelCanvas = document.getElementById("colorWheel");
    const wheelCtx = wheelCanvas.getContext("2d");


    function setStatus(text, kind) {
      statusEl.textContent = text || "";
      statusEl.className = "status" + (kind ? " " + kind : "");
    }

    function buildApiUrl(path) {
      const cleanPath = String(path || "").replace(/^\\/+/, "");
      if (APP_BASE_URL) {
        return APP_BASE_URL + "/" + cleanPath;
      }
      const origin = window.location && window.location.origin ? window.location.origin : "";
      return origin + "/app/ha-color/" + cleanPath;
    }

    function formatError(err, fallback) {
      const message = err && err.message ? String(err.message) : "";
      if (!message) {
        return fallback;
      }
      if (message === "Load failed" || message === "Failed to fetch" || message === "NetworkError when attempting to fetch resource.") {
        return I18N.network_error;
      }
      return message;
    }

    function toHex(v) {
      return Number(v).toString(16).padStart(2, "0").toUpperCase();
    }

    function currentHex() {
      return "#" + toHex(state.r) + toHex(state.g) + toHex(state.b);
    }

    function clampBrightnessPct(value) {
      return Math.max(0, Math.min(100, Number(value) || 0));
    }

    function brightnessPctFromHa(value, fallback) {
      if (value === null || value === undefined || value === "") {
        return clampBrightnessPct(fallback);
      }
      const raw = Math.max(0, Math.min(255, Number(value) || 0));
      return Math.max(0, Math.min(100, Math.round((raw / 255) * 100)));
    }

    /* ── HS color helpers (no black – this is for light bulbs) ── */
    function hslToRgb(h, s) {
      // HS wheel: hue = angle, saturation = distance from center.
      // Center = white (s=0), edge = full color (s=1). Lightness fixed at 50%
      // in HSL terms, which gives the brightest pure colors.
      const c = s, x = c * (1 - Math.abs((h * 6) % 2 - 1)), m = 1 - c;
      let r, g, b;
      const i = Math.floor(h * 6) % 6;
      switch (i) {
        case 0: r=c; g=x; b=0; break; case 1: r=x; g=c; b=0; break;
        case 2: r=0; g=c; b=x; break; case 3: r=0; g=x; b=c; break;
        case 4: r=x; g=0; b=c; break; default: r=c; g=0; b=x;
      }
      return [Math.round((r+m)*255), Math.round((g+m)*255), Math.round((b+m)*255)];
    }
    function rgbToHs(r, g, b) {
      r /= 255; g /= 255; b /= 255;
      const max = Math.max(r,g,b), min = Math.min(r,g,b), d = max - min;
      let h = 0;
      if (d) {
        if (max===r) h = ((g-b)/d + 6) % 6;
        else if (max===g) h = (b-r)/d + 2;
        else h = (r-g)/d + 4;
        h /= 6;
      }
      const s = max === 0 ? 0 : 1 - min / max;
      return { h, s };
    }

    /* ── Canvas: filled HS disc (like Hue / HA) ────────────────── */
    const W = wheelCanvas.width, CX = W / 2, CY = W / 2, RADIUS = W / 2 - 2;

    function drawWheel() {
      const { h, s } = state.hs;
      // Draw the disc pixel-by-pixel via ImageData for a smooth gradient
      const imgData = wheelCtx.createImageData(W, W);
      const data = imgData.data;
      for (let y = 0; y < W; y++) {
        for (let x = 0; x < W; x++) {
          const dx = x - CX, dy = y - CY, dist = Math.hypot(dx, dy);
          const idx = (y * W + x) * 4;
          if (dist > RADIUS + 1) {
            data[idx+3] = 0;
            continue;
          }
          const angle = ((Math.atan2(dy, dx) / (2*Math.PI)) + 1) % 1;
          const sat = Math.min(dist / RADIUS, 1);
          const [cr, cg, cb] = hslToRgb(angle, sat);
          data[idx]   = cr;
          data[idx+1] = cg;
          data[idx+2] = cb;
          data[idx+3] = dist <= RADIUS ? 255 : Math.round(Math.max(0, (RADIUS + 1 - dist)) * 255);
        }
      }
      wheelCtx.putImageData(imgData, 0, 0);

      // Pointer circle at the selected color position
      const pAngle = h * 2 * Math.PI;
      const pDist = s * RADIUS;
      const px = CX + pDist * Math.cos(pAngle);
      const py = CY + pDist * Math.sin(pAngle);
      wheelCtx.beginPath();
      wheelCtx.arc(px, py, 10, 0, 2*Math.PI);
      wheelCtx.strokeStyle = "#fff";
      wheelCtx.lineWidth = 3;
      wheelCtx.stroke();
      wheelCtx.beginPath();
      wheelCtx.arc(px, py, 10, 0, 2*Math.PI);
      wheelCtx.strokeStyle = "rgba(0,0,0,0.3)";
      wheelCtx.lineWidth = 1;
      wheelCtx.stroke();
    }

    // Cache the wheel image so dragging doesn't re-render all pixels
    let wheelImageCache = null;
    function drawWheelFast() {
      const { h, s } = state.hs;
      if (!wheelImageCache) {
        // Draw once without pointer
        const imgData = wheelCtx.createImageData(W, W);
        const data = imgData.data;
        for (let y = 0; y < W; y++) {
          for (let x = 0; x < W; x++) {
            const dx = x - CX, dy = y - CY, dist = Math.hypot(dx, dy);
            const idx = (y * W + x) * 4;
            if (dist > RADIUS + 1) { data[idx+3] = 0; continue; }
            const angle = ((Math.atan2(dy, dx) / (2*Math.PI)) + 1) % 1;
            const sat = Math.min(dist / RADIUS, 1);
            const [cr, cg, cb] = hslToRgb(angle, sat);
            data[idx] = cr; data[idx+1] = cg; data[idx+2] = cb;
            data[idx+3] = dist <= RADIUS ? 255 : Math.round(Math.max(0, (RADIUS+1-dist)) * 255);
          }
        }
        wheelImageCache = imgData;
      }
      wheelCtx.putImageData(wheelImageCache, 0, 0);
      // Draw pointer
      const pAngle = h * 2 * Math.PI;
      const pDist = s * RADIUS;
      const px = CX + pDist * Math.cos(pAngle), py = CY + pDist * Math.sin(pAngle);
      wheelCtx.beginPath();
      wheelCtx.arc(px, py, 10, 0, 2*Math.PI);
      wheelCtx.strokeStyle = "#fff"; wheelCtx.lineWidth = 3; wheelCtx.stroke();
      wheelCtx.beginPath();
      wheelCtx.arc(px, py, 10, 0, 2*Math.PI);
      wheelCtx.strokeStyle = "rgba(0,0,0,0.3)"; wheelCtx.lineWidth = 1; wheelCtx.stroke();
    }

    /* ── Wheel interaction ─────────────────────────────────────── */
    function wheelEventPos(e) {
      const rect = wheelCanvas.getBoundingClientRect();
      const scaleX = W / rect.width, scaleY = W / rect.height;
      const clientX = e.touches ? e.touches[0].clientX : e.clientX;
      const clientY = e.touches ? e.touches[0].clientY : e.clientY;
      return [(clientX - rect.left) * scaleX, (clientY - rect.top) * scaleY];
    }

    function handleWheelInput(e) {
      e.preventDefault();
      const [px, py] = wheelEventPos(e);
      const dx = px - CX, dy = py - CY;
      const dist = Math.min(Math.hypot(dx, dy), RADIUS);
      state.hs.h = ((Math.atan2(dy, dx) / (2 * Math.PI)) + 1) % 1;
      state.hs.s = dist / RADIUS;
      const [nr, ng, nb] = hslToRgb(state.hs.h, state.hs.s);
      setColor(nr, ng, nb);
    }

    wheelCanvas.addEventListener("mousedown", handleWheelInput);
    wheelCanvas.addEventListener("mousemove", (e) => { if (e.buttons) handleWheelInput(e); });
    wheelCanvas.addEventListener("touchstart", handleWheelInput, { passive: false });
    wheelCanvas.addEventListener("touchmove", handleWheelInput, { passive: false });

    function syncUi() {
      const hex = currentHex();
      preview.style.background = hex;
      preview.style.filter = "brightness(" + (0.25 + (state.brightnessPct / 100) * 0.75).toFixed(2) + ")";
      colorPicker.value = hex;
      hexValue.textContent = hex;
      sliderBrightness.value = String(state.brightnessPct);
      valueBrightness.textContent = String(state.brightnessPct) + "%";
      state.hs = rgbToHs(state.r, state.g, state.b);
      drawWheelFast();
      if (tg && tg.MainButton) {
        tg.MainButton.setText(I18N.apply + " " + hex + " \u00b7 " + state.brightnessPct + "%");
      }
    }

    function setLightValues(r, g, b, brightnessPct) {
      state.r = Math.max(0, Math.min(255, Number(r) || 0));
      state.g = Math.max(0, Math.min(255, Number(g) || 0));
      state.b = Math.max(0, Math.min(255, Number(b) || 0));
      state.brightnessPct = clampBrightnessPct(brightnessPct);
      syncUi();
    }

    function setColor(r, g, b) {
      setLightValues(r, g, b, state.brightnessPct);
    }

    function setBrightnessPct(value) {
      setLightValues(state.r, state.g, state.b, value);
    }

    function updateLightMeta(light, fallbackBrightnessPct) {
      const name = light.friendly_name || I18N.light_fallback;
      const rawLampState = String(light.state || "unknown").toLowerCase();
      const lampState = rawLampState === "on" ? I18N.state_on : (rawLampState === "off" ? I18N.state_off : I18N.state_unknown);
      const brightnessPct = brightnessPctFromHa(light.brightness, fallbackBrightnessPct);
      lightMeta.textContent = name + " - " + lampState + " - " + brightnessPct + "%";
      return brightnessPct;
    }

    function renderPresets(items) {
      state.presets = Array.isArray(items) ? items : [];
      if (!state.presets.length) {
        presetGrid.innerHTML = '<div class="muted">' + I18N.no_saved_colors_yet + '</div>';
        return;
      }
      presetGrid.innerHTML = "";
      for (const item of state.presets) {
        const r = Number(item.r) || 0;
        const g = Number(item.g) || 0;
        const b = Number(item.b) || 0;
        const hex = "#" + toHex(r) + toHex(g) + toHex(b);
        const button = document.createElement("button");
        button.type = "button";
        button.className = "preset";
        button.innerHTML =
          '<div class="preset-swatch" style="background:' + hex + '"></div>' +
          '<span class="preset-name"></span>' +
          '<span class="preset-hex">' + hex + "</span>";
        button.querySelector(".preset-name").textContent = item.name || I18N.preset;
        button.addEventListener("click", () => {
          setColor(r, g, b);
          setStatus(I18N.preset_loaded.replace("{name}", item.name || hex), "");
          if (tg && tg.HapticFeedback && tg.HapticFeedback.selectionChanged) {
            tg.HapticFeedback.selectionChanged();
          }
        });
        presetGrid.appendChild(button);
      }
    }

    async function postJson(path, payload) {
      const res = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      let data = {};
      try {
        data = await res.json();
      } catch (_) {
        data = {};
      }
      if (!res.ok || data.ok === false) {
        const message = data.error || I18N.request_failed;
        throw new Error(message);
      }
      return data;
    }

    async function loadState() {
      if (!tg || !tg.initData) {
        setStatus(I18N.telegram_mini_app_required, "error");
        return;
      }
      try {
        const data = await postJson(buildApiUrl("state"), { init_data: tg.initData });
        const light = data.light_state || {};
        const rgb = Array.isArray(light.rgb_color) && light.rgb_color.length === 3
          ? light.rgb_color
          : [255, 255, 255];
        const brightnessPct = updateLightMeta(light, state.brightnessPct);
        setLightValues(rgb[0], rgb[1], rgb[2], brightnessPct);
        renderPresets(data.saved_colors || []);
        setStatus(I18N.ready, "");
      } catch (err) {
        setStatus(formatError(err, I18N.light_state_load_failed), "error");
      }
    }

    async function applyColor() {
      if (!tg || !tg.initData) {
        setStatus(I18N.telegram_init_missing, "error");
        return;
      }
      try {
        setStatus(I18N.applying_color, "");
        if (tg.MainButton && tg.MainButton.showProgress) {
          tg.MainButton.showProgress();
        }
        const data = await postJson(buildApiUrl("apply"), {
          init_data: tg.initData,
          r: state.r,
          g: state.g,
          b: state.b,
          brightness_pct: state.brightnessPct,
        });
        const light = data.light_state || {};
        const rgb = Array.isArray(light.rgb_color) && light.rgb_color.length === 3
          ? light.rgb_color
          : [state.r, state.g, state.b];
        const brightnessPct = updateLightMeta(light, state.brightnessPct);
        setLightValues(rgb[0], rgb[1], rgb[2], brightnessPct);
        setStatus(I18N.color_applied.replace("{hex}", currentHex().replace("#", "")), "ok");
        if (tg && tg.HapticFeedback && tg.HapticFeedback.notificationOccurred) {
          tg.HapticFeedback.notificationOccurred("success");
        }
      } catch (err) {
        setStatus(formatError(err, I18N.color_apply_failed), "error");
        if (tg && tg.HapticFeedback && tg.HapticFeedback.notificationOccurred) {
          tg.HapticFeedback.notificationOccurred("error");
        }
      } finally {
        if (tg.MainButton && tg.MainButton.hideProgress) {
          tg.MainButton.hideProgress();
        }
      }
    }

    async function saveCurrentPreset() {
      const name = (presetName.value || "").trim();
      if (!name) {
        setStatus(I18N.enter_preset_name, "error");
        return;
      }
      if (!tg || !tg.initData) {
        setStatus(I18N.telegram_init_missing, "error");
        return;
      }
      try {
        const data = await postJson(buildApiUrl("save"), {
          init_data: tg.initData,
          name,
          r: state.r,
          g: state.g,
          b: state.b,
        });
        renderPresets(data.saved_colors || []);
        presetName.value = "";
        setStatus(I18N.preset_saved.replace("{name}", name), "ok");
        if (tg && tg.HapticFeedback && tg.HapticFeedback.notificationOccurred) {
          tg.HapticFeedback.notificationOccurred("success");
        }
      } catch (err) {
        setStatus(formatError(err, I18N.preset_save_failed), "error");
      }
    }

    colorPicker.addEventListener("input", (event) => {
      const hex = String(event.target.value || "#FFFFFF").replace("#", "");
      setColor(parseInt(hex.slice(0, 2), 16), parseInt(hex.slice(2, 4), 16), parseInt(hex.slice(4, 6), 16));
    });
    sliderBrightness.addEventListener("input", () => setBrightnessPct(sliderBrightness.value));
    savePreset.addEventListener("click", saveCurrentPreset);

    if (tg) {
      tg.ready();
      if (tg.expand) {
        tg.expand();
      }
      if (tg.enableClosingConfirmation) {
        tg.enableClosingConfirmation();
      }
      if (tg.MainButton) {
        tg.MainButton.setText(I18N.apply);
        tg.MainButton.show();
        tg.MainButton.onClick(applyColor);
      }
      if (tg.SecondaryButton) {
        tg.SecondaryButton.setParams({ text: I18N.close, position: "left", is_visible: true });
        tg.SecondaryButton.onClick(() => tg.close && tg.close());
        tg.SecondaryButton.show();
      }
    }

    syncUi();
    loadState();
  </script>
</body>
</html>
""".replace("__LANG__", LANG).replace("__TITLE__", t("ha_webapp_title")).replace(
        "__HA_LIGHT_TITLE__", t("ha_light_title").replace("🏠 ", "")
    ).replace("__SUBTITLE__", t("ha_webapp_subtitle")).replace(
        "__CHOOSE_COLOR__", t("choose_color")
    ).replace("__LOADING_LIGHT_STATE__", t("loading_light_state")).replace(
        "__COLOR_WHEEL__", t("color_wheel")
    ).replace("__BRIGHTNESS__", t("brightness").replace("🔆 ", "")).replace(
        "__SAVED_COLORS__", t("saved_colors").replace("💾 ", "")
    ).replace("__PRESET_NAME__", t("preset_name")).replace("__SAVE__", t("save")).replace(
        "__APP_BASE_URL__", app_base_url_json
    ).replace("__I18N__", i18n_json)
