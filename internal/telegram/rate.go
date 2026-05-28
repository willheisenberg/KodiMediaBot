package telegram

import (
	"log"
	"sync"
	"time"

	"github.com/PaulSonOfLars/gotgbot/v2"
)

// ── Rate limiting constants ─────────────────────────────────────────

const (
	tgMinInterval       = 0.06  // 60ms between API calls
	tgDeleteMinInterval = 0.06
	tgMaxRetries        = 3
)

var (
	tgLastTS         float64
	tgDeleteLastTS   float64
	tgDynamicDelay   float64
	tgDynamicUntil   float64
	tgRateLock       sync.Mutex
	tgDeleteRateLock sync.Mutex
)

// TelegramRequest wraps a Telegram API call with rate limiting and retry.
// The caller passes a function that performs the actual API call.
func TelegramRequest(fn func() error) error {
	for attempt := 0; attempt < tgMaxRetries; attempt++ {
		tgRateLock.Lock()
		now := float64(time.Now().UnixMilli()) / 1000.0
		extra := 0.0
		if now < tgDynamicUntil {
			extra = tgDynamicDelay
		}
		wait := tgMinInterval + extra - (now - tgLastTS)
		if wait > 0 {
			time.Sleep(time.Duration(wait * float64(time.Second)))
		}
		err := fn()
		tgLastTS = float64(time.Now().UnixMilli()) / 1000.0
		tgRateLock.Unlock()

		if err == nil {
			return nil
		}
		// Check for rate limiting / timeout errors
		log.Printf("WARN Telegram request error (attempt %d/%d): %v", attempt+1, tgMaxRetries, err)
		time.Sleep(1500 * time.Millisecond)
	}
	// Final attempt
	tgRateLock.Lock()
	now := float64(time.Now().UnixMilli()) / 1000.0
	extra := 0.0
	if now < tgDynamicUntil {
		extra = tgDynamicDelay
	}
	wait := tgMinInterval + extra - (now - tgLastTS)
	if wait > 0 {
		time.Sleep(time.Duration(wait * float64(time.Second)))
	}
	err := fn()
	tgLastTS = float64(time.Now().UnixMilli()) / 1000.0
	tgRateLock.Unlock()
	return err
}

// TelegramRequestDelete wraps a Telegram delete API call with separate rate limiting.
func TelegramRequestDelete(fn func() error) error {
	for attempt := 0; attempt < tgMaxRetries; attempt++ {
		tgDeleteRateLock.Lock()
		now := float64(time.Now().UnixMilli()) / 1000.0
		extra := 0.0
		if now < tgDynamicUntil {
			extra = tgDynamicDelay
		}
		wait := tgDeleteMinInterval + extra - (now - tgDeleteLastTS)
		if wait > 0 {
			time.Sleep(time.Duration(wait * float64(time.Second)))
		}
		err := fn()
		tgDeleteLastTS = float64(time.Now().UnixMilli()) / 1000.0
		tgDeleteRateLock.Unlock()

		if err == nil {
			return nil
		}
		log.Printf("WARN Telegram delete error (attempt %d/%d): %v", attempt+1, tgMaxRetries, err)
		time.Sleep(1500 * time.Millisecond)
	}
	// Final attempt
	tgDeleteRateLock.Lock()
	now := float64(time.Now().UnixMilli()) / 1000.0
	extra := 0.0
	if now < tgDynamicUntil {
		extra = tgDynamicDelay
	}
	wait := tgDeleteMinInterval + extra - (now - tgDeleteLastTS)
	if wait > 0 {
		time.Sleep(time.Duration(wait * float64(time.Second)))
	}
	err := fn()
	tgDeleteLastTS = float64(time.Now().UnixMilli()) / 1000.0
	tgDeleteRateLock.Unlock()
	return err
}

// SendAndTrack sends a message and tracks its ID.
func SendAndTrack(bot *gotgbot.Bot, chatID int64, text string, opts *gotgbot.SendMessageOpts) (*gotgbot.Message, error) {
	if opts == nil {
		opts = &gotgbot.SendMessageOpts{}
	}
	if opts.LinkPreviewOptions == nil {
		opts.LinkPreviewOptions = &gotgbot.LinkPreviewOptions{IsDisabled: true}
	}

	var msg *gotgbot.Message
	err := TelegramRequest(func() error {
		var e error
		msg, e = bot.SendMessage(chatID, text, opts)
		return e
	})
	if err != nil {
		return nil, err
	}
	if msg != nil {
		StateMu.Lock()
		PrevBotID = LastBotID
		LastBotID = msg.MessageId
		ActiveChatID = chatID
		StateMu.Unlock()
		log.Printf("DEBUG Bot msg chat_id=%d message_id=%d", chatID, msg.MessageId)
	}
	return msg, nil
}

// DeleteMessageIfPresent silently deletes a message.
func DeleteMessageIfPresent(bot *gotgbot.Bot, chatID int64, messageID int64) {
	if messageID == 0 {
		return
	}
	_ = TelegramRequestDelete(func() error {
		_, err := bot.DeleteMessage(chatID, messageID, nil)
		return err
	})
}

// DeleteMessagesIfPresent silently deletes multiple messages.
func DeleteMessagesIfPresent(bot *gotgbot.Bot, chatID int64, messageIDs []int64) {
	for _, mid := range messageIDs {
		DeleteMessageIfPresent(bot, chatID, mid)
	}
}
