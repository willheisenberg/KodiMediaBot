package telegram

import (
	"fmt"
	"log"
	"strconv"
	"strings"
	"time"

	"github.com/PaulSonOfLars/gotgbot/v2"
	"github.com/PaulSonOfLars/gotgbot/v2/ext"

	KA "github.com/willheisenberg/KodiMediaBot/internal/core/kodiapi"
	ha "github.com/willheisenberg/KodiMediaBot/internal/core/homeassistant"
	"github.com/willheisenberg/KodiMediaBot/internal/core/playlist"
	"github.com/willheisenberg/KodiMediaBot/internal/core/projector"
	"github.com/willheisenberg/KodiMediaBot/internal/core/queue"
	"github.com/willheisenberg/KodiMediaBot/internal/config"
)

// onButton handles all inline keyboard callback queries.
func onButton(bot *gotgbot.Bot, ctx *ext.Context) error {
	cb := ctx.CallbackQuery
	data := cb.Data
	chatID := cb.Message.GetChat().Id

	StateMu.Lock()
	ActiveChatID = chatID
	StateMu.Unlock()

	// Acknowledge the callback
	_, _ = cb.Answer(bot, nil)

	switch {
	// ── Playback controls ───────────────────────────────
	case data == "playpause":
		go handlePlayPause(bot, chatID)

	case data == "skip":
		go func() {
			queue.SkipQueue()
			UpdateNowPlayingMessage(bot, chatID)
			UpdateListMessage(bot, chatID)
		}()

	case data == "back":
		go func() {
			queue.BackQueue()
			UpdateNowPlayingMessage(bot, chatID)
			UpdateListMessage(bot, chatID)
		}()

	case data == "stop":
		go func() {
			queue.HardStopAndClear()
			UpdateNowPlayingMessage(bot, chatID)
			UpdateListMessage(bot, chatID)
		}()

	// ── Volume ──────────────────────────────────────────
	case data == "vol_up":
		go KA.RunVolumeDelta(3)

	case data == "vol_down":
		go KA.RunVolumeDelta(-3)

	// ── Seek ────────────────────────────────────────────
	case data == "seek_fwd":
		go func() {
			queue.SeekRelativeSeconds(30)
			UpdateNowPlayingMessage(bot, chatID)
		}()

	case data == "seek_back":
		go func() {
			queue.SeekRelativeSeconds(-15)
			UpdateNowPlayingMessage(bot, chatID)
		}()

	// ── Repeat mode ─────────────────────────────────────
	case data == "repeat":
		go handleRepeatToggle(bot, chatID)

	// ── Shuffle ─────────────────────────────────────────
	case data == "shuffle":
		go handleShuffle(bot, chatID)

	// ── Queue menu ──────────────────────────────────────
	case data == "queue_menu":
		go handleQueueMenu(bot, chatID)

	case strings.HasPrefix(data, "queue_del:"):
		go handleQueueDelete(bot, chatID, data)

	case strings.HasPrefix(data, "queue_play:"):
		go handleQueuePlay(bot, chatID, data)

	case data == "queue_clear":
		go func() {
			queue.ClearQueue()
			_, _ = SendAndTrack(bot, chatID, "🗑 Queue gelöscht.", nil)
			UpdateListMessage(bot, chatID)
		}()

	// ── Playlist menu ───────────────────────────────────
	case data == "playlist_menu":
		go handlePlaylistMenu(bot, chatID)

	case data == "playlist_save":
		go handlePlaylistSave(bot, chatID)

	case strings.HasPrefix(data, "playlist_load:"):
		go handlePlaylistLoad(bot, chatID, data)

	case strings.HasPrefix(data, "playlist_del:"):
		go handlePlaylistDelete(bot, chatID, data)

	// ── Power menu ──────────────────────────────────────
	case data == "power_menu":
		go handlePowerMenu(bot, chatID)

	case data == "power_on":
		go func() {
			KA.RunCECPower(true)
			_, _ = SendAndTrack(bot, chatID, "🔌 Power ON gesendet.", nil)
			ResetHiFiCache()
		}()

	case data == "power_off":
		go func() {
			KA.RunCECPower(false)
			_, _ = SendAndTrack(bot, chatID, "🔌 Power OFF gesendet.", nil)
			ResetHiFiCache()
		}()

	case data == "airplay_kill":
		go func() {
			KA.RunAirplayKill()
			_, _ = SendAndTrack(bot, chatID, "📺 AirPlay deaktiviert.", nil)
			ResetHiFiCache()
		}()

	// ── Projector ───────────────────────────────────────
	case data == "projector_on":
		go func() {
			ok := projector.Projector.PowerOn()
			if ok {
				_, _ = SendAndTrack(bot, chatID, "📽 Beamer eingeschaltet.", nil)
			} else {
				_, _ = SendAndTrack(bot, chatID, "❌ Beamer konnte nicht eingeschaltet werden.", nil)
			}
		}()

	case data == "projector_off":
		go func() {
			ok := projector.Projector.PowerOff()
			if ok {
				_, _ = SendAndTrack(bot, chatID, "📽 Beamer ausgeschaltet.", nil)
			} else {
				_, _ = SendAndTrack(bot, chatID, "❌ Beamer konnte nicht ausgeschaltet werden.", nil)
			}
		}()

	// ── Settings menu ───────────────────────────────────
	case data == "settings_menu":
		go handleSettingsMenu(bot, chatID)

	case data == "av_settings":
		go handleAVSettings(bot, chatID)

	case strings.HasPrefix(data, "audio_stream:"):
		go handleAudioStreamSelect(bot, chatID, data)

	case strings.HasPrefix(data, "sub_stream:"):
		go handleSubtitleSelect(bot, chatID, data)

	case data == "sub_off":
		go func() {
			KA.DisableSubtitles()
			_, _ = SendAndTrack(bot, chatID, "🔇 Untertitel deaktiviert.", nil)
		}()

	// ── Favourites ──────────────────────────────────────
	case data == "fav_menu":
		go handleFavouritesMenu(bot, chatID)

	case strings.HasPrefix(data, "fav_play:"):
		go handleFavouritePlay(bot, chatID, data)

	// ── HA menu ─────────────────────────────────────────
	case data == "ha_menu":
		go handleHAMenu(bot, chatID)

	case data == "ha_toggle":
		go handleHAToggle(bot, chatID)

	case data == "ha_brightness_up":
		go handleHABrightness(bot, chatID, 10)

	case data == "ha_brightness_down":
		go handleHABrightness(bot, chatID, -10)

	default:
		log.Printf("DEBUG Unhandled callback: %s", data)
	}

	return nil
}

// ── Playback ────────────────────────────────────────────────────────

func handlePlayPause(bot *gotgbot.Bot, chatID int64) {
	pid := KA.GetActivePlayerID()
	if pid < 0 {
		// Nothing playing — start queue if there is one
		queue.Mu.Lock()
		qLen := len(queue.Queue)
		queue.Mu.Unlock()
		if qLen > 0 {
			queue.PlayIndex(0)
		}
		return
	}
	KA.KodiCall("Player.PlayPause", map[string]interface{}{"playerid": pid})
	UpdateNowPlayingMessage(bot, chatID)
}

func handleRepeatToggle(bot *gotgbot.Bot, chatID int64) {
	queue.Mu.Lock()
	switch queue.RepeatMode {
	case "off":
		queue.RepeatMode = "all"
	case "all":
		queue.RepeatMode = "one"
	case "one":
		queue.RepeatMode = "off"
	}
	mode := queue.RepeatMode
	queue.Mu.Unlock()

	icons := map[string]string{"off": "➡️", "all": "🔁", "one": "🔂"}
	_, _ = SendAndTrack(bot, chatID, fmt.Sprintf("%s Repeat: %s", icons[mode], mode), nil)
}

func handleShuffle(bot *gotgbot.Bot, chatID int64) {
	queue.Mu.Lock()
	qLen := len(queue.Queue)
	queue.Mu.Unlock()
	if qLen < 2 {
		_, _ = SendAndTrack(bot, chatID, "❌ Zu wenige Items zum Mischen.", nil)
		return
	}
	// Simple shuffle via random swaps
	queue.Mu.Lock()
	for i := len(queue.Queue) - 1; i > 0; i-- {
		j := int(KA.Now()*1000) % (i + 1)
		queue.Queue[i], queue.Queue[j] = queue.Queue[j], queue.Queue[i]
	}
	queue.DisplayIndex = nil
	queue.CurrentIndex = nil
	queue.NextIndex = 0
	queue.Mu.Unlock()
	queue.MarkListDirty()
	_, _ = SendAndTrack(bot, chatID, "🔀 Queue gemischt!", nil)
	UpdateListMessage(bot, chatID)
}

// ── Queue menu ──────────────────────────────────────────────────────

func handleQueueMenu(bot *gotgbot.Bot, chatID int64) {
	queue.Mu.Lock()
	qLen := len(queue.Queue)
	queue.Mu.Unlock()

	if qLen == 0 {
		_, _ = SendAndTrack(bot, chatID, "📭 Queue ist leer.", nil)
		return
	}

	UpdateListMessage(bot, chatID)

	// Build delete/play buttons (first 10 items)
	maxItems := 10
	if qLen < maxItems {
		maxItems = qLen
	}
	var rows [][]gotgbot.InlineKeyboardButton
	for i := 0; i < maxItems; i++ {
		queue.Mu.Lock()
		title, _ := queue.Queue[i]["title"].(string)
		queue.Mu.Unlock()
		if len(title) > 25 {
			title = title[:22] + "..."
		}
		rows = append(rows, []gotgbot.InlineKeyboardButton{
			{Text: fmt.Sprintf("▶ %d", i+1), CallbackData: fmt.Sprintf("queue_play:%d", i)},
			{Text: fmt.Sprintf("🗑 %d", i+1), CallbackData: fmt.Sprintf("queue_del:%d", i)},
		})
	}
	rows = append(rows, []gotgbot.InlineKeyboardButton{
		{Text: "🗑 Alles löschen", CallbackData: "queue_clear"},
	})

	_ = TelegramRequest(func() error {
		_, e := bot.SendMessage(chatID, "Queue verwalten:", &gotgbot.SendMessageOpts{
			ReplyMarkup: gotgbot.InlineKeyboardMarkup{InlineKeyboard: rows},
		})
		return e
	})
}

func handleQueueDelete(bot *gotgbot.Bot, chatID int64, data string) {
	parts := strings.SplitN(data, ":", 2)
	if len(parts) != 2 {
		return
	}
	idx, err := strconv.Atoi(parts[1])
	if err != nil {
		return
	}
	ok, errMsg := queue.DeleteIndex(idx)
	if !ok {
		_, _ = SendAndTrack(bot, chatID, fmt.Sprintf("❌ %s", errMsg), nil)
		return
	}
	_, _ = SendAndTrack(bot, chatID, fmt.Sprintf("🗑 Item %d gelöscht.", idx+1), nil)
	UpdateListMessage(bot, chatID)
}

func handleQueuePlay(bot *gotgbot.Bot, chatID int64, data string) {
	parts := strings.SplitN(data, ":", 2)
	if len(parts) != 2 {
		return
	}
	idx, err := strconv.Atoi(parts[1])
	if err != nil {
		return
	}
	queue.PlayIndex(idx)
	UpdateNowPlayingMessage(bot, chatID)
	UpdateListMessage(bot, chatID)
}

// ── Playlist menu ───────────────────────────────────────────────────

func handlePlaylistMenu(bot *gotgbot.Bot, chatID int64) {
	cfg := config.Get()
	files := playlist.ListPlaylistFiles(cfg.PlaylistDir)

	var rows [][]gotgbot.InlineKeyboardButton
	rows = append(rows, []gotgbot.InlineKeyboardButton{
		{Text: "💾 Queue speichern", CallbackData: "playlist_save"},
	})

	for _, f := range files {
		name := strings.TrimSuffix(f, ".json")
		rows = append(rows, []gotgbot.InlineKeyboardButton{
			{Text: "▶ " + name, CallbackData: "playlist_load:" + f},
			{Text: "🗑", CallbackData: "playlist_del:" + f},
		})
	}

	_ = TelegramRequest(func() error {
		_, e := bot.SendMessage(chatID, "💾 Playlists:", &gotgbot.SendMessageOpts{
			ReplyMarkup: gotgbot.InlineKeyboardMarkup{InlineKeyboard: rows},
		})
		return e
	})
}

func handlePlaylistSave(bot *gotgbot.Bot, chatID int64) {
	cfg := config.Get()
	queue.Mu.Lock()
	q := make([]map[string]interface{}, len(queue.Queue))
	copy(q, queue.Queue)
	queue.Mu.Unlock()

	if len(q) == 0 {
		_, _ = SendAndTrack(bot, chatID, "❌ Queue ist leer.", nil)
		return
	}

	name := fmt.Sprintf("playlist_%d", time.Now().Unix())
	ok, info := playlist.SavePlaylistToDisk(cfg.PlaylistDir, name, q)
	if !ok {
		_, _ = SendAndTrack(bot, chatID, fmt.Sprintf("❌ %s", info), nil)
		return
	}
	_, _ = SendAndTrack(bot, chatID, fmt.Sprintf("✅ Playlist gespeichert: %s", info), nil)
}

func handlePlaylistLoad(bot *gotgbot.Bot, chatID int64, data string) {
	cfg := config.Get()
	parts := strings.SplitN(data, ":", 2)
	if len(parts) != 2 {
		return
	}
	filename := parts[1]
	ok, result := playlist.LoadPlaylistFromDisk(cfg.PlaylistDir, filename)
	if !ok {
		errMsg, _ := result.(string)
		_, _ = SendAndTrack(bot, chatID, fmt.Sprintf("❌ %s", errMsg), nil)
		return
	}
	items, ok := result.([]map[string]interface{})
	if !ok {
		_, _ = SendAndTrack(bot, chatID, "❌ Ungültiges Playlist-Format.", nil)
		return
	}
	queue.ClearQueue()
	for _, item := range items {
		queue.QueueItem(item)
	}
	_, _ = SendAndTrack(bot, chatID, fmt.Sprintf("✅ Playlist geladen: %d Items.", len(items)), nil)
	queue.PlayIndex(0)
	UpdateListMessage(bot, chatID)
}

func handlePlaylistDelete(bot *gotgbot.Bot, chatID int64, data string) {
	cfg := config.Get()
	parts := strings.SplitN(data, ":", 2)
	if len(parts) != 2 {
		return
	}
	filename := parts[1]
	ok, info := playlist.DeletePlaylistFromDisk(cfg.PlaylistDir, filename)
	if !ok {
		_, _ = SendAndTrack(bot, chatID, fmt.Sprintf("❌ %s", info), nil)
		return
	}
	_, _ = SendAndTrack(bot, chatID, fmt.Sprintf("🗑 Playlist gelöscht: %s", info), nil)
}

// ── Power menu ──────────────────────────────────────────────────────

func handlePowerMenu(bot *gotgbot.Bot, chatID int64) {
	RefreshHiFiCache()

	StateMu.Lock()
	status := HiFiStatusCache
	airplay := AirplayStatusCache
	volume := DenonVolumeCache
	StateMu.Unlock()

	text := "🔌 *Power Menu*\n"
	if status != "" {
		text += fmt.Sprintf("HiFi: %s\n", status)
	}
	if airplay != "" {
		text += fmt.Sprintf("AirPlay: %s\n", airplay)
	}
	if volume != "" {
		text += fmt.Sprintf("Volume: %s dB\n", volume)
	}

	var rows [][]gotgbot.InlineKeyboardButton
	rows = append(rows, []gotgbot.InlineKeyboardButton{
		{Text: "🔌 Power ON", CallbackData: "power_on"},
		{Text: "🔌 Power OFF", CallbackData: "power_off"},
	})

	cfg := config.Get()
	if cfg.DenonHost != "" {
		rows = append(rows, []gotgbot.InlineKeyboardButton{
			{Text: "📺 AirPlay Kill", CallbackData: "airplay_kill"},
		})
	}
	rows = append(rows, []gotgbot.InlineKeyboardButton{
		{Text: "📽 Beamer AN", CallbackData: "projector_on"},
		{Text: "📽 Beamer AUS", CallbackData: "projector_off"},
	})

	_ = TelegramRequest(func() error {
		_, e := bot.SendMessage(chatID, text, &gotgbot.SendMessageOpts{
			ParseMode:   "Markdown",
			ReplyMarkup: gotgbot.InlineKeyboardMarkup{InlineKeyboard: rows},
		})
		return e
	})
}

// ── Settings menu ───────────────────────────────────────────────────

func handleSettingsMenu(bot *gotgbot.Bot, chatID int64) {
	var rows [][]gotgbot.InlineKeyboardButton
	rows = append(rows, []gotgbot.InlineKeyboardButton{
		{Text: "🔊 AV-Einstellungen", CallbackData: "av_settings"},
	})
	rows = append(rows, []gotgbot.InlineKeyboardButton{
		{Text: "⭐ Favoriten", CallbackData: "fav_menu"},
	})

	_ = TelegramRequest(func() error {
		_, e := bot.SendMessage(chatID, "⚙️ Einstellungen:", &gotgbot.SendMessageOpts{
			ReplyMarkup: gotgbot.InlineKeyboardMarkup{InlineKeyboard: rows},
		})
		return e
	})
}

func handleAVSettings(bot *gotgbot.Bot, chatID int64) {
	info := KA.GetAVSettings()
	if info["error"] != nil {
		_, _ = SendAndTrack(bot, chatID, "❌ Nichts wird abgespielt.", nil)
		return
	}

	text := "🔊 *AV\\-Einstellungen*\n"

	// Current audio stream
	currentAudio, _ := info["currentaudiostream"].(map[string]interface{})
	if currentAudio != nil {
		text += fmt.Sprintf("\n🔈 Audio: %s", escapeMarkdown(AVStreamLabel(currentAudio)))
	}

	// Current subtitle
	text += fmt.Sprintf("\n💬 Untertitel: %s", escapeMarkdown(CurrentSubtitleLabel(info)))

	// Audio stream buttons
	streams, _ := info["audiostreams"].([]interface{})
	var rows [][]gotgbot.InlineKeyboardButton
	if len(streams) > 1 {
		text += "\n\n*Audio\\-Spuren:*"
		for _, s := range streams {
			stream, ok := s.(map[string]interface{})
			if !ok {
				continue
			}
			idx := 0
			if v, ok := stream["index"].(float64); ok {
				idx = int(v)
			}
			label := AVStreamLabel(stream)
			if len(label) > 30 {
				label = label[:27] + "..."
			}
			rows = append(rows, []gotgbot.InlineKeyboardButton{
				{Text: label, CallbackData: fmt.Sprintf("audio_stream:%d", idx)},
			})
		}
	}

	// Subtitle buttons
	subs, _ := info["subtitles"].([]interface{})
	if len(subs) > 0 {
		text += "\n\n*Untertitel:*"
		for _, s := range subs {
			sub, ok := s.(map[string]interface{})
			if !ok {
				continue
			}
			idx := 0
			if v, ok := sub["index"].(float64); ok {
				idx = int(v)
			}
			name, _ := sub["name"].(string)
			lang, _ := sub["language"].(string)
			label := name
			if label == "" {
				label = lang
			}
			if label == "" {
				label = fmt.Sprintf("Sub %d", idx+1)
			}
			langLower := strings.ToLower(lang)
			if entry, ok2 := LangMap[langLower]; ok2 {
				label = entry.Flag + " " + label
			}
			rows = append(rows, []gotgbot.InlineKeyboardButton{
				{Text: label, CallbackData: fmt.Sprintf("sub_stream:%d", idx)},
			})
		}
		rows = append(rows, []gotgbot.InlineKeyboardButton{
			{Text: "🔇 Untertitel aus", CallbackData: "sub_off"},
		})
	}

	_ = TelegramRequest(func() error {
		_, e := bot.SendMessage(chatID, text, &gotgbot.SendMessageOpts{
			ParseMode:   "MarkdownV2",
			ReplyMarkup: gotgbot.InlineKeyboardMarkup{InlineKeyboard: rows},
		})
		return e
	})
}

func handleAudioStreamSelect(bot *gotgbot.Bot, chatID int64, data string) {
	parts := strings.SplitN(data, ":", 2)
	if len(parts) != 2 {
		return
	}
	idx, err := strconv.Atoi(parts[1])
	if err != nil {
		return
	}
	ok := KA.SetAudioStream(idx)
	if ok {
		_, _ = SendAndTrack(bot, chatID, fmt.Sprintf("🔈 Audio-Spur %d gewählt.", idx+1), nil)
	} else {
		_, _ = SendAndTrack(bot, chatID, "❌ Audio-Spur konnte nicht gewechselt werden.", nil)
	}
}

func handleSubtitleSelect(bot *gotgbot.Bot, chatID int64, data string) {
	parts := strings.SplitN(data, ":", 2)
	if len(parts) != 2 {
		return
	}
	idx, err := strconv.Atoi(parts[1])
	if err != nil {
		return
	}
	ok := KA.SetSubtitleStream(idx)
	if ok {
		_, _ = SendAndTrack(bot, chatID, fmt.Sprintf("💬 Untertitel %d gewählt.", idx+1), nil)
	} else {
		_, _ = SendAndTrack(bot, chatID, "❌ Untertitel konnte nicht gewechselt werden.", nil)
	}
}

// ── Favourites menu ─────────────────────────────────────────────────

func handleFavouritesMenu(bot *gotgbot.Bot, chatID int64) {
	favs := KA.GetPlayableFavourites()
	if len(favs) == 0 {
		_, _ = SendAndTrack(bot, chatID, "⭐ Keine Favoriten gefunden.", nil)
		return
	}

	var rows [][]gotgbot.InlineKeyboardButton
	for i, fav := range favs {
		title, _ := fav["title"].(string)
		if len(title) > 30 {
			title = title[:27] + "..."
		}
		rows = append(rows, []gotgbot.InlineKeyboardButton{
			{Text: "⭐ " + title, CallbackData: fmt.Sprintf("fav_play:%d", i)},
		})
	}

	_ = TelegramRequest(func() error {
		_, e := bot.SendMessage(chatID, "⭐ Favoriten:", &gotgbot.SendMessageOpts{
			ReplyMarkup: gotgbot.InlineKeyboardMarkup{InlineKeyboard: rows},
		})
		return e
	})
}

func handleFavouritePlay(bot *gotgbot.Bot, chatID int64, data string) {
	parts := strings.SplitN(data, ":", 2)
	if len(parts) != 2 {
		return
	}
	idx, err := strconv.Atoi(parts[1])
	if err != nil {
		return
	}
	favs := KA.GetPlayableFavourites()
	if idx < 0 || idx >= len(favs) {
		_, _ = SendAndTrack(bot, chatID, "❌ Ungültiger Favorit.", nil)
		return
	}
	fav := favs[idx]
	target, _ := fav["target"].(string)
	title, _ := fav["title"].(string)
	ok := KA.PlayFavouriteTarget(target, title)
	if ok {
		_, _ = SendAndTrack(bot, chatID, fmt.Sprintf("⭐ Spiele: %s", title), nil)
	} else {
		_, _ = SendAndTrack(bot, chatID, "❌ Favorit konnte nicht abgespielt werden.", nil)
	}
}

// ── HA menu ─────────────────────────────────────────────────────────

func handleHAMenu(bot *gotgbot.Bot, chatID int64) {
	if !ha.HAAvailable() {
		_, _ = SendAndTrack(bot, chatID, "❌ Home Assistant nicht konfiguriert.", nil)
		return
	}

	state := ha.GetLightState()
	stateStr := "Aus"
	if state != nil {
		if s, ok := state["state"].(string); ok && s == "on" {
			stateStr = "An"
		}
	}
	friendlyName := config.Get().HALightID
	if state != nil {
		if fn, ok := state["friendly_name"].(string); ok {
			friendlyName = fn
		}
	}

	text := fmt.Sprintf("💡 *%s* — %s", friendlyName, stateStr)
	if state != nil {
		if brightness, ok := state["brightness"]; ok {
			pct := ha.BrightnessPercentFromHA(brightness)
			if pct != nil {
				text += fmt.Sprintf(" (%d%%)", *pct)
			}
		}
	}

	var rows [][]gotgbot.InlineKeyboardButton
	rows = append(rows, []gotgbot.InlineKeyboardButton{
		{Text: "💡 Toggle", CallbackData: "ha_toggle"},
		{Text: "🔆+", CallbackData: "ha_brightness_up"},
		{Text: "🔅-", CallbackData: "ha_brightness_down"},
	})

	if ha.HAWebappAvailable() {
		webappURL := ha.ResolveHAWebappURL()
		rows = append(rows, []gotgbot.InlineKeyboardButton{
			{Text: "🎨 Farbwähler", WebApp: &gotgbot.WebAppInfo{Url: webappURL}},
		})
	}

	_ = TelegramRequest(func() error {
		_, e := bot.SendMessage(chatID, text, &gotgbot.SendMessageOpts{
			ParseMode:   "Markdown",
			ReplyMarkup: gotgbot.InlineKeyboardMarkup{InlineKeyboard: rows},
		})
		return e
	})
}

func handleHAToggle(bot *gotgbot.Bot, chatID int64) {
	ok, newState := ha.ToggleLight()
	if ok {
		icon := "💡"
		if newState == "off" {
			icon = "🌑"
		}
		_, _ = SendAndTrack(bot, chatID, fmt.Sprintf("%s Licht: %s", icon, newState), nil)
	} else {
		_, _ = SendAndTrack(bot, chatID, "❌ Toggle fehlgeschlagen.", nil)
	}
}

func handleHABrightness(bot *gotgbot.Bot, chatID int64, delta int) {
	state := ha.GetLightState()
	currentPct := 50
	if state != nil {
		if brightness, ok := state["brightness"]; ok {
			pct := ha.BrightnessPercentFromHA(brightness)
			if pct != nil {
				currentPct = *pct
			}
		}
	}
	newPct := currentPct + delta
	if newPct < 0 {
		newPct = 0
	}
	if newPct > 100 {
		newPct = 100
	}
	ok := ha.SetLightBrightness(newPct)
	if ok {
		_, _ = SendAndTrack(bot, chatID, fmt.Sprintf("🔆 Helligkeit: %d%%", newPct), nil)
	} else {
		_, _ = SendAndTrack(bot, chatID, "❌ Helligkeit konnte nicht geändert werden.", nil)
	}
}


