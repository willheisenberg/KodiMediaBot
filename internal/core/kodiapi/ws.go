package kodiapi

import (
	"encoding/json"
	"log"
	"math"
	"net/url"
	"strings"
	"time"

	"github.com/gorilla/websocket"
	"github.com/willheisenberg/KodiMediaBot/internal/config"
)

// KodiWSListener connects to Kodi's WebSocket and dispatches events.
// It runs forever with exponential backoff on disconnect.
func KodiWSListener() {
	cfg := config.Get()
	wsURL := cfg.KodiWSURL()
	backoff := 3.0

	for {
		func() {
			dialer := websocket.Dialer{
				HandshakeTimeout: 10 * time.Second,
			}
			conn, _, err := dialer.Dial(wsURL, nil)
			if err != nil {
				mu.Lock()
				WSConnected = false
				WSState = "unknown"
				mu.Unlock()
				log.Printf("WARN WS connect failed: %v. Reconnecting in %.0fs", err, backoff)
				time.Sleep(time.Duration(backoff) * time.Second)
				backoff = math.Min(backoff*2, 60)
				return
			}
			defer conn.Close()

			mu.Lock()
			WSConnected = true
			mu.Unlock()
			backoff = 3

			// Set ping/pong handlers
			conn.SetPongHandler(func(appData string) error {
				return nil
			})

			// Start ping ticker
			pingTicker := time.NewTicker(20 * time.Second)
			defer pingTicker.Stop()

			go func() {
				for range pingTicker.C {
					if err := conn.WriteMessage(websocket.PingMessage, nil); err != nil {
						return
					}
				}
			}()

			for {
				_, raw, err := conn.ReadMessage()
				if err != nil {
					mu.Lock()
					WSConnected = false
					WSState = "unknown"
					mu.Unlock()
					log.Printf("WARN WS disconnected: %v. Reconnecting in %.0fs", err, backoff)
					time.Sleep(time.Duration(backoff) * time.Second)
					backoff = math.Min(backoff*2, 60)
					return
				}

				var msg map[string]interface{}
				if err := json.Unmarshal(raw, &msg); err != nil {
					continue
				}

				method := jsonStr(msg, "method")
				if method != "" && cfg.DebugWS {
					log.Printf("DEBUG WS event: method=%s", method)
				}

				switch method {
				case "Other.playback_init":
					handlePlaybackInit(msg)
				case "Player.OnPlay", "Player.OnAVStart":
					handlePlayerOnPlay(msg)
				case "Player.OnPause":
					handlePlayerOnPause()
				case "Player.OnResume":
					handlePlayerOnResume()
				case "Player.OnStop":
					handlePlayerOnStop(msg)
				}
			}
		}()
	}
}

func handlePlaybackInit(msg map[string]interface{}) {
	params := jsonMap(msg, "params")
	data := jsonMap(params, "data")
	if data == nil {
		return
	}
	vid := jsonStr(data, "video_id")
	playingFile := jsonStr(data, "playing_file")
	mu.Lock()
	if vid != "" {
		LastWSYTID = vid
	}
	if playingFile != "" {
		LastWSPlayingFile = playingFile
	}
	mu.Unlock()
}

func handlePlayerOnPlay(msg map[string]interface{}) {
	mu.Lock()
	WSPlaying = true
	WSState = "playing"
	WSLastEventTS = Now()
	mu.Unlock()

	params := jsonMap(msg, "params")
	data := jsonMap(params, "data")
	if data == nil {
		data = make(map[string]interface{})
	}
	playerParams := jsonMap(data, "player")
	if playerParams == nil {
		playerParams = make(map[string]interface{})
	}
	itemParams := jsonMap(data, "item")
	if itemParams == nil {
		itemParams = make(map[string]interface{})
	}

	var item map[string]interface{}
	if _, ok := playerParams["playerid"]; ok {
		pid := jsonInt(playerParams, "playerid", -1)
		mu.Lock()
		LastWSPlayerID = &pid
		mu.Unlock()

		res := KodiCall("Player.GetItem", map[string]interface{}{
			"playerid":   pid,
			"properties": PlayerGetItemProperties,
		})
		result := jsonMap(res, "result")
		if result != nil {
			item, _ = result["item"].(map[string]interface{})
		}
		if item != nil {
			playingFile := jsonStr(item, "file")
			if playingFile != "" {
				mu.Lock()
				LastWSPlayingFile = playingFile
				mu.Unlock()
			}
		}
	}

	// Update last item
	hasItemInfo := false
	for _, k := range []string{"id", "type", "title"} {
		if _, ok := itemParams[k]; ok {
			hasItemInfo = true
			break
		}
	}
	if hasItemInfo {
		mu.Lock()
		LastWSItem = make(map[string]interface{})
		for _, k := range []string{"id", "type", "title"} {
			if v, ok := itemParams[k]; ok {
				LastWSItem[k] = v
			}
		}
		mu.Unlock()
	}

	if item == nil {
		pid := jsonInt(playerParams, "playerid", -1)
		if pid >= 0 {
			res := KodiCall("Player.GetItem", map[string]interface{}{
				"playerid":   pid,
				"properties": PlayerGetItemProperties,
			})
			result := jsonMap(res, "result")
			if result != nil {
				item, _ = result["item"].(map[string]interface{})
			}
		}
	}

	if wsOnPlay != nil {
		wsOnPlay(item, itemParams)
	}
	if wsOnPlaybackRefresh != nil {
		wsOnPlaybackRefresh()
	}
}

func handlePlayerOnPause() {
	mu.Lock()
	WSPlaying = false
	WSState = "paused"
	WSLastEventTS = Now()
	mu.Unlock()
	if wsOnPause != nil {
		wsOnPause()
	}
}

func handlePlayerOnResume() {
	mu.Lock()
	WSPlaying = true
	WSState = "playing"
	WSLastEventTS = Now()
	mu.Unlock()
	if wsOnResume != nil {
		wsOnResume()
	}
}

func handlePlayerOnStop(msg map[string]interface{}) {
	mu.Lock()
	WSPlaying = false
	WSState = "stopped"
	WSLastEventTS = Now()
	mu.Unlock()

	params := jsonMap(msg, "params")
	data := jsonMap(params, "data")
	if data == nil {
		data = make(map[string]interface{})
	}
	itemParams := jsonMap(data, "item")
	if itemParams == nil {
		itemParams = make(map[string]interface{})
	}
	playerParams := jsonMap(data, "player")
	if playerParams == nil {
		playerParams = make(map[string]interface{})
	}

	stoppedFile := jsonStr(itemParams, "file")
	if stoppedFile == "" {
		mu.Lock()
		stoppedFile = LastWSPlayingFile
		mu.Unlock()
	}

	// Temp media cleanup is handled by the media package via the wsOnStop callback
	mu.Lock()
	LastWSPlayingFile = ""
	mu.Unlock()

	if wsOnStop != nil {
		wsOnStop(itemParams, playerParams)
	}
}

// IsSoundcloudStreamURL returns true if the URL looks like a SoundCloud CDN stream.
func IsSoundcloudStreamURL(u string) bool {
	return strings.Contains(u, "sndcdn") || strings.Contains(u, "media-streaming.soundcloud.cloud")
}

// ExtractSoundcloudURL extracts a SoundCloud URL from a Kodi plugin file URL.
func ExtractSoundcloudURL(fileURL string) string {
	if fileURL == "" {
		return ""
	}
	if !strings.HasPrefix(fileURL, "plugin://plugin.audio.soundcloud/play/") {
		return ""
	}
	parsed, err := url.Parse(fileURL)
	if err != nil {
		return ""
	}
	qs := parsed.Query()
	raw := qs.Get("url")
	if raw == "" {
		return ""
	}
	decoded, err := url.QueryUnescape(raw)
	if err != nil {
		decoded = raw
	}
	// Remove query params from the decoded URL
	idx := strings.Index(decoded, "?")
	if idx >= 0 {
		decoded = decoded[:idx]
	}
	if SCRE.MatchString(decoded) {
		return decoded
	}
	decoded2, err := url.QueryUnescape(raw)
	if err != nil {
		return raw
	}
	return decoded2
}


