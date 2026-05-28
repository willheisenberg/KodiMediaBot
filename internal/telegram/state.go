// Package telegram implements the Telegram bot UI, handlers, and media server.
package telegram

import (
	"sync"
)

// ── Shared mutable state ────────────────────────────────────────────

var (
	StateMu sync.Mutex

	// Tracked message IDs
	LastBotID    int64 // last bot message
	PrevBotID    int64 // previous bot message
	LastSeenID   int64 // last user message
	ListMsgID    int64 // queue list message
	PanelMsgID   int64 // control panel message
	ProgressMsgID int64 // now-playing message
	HAMenuMsgID  int64 // HA menu message

	// Chat tracking
	ActiveChatID int64

	// HiFi status caches
	HiFiStatusCache    string
	AirplayStatusCache string
	DenonVolumeCache   string
	HiFiCacheTS        float64
	HiFiCacheTTL       = 10.0

	// Rate limiting
	RateMu     sync.Mutex
	RateEditMu sync.Mutex

	// HA menu state
	HAMenuTimeout     float64
	HAMenuTimeoutSec  = 120.0

	// UI state
	NowPlayingRefreshPending bool
	PanelRefreshPending      bool

	// Radio reconnect
	RadioReconnectActive bool
)

// ResetHiFiCache clears the HiFi status cache.
func ResetHiFiCache() {
	StateMu.Lock()
	HiFiStatusCache = ""
	AirplayStatusCache = ""
	DenonVolumeCache = ""
	HiFiCacheTS = 0
	StateMu.Unlock()
}
