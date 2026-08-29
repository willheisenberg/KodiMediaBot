"""Central configuration for KodiMediaBot.

All environment variables are read once at import time and exposed via
the module-level ``CFG`` instance.  Every other module should import
``from kodibot.config import CFG`` instead of calling ``os.environ`` directly.
"""

import dataclasses
import os


def _bool_env(key: str, default: bool = False) -> bool:
    raw = (os.environ.get(key) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclasses.dataclass(frozen=True)
class Config:
    # ── Kodi connection ──────────────────────────────────────────────
    kodi_host: str
    kodi_port: int
    kodi_ws_port: int
    kodi_user: str
    kodi_pass: str

    # ── Telegram ─────────────────────────────────────────────────────
    tg_token: str
    startup_chat_id: int
    telegram_local_mode: bool
    telegram_base_url: str
    telegram_base_file_url: str
    telegram_read_timeout: float
    telegram_write_timeout: float
    telegram_connect_timeout: float
    telegram_pool_timeout: float
    telegram_download_size_limit: int  # bytes
    telegram_get_file_read_timeout: float
    telegram_get_file_write_timeout: float
    telegram_get_file_connect_timeout: float
    telegram_get_file_pool_timeout: float

    # ── Devices ──────────────────────────────────────────────────────
    cec_host: str
    denon_host: str | None
    denon_volume_step_commands: int

    # ── Debugging ────────────────────────────────────────────────────
    debug_ws: bool

    # ── SoundCloud ───────────────────────────────────────────────────
    sc_client_id: str
    sc_client_id_file: str

    # ── Media server ─────────────────────────────────────────────────
    upload_dir: str
    kodi_upload_dir: str
    media_server_host: str
    media_server_port: int
    media_server_scheme: str
    media_base_url: str

    # ── Radio / ICY ──────────────────────────────────────────────────
    radio_m3u_path: str
    radio_stream_map_raw: str
    icy_title_ttl: float
    icy_timeout: float

    # ── Search caches ────────────────────────────────────────────────
    yt_search_ttl: float
    yt_search_fail_ttl: float
    yt_search_timeout: float
    sc_search_ttl: float
    sc_search_fail_ttl: float
    sc_search_timeout: float

    # ── Persistence ──────────────────────────────────────────────────
    playlist_dir: str
    ui_state_file: str

    # ── Home Assistant ────────────────────────────────────────────────
    ha_host: str | None
    ha_port: int
    ha_token: str
    ha_light_id: str
    ha_colors_file: str
    ha_webapp_url: str
    ha_webapp_max_age: int

    # ── Projector (Beamer) ────────────────────────────────────────────
    projector_lirc_device: str
    projector_address: int
    projector_power_on_code: int
    projector_power_off_code: int
    projector_power_on_repeats: int

    # ── Display power (projector or TV) ───────────────────────────────
    display_button_label: str
    display_power_on_cmd: str
    display_power_off_cmd: str
    display_command_timeout: float
    tv_host: str | None

    # ── Optional panel sections ───────────────────────────────────────
    panel_show_volume: bool
    panel_show_hifi: bool
    panel_show_display: bool
    panel_show_airplay: bool
    panel_show_ha: bool

    # ── Internal tuning ──────────────────────────────────────────────
    kodi_error_log_interval: float

    # ── Radio Browser API ────────────────────────────────────────────
    radio_api_url: str

    # ── IPTV / TV configuration ───────────────────────────────────────
    iptv_m3u_url: str

    # ── Derived helpers ──────────────────────────────────────────────

    @property
    def kodi_url(self) -> str:
        return f"http://{self.kodi_host}:{self.kodi_port}/jsonrpc"

    @property
    def kodi_auth(self) -> tuple[str, str]:
        return (self.kodi_user, self.kodi_pass)

    @property
    def kodi_ws_url(self) -> str:
        return f"ws://{self.kodi_host}:{self.kodi_ws_port}/jsonrpc"

    @property
    def ha_base_url(self) -> str | None:
        if not self.ha_host:
            return None
        return f"http://{self.ha_host}:{self.ha_port}"

    def resolve_media_base_url(self) -> str:
        if self.media_base_url:
            return self.media_base_url
        public_host = (
            os.environ.get("MEDIA_SERVER_PUBLIC_HOST")
            or os.environ.get("HOST_IP")
            or os.environ.get("CEC_HOST")
            or self.kodi_host
            or "127.0.0.1"
        )
        return f"{self.media_server_scheme}://{public_host}:{self.media_server_port}"

    # ── Factory ──────────────────────────────────────────────────────

    @classmethod
    def from_env(cls) -> "Config":
        cec_host = (
            os.environ.get("CEC_HOST")
            or os.environ.get("HOST_IP")
            or ""
        )
        return cls(
            # Kodi
            kodi_host=os.environ["KODI_HOST"],
            kodi_port=int(os.environ["KODI_PORT"]),
            kodi_ws_port=int(os.environ["KODI_WS_PORT"]),
            kodi_user=os.environ["KODI_USER"],
            kodi_pass=os.environ["KODI_PASS"],
            # Telegram
            tg_token=os.environ["TG_TOKEN"],
            startup_chat_id=int(os.environ.get("STARTUP_CHAT_ID", "-1003641420817")),
            telegram_local_mode=_bool_env("TELEGRAM_LOCAL_MODE"),
            telegram_base_url=(os.environ.get("TELEGRAM_BASE_URL") or "").strip(),
            telegram_base_file_url=(os.environ.get("TELEGRAM_BASE_FILE_URL") or "").strip(),
            telegram_read_timeout=float(os.environ.get("TELEGRAM_READ_TIMEOUT", "300")),
            telegram_write_timeout=float(os.environ.get("TELEGRAM_WRITE_TIMEOUT", "30")),
            telegram_connect_timeout=float(os.environ.get("TELEGRAM_CONNECT_TIMEOUT", "30")),
            telegram_pool_timeout=float(os.environ.get("TELEGRAM_POOL_TIMEOUT", "30")),
            telegram_download_size_limit=int(os.environ.get("TELEGRAM_DOWNLOAD_SIZE_LIMIT_MB", "20")) * 1024 * 1024,
            telegram_get_file_read_timeout=float(os.environ.get("TELEGRAM_GET_FILE_READ_TIMEOUT", "300")),
            telegram_get_file_write_timeout=float(os.environ.get("TELEGRAM_GET_FILE_WRITE_TIMEOUT", "30")),
            telegram_get_file_connect_timeout=float(os.environ.get("TELEGRAM_GET_FILE_CONNECT_TIMEOUT", "30")),
            telegram_get_file_pool_timeout=float(os.environ.get("TELEGRAM_GET_FILE_POOL_TIMEOUT", "30")),
            # Devices
            cec_host=cec_host,
            denon_host=os.environ.get("DENON_HOST") or None,
            denon_volume_step_commands=int(os.environ.get("DENON_VOLUME_STEP_COMMANDS", "2")),
            # Debug
            debug_ws=_bool_env("DEBUG_WS"),
            # SoundCloud
            sc_client_id=(os.environ.get("SC_CLIENT_ID") or "").strip(),
            sc_client_id_file=os.environ.get(
                "SC_CLIENT_ID_FILE",
                "/storage/.kodi/userdata/addon_data/plugin.audio.soundcloud/cache/api-client-id",
            ),
            # Media server
            upload_dir=os.environ.get("UPLOAD_DIR", "/data/uploads"),
            kodi_upload_dir=os.environ.get("KODI_UPLOAD_DIR", "/storage/docker/partyqueue/uploads"),
            media_server_host=os.environ.get("MEDIA_SERVER_HOST", "0.0.0.0"),
            media_server_port=int(os.environ.get("MEDIA_SERVER_PORT", "8765")),
            media_server_scheme=os.environ.get("MEDIA_SERVER_SCHEME", "http"),
            media_base_url=(os.environ.get("MEDIA_BASE_URL") or "").rstrip("/"),
            # Radio
            radio_m3u_path=os.environ.get("RADIO_M3U_PATH", "/data/kodi.m3u"),
            radio_stream_map_raw=os.environ.get("RADIO_STREAM_MAP", ""),
            icy_title_ttl=float(os.environ.get("ICY_TITLE_TTL", "15")),
            icy_timeout=float(os.environ.get("ICY_TIMEOUT", "6")),
            # Search caches
            yt_search_ttl=float(os.environ.get("RADIO_YT_TTL", "21600")),
            yt_search_fail_ttl=float(os.environ.get("RADIO_YT_FAIL_TTL", "300")),
            yt_search_timeout=float(os.environ.get("RADIO_YT_TIMEOUT", "8")),
            sc_search_ttl=float(os.environ.get("RADIO_SC_TTL", "21600")),
            sc_search_fail_ttl=float(os.environ.get("RADIO_SC_FAIL_TTL", "300")),
            sc_search_timeout=float(os.environ.get("RADIO_SC_TIMEOUT", "8")),
            # Persistence
            playlist_dir=os.environ.get("PLAYLIST_DIR", "/data/playlists"),
            ui_state_file=os.environ.get("UI_STATE_FILE", "/data/playlists/telegram_ui_state.json"),
            # Home Assistant
            ha_host=os.environ.get("HA_HOST") or None,
            ha_port=int(os.environ.get("HA_PORT", "8123")),
            ha_token=(os.environ.get("HA_TOKEN") or "").strip(),
            ha_light_id=(os.environ.get("HA_LIGHT_ID") or "").strip(),
            ha_colors_file=os.environ.get("HA_COLORS_FILE", "/data/ha_colors.json"),
            ha_webapp_url=(os.environ.get("HA_WEBAPP_URL") or "").strip(),
            ha_webapp_max_age=int(os.environ.get("HA_WEBAPP_MAX_AGE", "900")),
            # Projector (Beamer)
            projector_lirc_device=os.environ.get("PROJECTOR_LIRC_DEVICE", "/dev/lirc0"),
            projector_address=int(os.environ.get("PROJECTOR_ADDRESS", "0x08"), 16),
            projector_power_on_code=int(os.environ.get("PROJECTOR_POWER_ON_CODE", "0x03"), 16),
            projector_power_off_code=int(os.environ.get("PROJECTOR_POWER_OFF_CODE", "0x00"), 16),
            projector_power_on_repeats=int(os.environ.get("PROJECTOR_POWER_ON_REPEATS", "4")),
            # Display power (projector or TV)
            display_button_label=(os.environ.get("DISPLAY_BUTTON_LABEL") or "📽 Beamer").strip(),
            display_power_on_cmd=(
                os.environ.get("DISPLAY_POWER_ON_CMD")
                or "python -m kodibot.core.projector on"
            ),
            display_power_off_cmd=(
                os.environ.get("DISPLAY_POWER_OFF_CMD")
                or "python -m kodibot.core.projector off"
            ),
            display_command_timeout=float(os.environ.get("DISPLAY_COMMAND_TIMEOUT", "15")),
            tv_host=os.environ.get("TV_HOST") or None,
            # Optional panel sections
            panel_show_volume=_bool_env("PANEL_SHOW_VOLUME", True),
            panel_show_hifi=_bool_env("PANEL_SHOW_HIFI", True),
            panel_show_display=_bool_env("PANEL_SHOW_DISPLAY", True),
            panel_show_airplay=_bool_env("PANEL_SHOW_AIRPLAY", True),
            panel_show_ha=_bool_env("PANEL_SHOW_HA", True),
            # Tuning
            kodi_error_log_interval=float(os.environ.get("KODI_ERROR_LOG_INTERVAL", "10")),
            # Radio Browser
            radio_api_url=os.environ.get("RADIO_API_URL", "https://de1.api.radio-browser.info/json").rstrip("/"),
            # IPTV
            iptv_m3u_url=os.environ.get(
                "IPTV_M3U_URL",
                "/data/kodi.m3u,https://raw.githubusercontent.com/jnk22/kodinerds-iptv/master/iptv/clean/clean_tv.m3u,https://iptv-org.github.io/iptv/countries/de.m3u",
            ),
        )


# Module-level singleton – created at import time.
CFG = Config.from_env()
