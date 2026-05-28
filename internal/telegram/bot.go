package telegram

import (
	"fmt"
	"log"
	"strings"

	"github.com/PaulSonOfLars/gotgbot/v2"
	"github.com/PaulSonOfLars/gotgbot/v2/ext"
	"github.com/PaulSonOfLars/gotgbot/v2/ext/handlers"
	"github.com/PaulSonOfLars/gotgbot/v2/ext/handlers/filters/message"

	"github.com/willheisenberg/KodiMediaBot/internal/config"
	KA "github.com/willheisenberg/KodiMediaBot/internal/core/kodiapi"
	"github.com/willheisenberg/KodiMediaBot/internal/core/queue"
)

// Run initialises the bot, registers handlers, starts the media server,
// WS listener, autoplay loop, and begins polling for Telegram updates.
func Run(token string) {
	cfg := config.Get()

	// Set up bot options
	var botOpts *gotgbot.BotOpts
	if cfg.TelegramBaseURL != "" {
		botOpts = &gotgbot.BotOpts{
			BotClient: &gotgbot.BaseBotClient{
				DefaultRequestOpts: &gotgbot.RequestOpts{
					APIURL: cfg.TelegramBaseURL,
				},
			},
		}
	}

	bot, err := gotgbot.NewBot(token, botOpts)
	if err != nil {
		log.Fatalf("FATAL Failed to create bot: %v", err)
	}

	log.Printf("INFO Bot authorized: %s", bot.User.Username)

	// Wire up callbacks
	queue.SetUICallbacks(
		func() { ScheduleNowPlayingRefreshFor(bot) },
		nil, // OnUnexpectedRadioStop
		nil, // CancelReconnect
	)
	queue.SetCleanupCallback(CleanupActiveImageSession)
	queue.RegisterWSCallbacks()

	// Start background services
	StartMediaServer()
	go KA.KodiWSListener()
	queue.StartAutoplayThread()
	CleanupStaleTempMedia()

	// Create updater
	dispatcher := ext.NewDispatcher(&ext.DispatcherOpts{
		Error: func(b *gotgbot.Bot, ctx *ext.Context, err error) ext.DispatcherAction {
			log.Printf("ERROR Handler error: %v", err)
			return ext.DispatcherActionNoop
		},
	})
	updater := ext.NewUpdater(dispatcher, nil)

	// Register command handlers
	dispatcher.AddHandler(handlers.NewCommand("start", startCommand))
	dispatcher.AddHandler(handlers.NewCommand("reset", resetPanelCommand))

	// Register callback query handler
	dispatcher.AddHandler(handlers.NewCallback(nil, onButton))

	// Register text message handler
	dispatcher.AddHandler(handlers.NewMessage(message.Text, handleText))

	// Register media handlers
	dispatcher.AddHandler(handlers.NewMessage(func(msg *gotgbot.Message) bool {
		return msg.Voice != nil || msg.Audio != nil || msg.Video != nil ||
			msg.VideoNote != nil || len(msg.Photo) > 0 || msg.Document != nil
	}, handleNontext))

	// Start polling
	log.Printf("INFO Starting long polling...")
	err = updater.StartPolling(bot, &ext.PollingOpts{
		DropPendingUpdates: false,
	})
	if err != nil {
		log.Fatalf("FATAL Failed to start polling: %v", err)
	}

	// Send startup message
	if cfg.StartupChatID != 0 {
		go func() {
			StateMu.Lock()
			ActiveChatID = cfg.StartupChatID
			StateMu.Unlock()
			_, _ = SendAndTrack(bot, cfg.StartupChatID, "🤖 Bot gestartet!", nil)
			SendControlPanel(bot, cfg.StartupChatID)
		}()
	}

	log.Printf("INFO Bot is running. Press Ctrl+C to stop.")
	updater.Idle()
}

// ScheduleNowPlayingRefreshFor schedules a now-playing panel refresh.
func ScheduleNowPlayingRefreshFor(bot *gotgbot.Bot) {
	StateMu.Lock()
	chatID := ActiveChatID
	NowPlayingRefreshPending = true
	StateMu.Unlock()

	if chatID == 0 {
		return
	}

	go func() {
		UpdateNowPlayingMessage(bot, chatID)
	}()
}

// startCommand handles the /start command.
func startCommand(bot *gotgbot.Bot, ctx *ext.Context) error {
	chatID := ctx.EffectiveChat.Id
	StateMu.Lock()
	ActiveChatID = chatID
	StateMu.Unlock()

	_, _ = SendAndTrack(bot, chatID, "🎵 *KodiMediaBot* — Willkommen!\n\nSende einen YouTube/SoundCloud-Link oder eine Textsuche.", &gotgbot.SendMessageOpts{
		ParseMode: "Markdown",
	})
	SendControlPanel(bot, chatID)
	return nil
}

// resetPanelCommand handles the /reset command.
func resetPanelCommand(bot *gotgbot.Bot, ctx *ext.Context) error {
	chatID := ctx.EffectiveChat.Id
	StateMu.Lock()
	ActiveChatID = chatID
	// Delete old panel messages
	oldPanel := PanelMsgID
	oldList := ListMsgID
	oldProgress := ProgressMsgID
	PanelMsgID = 0
	ListMsgID = 0
	ProgressMsgID = 0
	StateMu.Unlock()

	if oldPanel > 0 {
		DeleteMessageIfPresent(bot, chatID, oldPanel)
	}
	if oldList > 0 {
		DeleteMessageIfPresent(bot, chatID, oldList)
	}
	if oldProgress > 0 {
		DeleteMessageIfPresent(bot, chatID, oldProgress)
	}

	SendControlPanel(bot, chatID)
	return nil
}

// handleNontext handles media file messages.
func handleNontext(bot *gotgbot.Bot, ctx *ext.Context) error {
	msg := ctx.EffectiveMessage
	chatID := ctx.EffectiveChat.Id
	StateMu.Lock()
	ActiveChatID = chatID
	StateMu.Unlock()

	mediaType, fileID := ClassifyMessage(msg)
	if mediaType == "" || fileID == "" {
		return nil
	}

	title := "Upload"
	if msg.Caption != "" {
		title = msg.Caption
	}

	go func() {
		_, _ = SendAndTrack(bot, chatID, fmt.Sprintf("⏬ Downloading %s...", mediaType), nil)

		mimeType := ""
		switch mediaType {
		case "voice":
			mimeType = "audio/ogg"
		case "audio":
			if msg.Audio != nil {
				mimeType = msg.Audio.MimeType
				if msg.Audio.Title != "" {
					title = msg.Audio.Title
				}
			}
		case "video":
			if msg.Video != nil {
				mimeType = msg.Video.MimeType
			}
		case "photo":
			mimeType = "image/jpeg"
		}

		localPath, err := DownloadMediaItem(bot, fileID, title, mimeType)
		if err != nil {
			_, _ = SendAndTrack(bot, chatID, fmt.Sprintf("❌ %s", err.Error()), nil)
			return
		}

		if mediaType == "video" {
			localPath = MaybeFaststartMP4(localPath)
		}

		if mediaType == "photo" {
			// Handle photo playback
			kodiPath := ResolveKodiMediaPath(localPath)
			ok := KA.PlayPicture(kodiPath)
			if ok {
				_, _ = SendAndTrack(bot, chatID, "🖼 Bild wird angezeigt!", nil)
			} else {
				_, _ = SendAndTrack(bot, chatID, "❌ Bild konnte nicht angezeigt werden.", nil)
			}
			return
		}

		item := RegisterTempMedia(localPath, title)
		itemTitle, _ := item["title"].(string)
		itemURL, _ := item["url"].(string)
		itemKind, _ := item["kind"].(string)

		qItem := queue.MakeItem(itemTitle, itemURL, itemKind, "", "")
		queue.QueueItem(qItem)

		queueLen := len(queue.Queue)
		_, _ = SendAndTrack(bot, chatID, fmt.Sprintf("✅ *%s* hinzugefügt (Queue: %d)", escapeMarkdown(itemTitle), queueLen), &gotgbot.SendMessageOpts{
			ParseMode: "Markdown",
		})

		if queueLen == 1 {
			queue.PlayIndex(0)
		}
	}()

	return nil
}

func escapeMarkdown(s string) string {
	replacer := strings.NewReplacer(
		"_", "\\_",
		"*", "\\*",
		"[", "\\[",
		"]", "\\]",
		"(", "\\(",
		")", "\\)",
		"~", "\\~",
		"`", "\\`",
		">", "\\>",
		"#", "\\#",
		"+", "\\+",
		"-", "\\-",
		"=", "\\=",
		"|", "\\|",
		"{", "\\{",
		"}", "\\}",
		".", "\\.",
		"!", "\\!",
	)
	return replacer.Replace(s)
}
