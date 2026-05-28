package telegram

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"

	"github.com/PaulSonOfLars/gotgbot/v2"
	"github.com/PaulSonOfLars/gotgbot/v2/ext"

	KA "github.com/willheisenberg/KodiMediaBot/internal/core/kodiapi"
	"github.com/willheisenberg/KodiMediaBot/internal/core/queue"
)

// handleText is the main text message handler.
func handleText(bot *gotgbot.Bot, ctx *ext.Context) error {
	msg := ctx.EffectiveMessage
	chatID := ctx.EffectiveChat.Id
	text := strings.TrimSpace(msg.Text)

	if text == "" {
		return nil
	}

	StateMu.Lock()
	ActiveChatID = chatID
	StateMu.Unlock()

	// Check for YouTube URL
	ytMatch := KA.YTRE.FindStringSubmatch(text)
	plMatch := KA.PLRE.FindStringSubmatch(text)

	if plMatch != nil {
		pid := plMatch[1]
		go handlePlaylistQueue(bot, chatID, pid)
		return nil
	}

	if ytMatch != nil {
		vid := ytMatch[1]
		go handleYouTubeQueue(bot, chatID, vid, text)
		return nil
	}

	// Check for SoundCloud URL
	if queue.SCShortRE.MatchString(text) {
		go handleSoundcloudShort(bot, chatID, text)
		return nil
	}

	if queue.SCSetRE.MatchString(text) {
		go handleSoundcloudSet(bot, chatID, text)
		return nil
	}

	if queue.IsSCTrackURL(text) {
		go handleSoundcloudTrack(bot, chatID, text)
		return nil
	}

	// Check for social video URL
	if IsSocialVideoURL(text) {
		go handleSocialVideo(bot, chatID, text)
		return nil
	}

	// Check for generic HTTP URL
	if strings.HasPrefix(text, "http://") || strings.HasPrefix(text, "https://") {
		go handleDirectURL(bot, chatID, text)
		return nil
	}

	// Check for volume command
	if handleVolumeCommand(bot, chatID, text) {
		return nil
	}

	// Check for seek command
	if handleSeekCommand(bot, chatID, text) {
		return nil
	}

	// Treat as search query
	go handleSearchQuery(bot, chatID, text)
	return nil
}

// ── YouTube handling ────────────────────────────────────────────────

func handleYouTubeQueue(bot *gotgbot.Bot, chatID int64, vid, originalText string) {
	_, _ = SendAndTrack(bot, chatID, "⏳ YouTube wird geladen...", nil)

	title := queue.FetchYoutubeTitle(vid)
	queue.QueueVideo(vid, title)

	queue.Mu.Lock()
	qLen := len(queue.Queue)
	queue.Mu.Unlock()

	_, _ = SendAndTrack(bot, chatID, fmt.Sprintf("✅ *%s* hinzugefügt \\(Queue: %d\\)", escapeMarkdown(title), qLen), &gotgbot.SendMessageOpts{
		ParseMode: "MarkdownV2",
	})

	if qLen == 1 {
		queue.PlayIndex(0)
	}
	UpdateListMessage(bot, chatID)
}

func handlePlaylistQueue(bot *gotgbot.Bot, chatID int64, pid string) {
	_, _ = SendAndTrack(bot, chatID, "⏳ Playlist wird geladen...", nil)

	vids := queue.ExpandPlaylist(pid)
	if len(vids) == 0 {
		_, _ = SendAndTrack(bot, chatID, "❌ Playlist leer oder nicht gefunden.", nil)
		return
	}

	wasEmpty := len(queue.Queue) == 0
	for _, vid := range vids {
		title := queue.FetchYoutubeTitle(vid)
		queue.QueueVideo(vid, title)
	}

	_, _ = SendAndTrack(bot, chatID, fmt.Sprintf("✅ %d Videos hinzugefügt!", len(vids)), nil)

	if wasEmpty {
		queue.PlayIndex(0)
	}
	UpdateListMessage(bot, chatID)
}

// ── SoundCloud handling ─────────────────────────────────────────────

func handleSoundcloudTrack(bot *gotgbot.Bot, chatID int64, scURL string) {
	wasEmpty := len(queue.Queue) == 0
	queue.QueueItem(queue.MakeSoundcloud(scURL))

	queue.Mu.Lock()
	qLen := len(queue.Queue)
	queue.Mu.Unlock()

	title := queue.SoundcloudDisplayTitle(scURL)
	_, _ = SendAndTrack(bot, chatID, fmt.Sprintf("✅ *%s* hinzugefügt \\(Queue: %d\\)", escapeMarkdown(title), qLen), &gotgbot.SendMessageOpts{
		ParseMode: "MarkdownV2",
	})

	if wasEmpty {
		queue.PlayIndex(0)
	}
	UpdateListMessage(bot, chatID)
}

func handleSoundcloudSet(bot *gotgbot.Bot, chatID int64, setURL string) {
	_, _ = SendAndTrack(bot, chatID, "⏳ SoundCloud Set wird geladen...", nil)

	wasEmpty := len(queue.Queue) == 0
	count := queue.QueueSoundcloudSet(setURL)

	if count == 0 {
		_, _ = SendAndTrack(bot, chatID, "❌ Set leer oder nicht gefunden.", nil)
		return
	}

	_, _ = SendAndTrack(bot, chatID, fmt.Sprintf("✅ %d Tracks hinzugefügt!", count), nil)

	if wasEmpty {
		queue.PlayIndex(0)
	}
	UpdateListMessage(bot, chatID)
}

func handleSoundcloudShort(bot *gotgbot.Bot, chatID int64, shortURL string) {
	resolved := queue.ResolveSCShort(shortURL)
	if resolved == "" {
		_, _ = SendAndTrack(bot, chatID, "❌ SoundCloud-Link konnte nicht aufgelöst werden.", nil)
		return
	}
	if queue.IsSCSetURL(resolved) {
		handleSoundcloudSet(bot, chatID, resolved)
	} else if queue.IsSCTrackURL(resolved) {
		handleSoundcloudTrack(bot, chatID, resolved)
	} else {
		_, _ = SendAndTrack(bot, chatID, "❌ Unbekannter SoundCloud-Link.", nil)
	}
}

// ── Social video ────────────────────────────────────────────────────

func handleSocialVideo(bot *gotgbot.Bot, chatID int64, videoURL string) {
	_, _ = SendAndTrack(bot, chatID, "⏳ Video wird heruntergeladen...", nil)

	localPath, err := DownloadSocialVideo(videoURL, "social_video")
	if err != nil {
		_, _ = SendAndTrack(bot, chatID, fmt.Sprintf("❌ %s", err.Error()), nil)
		return
	}

	localPath = MaybeFaststartMP4(localPath)
	item := RegisterTempMedia(localPath, "Social Video")
	itemTitle, _ := item["title"].(string)
	itemURL, _ := item["url"].(string)
	itemKind, _ := item["kind"].(string)

	wasEmpty := len(queue.Queue) == 0
	queue.QueueItem(queue.MakeItem(itemTitle, itemURL, itemKind, "", ""))

	_, _ = SendAndTrack(bot, chatID, "✅ Video hinzugefügt!", nil)

	if wasEmpty {
		queue.PlayIndex(0)
	}
	UpdateListMessage(bot, chatID)
}

// ── Direct URL ──────────────────────────────────────────────────────

func handleDirectURL(bot *gotgbot.Bot, chatID int64, urlStr string) {
	wasEmpty := len(queue.Queue) == 0

	// Determine kind
	kind := "video"
	lower := strings.ToLower(urlStr)
	audioExts := []string{".mp3", ".ogg", ".flac", ".aac", ".wav", ".m4a", ".opus"}
	for _, ext := range audioExts {
		if strings.HasSuffix(lower, ext) || strings.Contains(lower, ext+"?") {
			kind = "audio"
			break
		}
	}

	title := urlStr
	if len(title) > 60 {
		title = title[:57] + "..."
	}

	item := queue.MakeItem(title, urlStr, kind, urlStr, "")
	queue.QueueItem(item)

	queue.Mu.Lock()
	qLen := len(queue.Queue)
	queue.Mu.Unlock()

	_, _ = SendAndTrack(bot, chatID, fmt.Sprintf("✅ Link hinzugefügt \\(Queue: %d\\)", qLen), &gotgbot.SendMessageOpts{
		ParseMode: "MarkdownV2",
	})

	if wasEmpty {
		queue.PlayIndex(0)
	}
	UpdateListMessage(bot, chatID)
}

// ── Volume commands ─────────────────────────────────────────────────

func handleVolumeCommand(bot *gotgbot.Bot, chatID int64, text string) bool {
	lower := strings.ToLower(text)
	volRE := regexp.MustCompile(`^vol\s*([+-]?\d+)$`)

	m := volRE.FindStringSubmatch(lower)
	if m == nil {
		return false
	}
	delta, err := strconv.Atoi(m[1])
	if err != nil {
		return false
	}

	go func() {
		ok := KA.RunVolumeDelta(delta)
		if ok {
			_, _ = SendAndTrack(bot, chatID, fmt.Sprintf("🔊 Volume %+d", delta), nil)
		} else {
			_, _ = SendAndTrack(bot, chatID, "❌ Volume-Änderung fehlgeschlagen.", nil)
		}
	}()
	return true
}

// ── Seek commands ───────────────────────────────────────────────────

func handleSeekCommand(bot *gotgbot.Bot, chatID int64, text string) bool {
	lower := strings.ToLower(text)

	seekRE := regexp.MustCompile(`^seek\s*([+-]?\d+)$`)
	m := seekRE.FindStringSubmatch(lower)
	if m != nil {
		delta, err := strconv.Atoi(m[1])
		if err == nil {
			go func() {
				ok := queue.SeekRelativeSeconds(delta)
				if !ok {
					_, _ = SendAndTrack(bot, chatID, "❌ Seek fehlgeschlagen.", nil)
				}
			}()
			return true
		}
	}

	seekPctRE := regexp.MustCompile(`^seek\s*(\d+)%$`)
	m = seekPctRE.FindStringSubmatch(lower)
	if m != nil {
		pct, err := strconv.Atoi(m[1])
		if err == nil && pct >= 0 && pct <= 100 {
			go func() {
				ok := queue.SeekPercent(pct)
				if !ok {
					_, _ = SendAndTrack(bot, chatID, "❌ Seek fehlgeschlagen.", nil)
				}
			}()
			return true
		}
	}

	return false
}

// ── Search query ────────────────────────────────────────────────────

func handleSearchQuery(bot *gotgbot.Bot, chatID int64, query string) {
	_, _ = SendAndTrack(bot, chatID, fmt.Sprintf("🔍 Suche nach: *%s*...", escapeMarkdown(query)), &gotgbot.SendMessageOpts{
		ParseMode: "MarkdownV2",
	})

	// Search YouTube
	link := KA.SearchYoutubeLink(query, "")
	if link == "" {
		_, _ = SendAndTrack(bot, chatID, "❌ Keine Ergebnisse gefunden.", nil)
		return
	}

	vid := KA.ExtractYoutubeID(link)
	if vid == "" {
		_, _ = SendAndTrack(bot, chatID, "❌ Kein gültiger YouTube-Link gefunden.", nil)
		return
	}

	handleYouTubeQueue(bot, chatID, vid, query)
}
