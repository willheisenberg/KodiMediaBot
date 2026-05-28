// Package config provides central configuration for KodiMediaBot.
//
// All environment variables are read once at init time and exposed via
// the package-level Get() function. Every other package should use
// config.Get() instead of calling os.Getenv directly.
package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"sync"
)

// Config holds all configuration values.
type Config struct {
	// Kodi connection
	KodiHost   string
	KodiPort   int
	KodiWSPort int
	KodiUser   string
	KodiPass   string

	// Telegram
	TGToken                       string
	StartupChatID                 int64
	TelegramLocalMode             bool
	TelegramBaseURL               string
	TelegramBaseFileURL           string
	TelegramReadTimeout           float64
	TelegramWriteTimeout          float64
	TelegramConnectTimeout        float64
	TelegramPoolTimeout           float64
	TelegramDownloadSizeLimit     int64 // bytes
	TelegramGetFileReadTimeout    float64
	TelegramGetFileWriteTimeout   float64
	TelegramGetFileConnectTimeout float64
	TelegramGetFilePoolTimeout    float64

	// Devices
	CECHost                string
	DenonHost              string // empty string means not configured
	DenonVolumeStepCommands int

	// Debugging
	DebugWS bool

	// SoundCloud
	SCClientID     string
	SCClientIDFile string

	// Media server
	UploadDir         string
	KodiUploadDir     string
	MediaServerHost   string
	MediaServerPort   int
	MediaServerScheme string
	MediaBaseURL      string

	// Radio / ICY
	RadioM3UPath      string
	RadioStreamMapRaw string
	ICYTitleTTL       float64
	ICYTimeout        float64

	// Search caches
	YTSearchTTL      float64
	YTSearchFailTTL  float64
	YTSearchTimeout  float64
	SCSearchTTL      float64
	SCSearchFailTTL  float64
	SCSearchTimeout  float64

	// Persistence
	PlaylistDir string
	UIStateFile string

	// Home Assistant
	HAHost        string // empty string means not configured
	HAPort        int
	HAToken       string
	HALightID     string
	HAColorsFile  string
	HAWebappURL   string
	HAWebappMaxAge int

	// Projector (Beamer)
	PiGPIOHost              string
	PiGPIOPort              int
	ProjectorGPIO           int
	ProjectorProtocol       string
	ProjectorAddress        int
	ProjectorPowerOnCode    int
	ProjectorPowerOffCode   int
	ProjectorPowerOnRepeats int

	// Internal tuning
	KodiErrorLogInterval float64

	// Radio Browser API
	RadioAPIURL string

	// IPTV / TV
	IPTVM3UURL string
}

// KodiURL returns the Kodi JSON-RPC endpoint URL.
func (c *Config) KodiURL() string {
	return fmt.Sprintf("http://%s:%d/jsonrpc", c.KodiHost, c.KodiPort)
}

// KodiAuth returns (user, pass) for Kodi HTTP auth.
func (c *Config) KodiAuth() (string, string) {
	return c.KodiUser, c.KodiPass
}

// KodiWSURL returns the Kodi WebSocket URL.
func (c *Config) KodiWSURL() string {
	return fmt.Sprintf("ws://%s:%d/jsonrpc", c.KodiHost, c.KodiWSPort)
}

// HABaseURL returns the Home Assistant base URL or empty string.
func (c *Config) HABaseURL() string {
	if c.HAHost == "" {
		return ""
	}
	return fmt.Sprintf("http://%s:%d", c.HAHost, c.HAPort)
}

// ResolveMediaBaseURL returns the media base URL, with fallback logic.
func (c *Config) ResolveMediaBaseURL() string {
	if c.MediaBaseURL != "" {
		return c.MediaBaseURL
	}
	publicHost := envOrDefault("MEDIA_SERVER_PUBLIC_HOST", "")
	if publicHost == "" {
		publicHost = envOrDefault("HOST_IP", "")
	}
	if publicHost == "" {
		publicHost = envOrDefault("CEC_HOST", "")
	}
	if publicHost == "" {
		publicHost = c.KodiHost
	}
	if publicHost == "" {
		publicHost = "127.0.0.1"
	}
	return fmt.Sprintf("%s://%s:%d", c.MediaServerScheme, publicHost, c.MediaServerPort)
}

var (
	cfg     *Config
	cfgOnce sync.Once
)

// Get returns the singleton Config instance.
func Get() *Config {
	cfgOnce.Do(func() {
		cfg = fromEnv()
	})
	return cfg
}

func fromEnv() *Config {
	cecHost := envOrDefault("CEC_HOST", "")
	if cecHost == "" {
		cecHost = envOrDefault("HOST_IP", "")
	}

	return &Config{
		// Kodi
		KodiHost:   mustEnv("KODI_HOST"),
		KodiPort:   mustEnvInt("KODI_PORT"),
		KodiWSPort: mustEnvInt("KODI_WS_PORT"),
		KodiUser:   mustEnv("KODI_USER"),
		KodiPass:   mustEnv("KODI_PASS"),

		// Telegram
		TGToken:                       mustEnv("TG_TOKEN"),
		StartupChatID:                 envInt64("STARTUP_CHAT_ID", -1003641420817),
		TelegramLocalMode:             boolEnv("TELEGRAM_LOCAL_MODE"),
		TelegramBaseURL:               strings.TrimSpace(envOrDefault("TELEGRAM_BASE_URL", "")),
		TelegramBaseFileURL:           strings.TrimSpace(envOrDefault("TELEGRAM_BASE_FILE_URL", "")),
		TelegramReadTimeout:           envFloat("TELEGRAM_READ_TIMEOUT", 300),
		TelegramWriteTimeout:          envFloat("TELEGRAM_WRITE_TIMEOUT", 30),
		TelegramConnectTimeout:        envFloat("TELEGRAM_CONNECT_TIMEOUT", 30),
		TelegramPoolTimeout:           envFloat("TELEGRAM_POOL_TIMEOUT", 30),
		TelegramDownloadSizeLimit:     int64(envInt("TELEGRAM_DOWNLOAD_SIZE_LIMIT_MB", 20)) * 1024 * 1024,
		TelegramGetFileReadTimeout:    envFloat("TELEGRAM_GET_FILE_READ_TIMEOUT", 300),
		TelegramGetFileWriteTimeout:   envFloat("TELEGRAM_GET_FILE_WRITE_TIMEOUT", 30),
		TelegramGetFileConnectTimeout: envFloat("TELEGRAM_GET_FILE_CONNECT_TIMEOUT", 30),
		TelegramGetFilePoolTimeout:    envFloat("TELEGRAM_GET_FILE_POOL_TIMEOUT", 30),

		// Devices
		CECHost:                cecHost,
		DenonHost:              envOrDefault("DENON_HOST", ""),
		DenonVolumeStepCommands: envInt("DENON_VOLUME_STEP_COMMANDS", 2),

		// Debug
		DebugWS: boolEnv("DEBUG_WS"),

		// SoundCloud
		SCClientID:     strings.TrimSpace(envOrDefault("SC_CLIENT_ID", "")),
		SCClientIDFile: envOrDefault("SC_CLIENT_ID_FILE", "/storage/.kodi/userdata/addon_data/plugin.audio.soundcloud/cache/api-client-id"),

		// Media server
		UploadDir:         envOrDefault("UPLOAD_DIR", "/data/uploads"),
		KodiUploadDir:     envOrDefault("KODI_UPLOAD_DIR", "/storage/docker/partyqueue/uploads"),
		MediaServerHost:   envOrDefault("MEDIA_SERVER_HOST", "0.0.0.0"),
		MediaServerPort:   envInt("MEDIA_SERVER_PORT", 8765),
		MediaServerScheme: envOrDefault("MEDIA_SERVER_SCHEME", "http"),
		MediaBaseURL:      strings.TrimRight(envOrDefault("MEDIA_BASE_URL", ""), "/"),

		// Radio
		RadioM3UPath:      envOrDefault("RADIO_M3U_PATH", "/data/kodi.m3u"),
		RadioStreamMapRaw: envOrDefault("RADIO_STREAM_MAP", ""),
		ICYTitleTTL:       envFloat("ICY_TITLE_TTL", 15),
		ICYTimeout:        envFloat("ICY_TIMEOUT", 6),

		// Search caches
		YTSearchTTL:     envFloat("RADIO_YT_TTL", 21600),
		YTSearchFailTTL: envFloat("RADIO_YT_FAIL_TTL", 300),
		YTSearchTimeout: envFloat("RADIO_YT_TIMEOUT", 8),
		SCSearchTTL:     envFloat("RADIO_SC_TTL", 21600),
		SCSearchFailTTL: envFloat("RADIO_SC_FAIL_TTL", 300),
		SCSearchTimeout: envFloat("RADIO_SC_TIMEOUT", 8),

		// Persistence
		PlaylistDir: envOrDefault("PLAYLIST_DIR", "/data/playlists"),
		UIStateFile: envOrDefault("UI_STATE_FILE", "/data/telegram_ui_state.json"),

		// Home Assistant
		HAHost:        envOrDefault("HA_HOST", ""),
		HAPort:        envInt("HA_PORT", 8123),
		HAToken:       strings.TrimSpace(envOrDefault("HA_TOKEN", "")),
		HALightID:     strings.TrimSpace(envOrDefault("HA_LIGHT_ID", "")),
		HAColorsFile:  envOrDefault("HA_COLORS_FILE", "/data/ha_colors.json"),
		HAWebappURL:   strings.TrimSpace(envOrDefault("HA_WEBAPP_URL", "")),
		HAWebappMaxAge: envInt("HA_WEBAPP_MAX_AGE", 900),

		// Projector (Beamer)
		PiGPIOHost:              envOrDefault("PIGPIO_HOST", "127.0.0.1"),
		PiGPIOPort:              envInt("PIGPIO_PORT", 8888),
		ProjectorGPIO:           envInt("PROJECTOR_GPIO", 17),
		ProjectorProtocol:       envOrDefault("PROJECTOR_PROTOCOL", "NEC"),
		ProjectorAddress:        envHex("PROJECTOR_ADDRESS", 0x08),
		ProjectorPowerOnCode:    envHex("PROJECTOR_POWER_ON_CODE", 0x03),
		ProjectorPowerOffCode:   envHex("PROJECTOR_POWER_OFF_CODE", 0x00),
		ProjectorPowerOnRepeats: envInt("PROJECTOR_POWER_ON_REPEATS", 4),

		// Tuning
		KodiErrorLogInterval: envFloat("KODI_ERROR_LOG_INTERVAL", 10),

		// Radio Browser
		RadioAPIURL: strings.TrimRight(envOrDefault("RADIO_API_URL", "https://de1.api.radio-browser.info/json"), "/"),

		// IPTV
		IPTVM3UURL: envOrDefault("IPTV_M3U_URL", "https://raw.githubusercontent.com/jnk22/kodinerds-iptv/master/iptv/clean/kodi_tv.m3u,https://iptv-org.github.io/iptv/countries/de.m3u"),
	}
}

// ── Env helpers ─────────────────────────────────────────────────────

func mustEnv(key string) string {
	val := os.Getenv(key)
	if val == "" {
		panic(fmt.Sprintf("required environment variable %s is not set", key))
	}
	return val
}

func mustEnvInt(key string) int {
	v, err := strconv.Atoi(mustEnv(key))
	if err != nil {
		panic(fmt.Sprintf("environment variable %s is not a valid integer: %v", key, err))
	}
	return v
}

func envOrDefault(key, def string) string {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	return v
}

func envInt(key string, def int) int {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return def
	}
	return n
}

func envInt64(key string, def int64) int64 {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	n, err := strconv.ParseInt(v, 10, 64)
	if err != nil {
		return def
	}
	return n
}

func envFloat(key string, def float64) float64 {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	f, err := strconv.ParseFloat(v, 64)
	if err != nil {
		return def
	}
	return f
}

func envHex(key string, def int) int {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	v = strings.TrimSpace(v)
	// Support 0x prefix
	base := 16
	if strings.HasPrefix(v, "0x") || strings.HasPrefix(v, "0X") {
		v = v[2:]
	} else {
		// Try decimal
		n, err := strconv.Atoi(v)
		if err == nil {
			return n
		}
	}
	n, err := strconv.ParseInt(v, base, 64)
	if err != nil {
		return def
	}
	return int(n)
}

func boolEnv(key string) bool {
	raw := strings.TrimSpace(strings.ToLower(os.Getenv(key)))
	if raw == "" {
		return false
	}
	return raw == "1" || raw == "true" || raw == "yes" || raw == "on"
}
