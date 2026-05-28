package telegram

import (
	"fmt"
	"log"
	"strings"

	"github.com/PaulSonOfLars/gotgbot/v2"
	KA "github.com/willheisenberg/KodiMediaBot/internal/core/kodiapi"
	"github.com/willheisenberg/KodiMediaBot/internal/core/queue"
	ha "github.com/willheisenberg/KodiMediaBot/internal/core/homeassistant"
	"github.com/willheisenberg/KodiMediaBot/internal/config"
)

// ── Control Panel Keyboard ──────────────────────────────────────────

func ControlPanel() gotgbot.InlineKeyboardMarkup {
	cfg := config.Get()
	rows := [][]gotgbot.InlineKeyboardButton{
		{
			{Text: "⏮", CallbackData: "back"},
			{Text: "⏯", CallbackData: "playpause"},
			{Text: "⏭", CallbackData: "skip"},
			{Text: "⏹", CallbackData: "stop"},
		},
		{
			{Text: "🔉", CallbackData: "vol_down"},
			{Text: "🔊", CallbackData: "vol_up"},
			{Text: "⏪", CallbackData: "seek_back"},
			{Text: "⏩", CallbackData: "seek_fwd"},
		},
		{
			{Text: "🔁", CallbackData: "repeat"},
			{Text: "🔀", CallbackData: "shuffle"},
			{Text: "📋", CallbackData: "queue_menu"},
			{Text: "💾", CallbackData: "playlist_menu"},
		},
	}

	// Optional rows
	var extraBtns []gotgbot.InlineKeyboardButton
	if cfg.CECHost != "" || cfg.DenonHost != "" {
		extraBtns = append(extraBtns, gotgbot.InlineKeyboardButton{Text: "🔌", CallbackData: "power_menu"})
	}
	if ha.HAAvailable() {
		extraBtns = append(extraBtns, gotgbot.InlineKeyboardButton{Text: "💡", CallbackData: "ha_menu"})
	}
	extraBtns = append(extraBtns, gotgbot.InlineKeyboardButton{Text: "⚙️", CallbackData: "settings_menu"})

	if len(extraBtns) > 0 {
		rows = append(rows, extraBtns)
	}

	return gotgbot.InlineKeyboardMarkup{InlineKeyboard: rows}
}

// ── Queue list text ─────────────────────────────────────────────────

func BuildListText() string {
	queue.Mu.Lock()
	q := make([]map[string]interface{}, len(queue.Queue))
	copy(q, queue.Queue)
	dispIdx := queue.DisplayIndex
	queue.Mu.Unlock()

	if len(q) == 0 {
		return "📭 Queue ist leer."
	}

	var lines []string
	for i, item := range q {
		title, _ := item["title"].(string)
		link, _ := item["link"].(string)

		prefix := " "
		if dispIdx != nil && i == *dispIdx {
			prefix = "▶️"
		}

		line := FormatItemLine(i, prefix, title, link)
		lines = append(lines, line)
	}
	return strings.Join(lines, "\n")
}

func FormatItemLine(index int, prefix, title, link string) string {
	display := title
	if display == "" {
		display = "Unbekannt"
	}
	if len(display) > 60 {
		display = display[:57] + "..."
	}

	if link != "" {
		return fmt.Sprintf("%s %d\\. [%s](%s)", prefix, index+1, escapeMarkdown(display), link)
	}
	return fmt.Sprintf("%s %d\\. %s", prefix, index+1, escapeMarkdown(display))
}

// ── Now Playing Text ────────────────────────────────────────────────

func GetNowPlayingText() string {
	// Check if queue is playing
	queue.Mu.Lock()
	dispIdx := queue.DisplayIndex
	qLen := len(queue.Queue)
	autoplay := queue.AutoplayEnabled
	repeatMode := queue.RepeatMode
	external := queue.ExternalPlayback
	queue.Mu.Unlock()

	// Get active player info from Kodi
	pid := KA.GetActivePlayerID()
	if pid < 0 && (dispIdx == nil || !autoplay) && !external {
		return ""
	}

	var displayName, link string

	if pid >= 0 {
		res := KA.KodiCall("Player.GetItem", map[string]interface{}{
			"playerid":   pid,
			"properties": []string{"title", "artist", "album", "file", "showtitle", "season", "episode", "channel", "uniqueid", "imdbnumber", "type"},
		})
		result, _ := res["result"].(map[string]interface{})
		item, _ := result["item"].(map[string]interface{})
		if item != nil {
			displayName, link = KA.ExternalItemDisplay(item)
		}

		// Progress
		props := KA.KodiCall("Player.GetProperties", map[string]interface{}{
			"playerid":   pid,
			"properties": []string{"time", "totaltime", "speed", "percentage"},
		})
		propResult, _ := props["result"].(map[string]interface{})
		if propResult != nil {
			timeMap, _ := propResult["time"].(map[string]interface{})
			totalMap, _ := propResult["totaltime"].(map[string]interface{})

			// Update queue progress tracking
			queue.Mu.Lock()
			queue.LastProgressTS = KA.Now()
			queue.LastProgressTime = timeMap
			queue.LastProgressTotal = totalMap
			if queue.DisplayIndex != nil {
				idx := *queue.DisplayIndex
				queue.LastProgressIndex = &idx
			}
			queue.Mu.Unlock()

			timeStr := KA.FormatKodiTime(timeMap)
			totalStr := KA.FormatKodiTime(totalMap)
			speed, _ := propResult["speed"].(float64)

			stateIcon := "▶️"
			if speed == 0 {
				stateIcon = "⏸"
			}

			if displayName == "" {
				displayName = "Unknown"
			}

			text := fmt.Sprintf("%s *%s*", stateIcon, escapeMarkdown(displayName))
			if link != "" {
				text = fmt.Sprintf("%s [%s](%s)", stateIcon, escapeMarkdown(displayName), link)
			}
			text += fmt.Sprintf("\n⏱ %s / %s", timeStr, totalStr)

			// Queue position
			if dispIdx != nil && qLen > 0 {
				text += fmt.Sprintf("  \\[%d/%d\\]", *dispIdx+1, qLen)
			}

			// Repeat mode indicator
			switch repeatMode {
			case "one":
				text += " 🔂"
			case "all":
				text += " 🔁"
			}

			return text
		}
	}

	if displayName == "" && dispIdx != nil && qLen > 0 {
		queue.Mu.Lock()
		if *dispIdx < len(queue.Queue) {
			item := queue.Queue[*dispIdx]
			displayName, _ = item["title"].(string)
			link, _ = item["link"].(string)
		}
		queue.Mu.Unlock()
	}

	if displayName != "" {
		text := fmt.Sprintf("⏳ *%s*", escapeMarkdown(displayName))
		if link != "" {
			text = fmt.Sprintf("⏳ [%s](%s)", escapeMarkdown(displayName), link)
		}
		return text
	}

	return ""
}

// ── Panel message management ────────────────────────────────────────

func SendControlPanel(bot *gotgbot.Bot, chatID int64) {
	panel := ControlPanel()
	var msg *gotgbot.Message
	err := TelegramRequest(func() error {
		var e error
		msg, e = bot.SendMessage(chatID, "🎛 *Control Panel*", &gotgbot.SendMessageOpts{
			ParseMode: "MarkdownV2",
			ReplyMarkup: panel,
		})
		return e
	})
	if err != nil {
		log.Printf("ERROR Failed to send control panel: %v", err)
		return
	}
	if msg != nil {
		StateMu.Lock()
		PanelMsgID = msg.MessageId
		StateMu.Unlock()
	}
}

func UpdateNowPlayingMessage(bot *gotgbot.Bot, chatID int64) {
	if chatID == 0 {
		return
	}
	text := GetNowPlayingText()
	if text == "" {
		text = "⏹ Nichts wird abgespielt."
	}

	StateMu.Lock()
	msgID := ProgressMsgID
	StateMu.Unlock()

	if msgID > 0 {
		// Try to edit existing message
		err := TelegramRequest(func() error {
			_, _, e := bot.EditMessageText(text, &gotgbot.EditMessageTextOpts{
				ChatId:    chatID,
				MessageId: msgID,
				ParseMode: "MarkdownV2",
				LinkPreviewOptions: &gotgbot.LinkPreviewOptions{IsDisabled: true},
			})
			return e
		})
		if err == nil {
			return
		}
		// Message gone, send new one
	}

	var msg *gotgbot.Message
	err := TelegramRequest(func() error {
		var e error
		msg, e = bot.SendMessage(chatID, text, &gotgbot.SendMessageOpts{
			ParseMode: "MarkdownV2",
			LinkPreviewOptions: &gotgbot.LinkPreviewOptions{IsDisabled: true},
		})
		return e
	})
	if err != nil {
		log.Printf("ERROR Failed to send now playing: %v", err)
		return
	}
	if msg != nil {
		StateMu.Lock()
		ProgressMsgID = msg.MessageId
		StateMu.Unlock()
	}
}

func UpdateListMessage(bot *gotgbot.Bot, chatID int64) {
	if chatID == 0 {
		return
	}
	text := BuildListText()
	if text == "" {
		return
	}

	StateMu.Lock()
	msgID := ListMsgID
	StateMu.Unlock()

	if msgID > 0 {
		err := TelegramRequest(func() error {
			_, _, e := bot.EditMessageText(text, &gotgbot.EditMessageTextOpts{
				ChatId:    chatID,
				MessageId: msgID,
				ParseMode: "MarkdownV2",
				LinkPreviewOptions: &gotgbot.LinkPreviewOptions{IsDisabled: true},
			})
			return e
		})
		if err == nil {
			return
		}
	}

	var msg *gotgbot.Message
	err := TelegramRequest(func() error {
		var e error
		msg, e = bot.SendMessage(chatID, text, &gotgbot.SendMessageOpts{
			ParseMode: "MarkdownV2",
			LinkPreviewOptions: &gotgbot.LinkPreviewOptions{IsDisabled: true},
		})
		return e
	})
	if err != nil {
		log.Printf("ERROR Failed to send list: %v", err)
		return
	}
	if msg != nil {
		StateMu.Lock()
		ListMsgID = msg.MessageId
		StateMu.Unlock()
	}
}

// AVStreamLabel formats an audio stream label.
func AVStreamLabel(stream map[string]interface{}) string {
	name, _ := stream["name"].(string)
	lang, _ := stream["language"].(string)
	codec, _ := stream["codec"].(string)
	channels := 0
	if v, ok := stream["channels"].(float64); ok {
		channels = int(v)
	}

	parts := []string{}
	if name != "" {
		parts = append(parts, name)
	}
	if lang != "" {
		langLower := strings.ToLower(lang)
		if entry, ok := LangMap[langLower]; ok {
			parts = append(parts, entry.Flag+" "+entry.Name)
		} else {
			parts = append(parts, lang)
		}
	}
	if codec != "" {
		parts = append(parts, strings.ToUpper(codec))
	}
	if channels > 0 {
		chStr := "Mono"
		switch channels {
		case 2:
			chStr = "Stereo"
		case 6:
			chStr = "5.1"
		case 8:
			chStr = "7.1"
		default:
			chStr = fmt.Sprintf("%dch", channels)
		}
		parts = append(parts, chStr)
	}
	if len(parts) == 0 {
		return "Unknown"
	}
	return strings.Join(parts, " | ")
}

// CurrentSubtitleLabel formats the current subtitle label.
func CurrentSubtitleLabel(info map[string]interface{}) string {
	enabled, _ := info["subtitleenabled"].(bool)
	if !enabled {
		return "Aus"
	}
	current, _ := info["currentsubtitle"].(map[string]interface{})
	if current == nil {
		return "Ein"
	}
	name, _ := current["name"].(string)
	lang, _ := current["language"].(string)
	if name != "" {
		return name
	}
	if lang != "" {
		langLower := strings.ToLower(lang)
		if entry, ok := LangMap[langLower]; ok {
			return entry.Flag + " " + entry.Name
		}
		return lang
	}
	return "Ein"
}

// ── HiFi status helpers ─────────────────────────────────────────────

func RefreshHiFiCache() {
	now := KA.Now()
	StateMu.Lock()
	if now-HiFiCacheTS < HiFiCacheTTL {
		StateMu.Unlock()
		return
	}
	StateMu.Unlock()

	cfg := config.Get()
	hifiStatus := ""
	airplayStatus := ""
	denonVolume := ""

	if cfg.CECHost != "" || cfg.DenonHost != "" {
		hifiStatus = KA.GetHiFiPowerStatus()
	}
	if cfg.DenonHost != "" {
		airplayStatus = KA.GetAirplayStatus()
		denonVolume = KA.GetDenonMainzoneVolume()
	}

	StateMu.Lock()
	HiFiStatusCache = hifiStatus
	AirplayStatusCache = airplayStatus
	DenonVolumeCache = denonVolume
	HiFiCacheTS = now
	StateMu.Unlock()
}

// Ignore unused import warning — config is used via config.Get()
var _ = config.Get
