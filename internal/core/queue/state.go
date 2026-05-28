// Package queue provides the central playback queue and state management.
//
// This is the heart of the bot. It manages the playback queue, autoplay loop,
// SoundCloud/YouTube resolving, and WebSocket event handling.
package queue

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"os/exec"
	"regexp"
	"strings"
	"sync"
	"time"

	KA "github.com/willheisenberg/KodiMediaBot/internal/core/kodiapi"
)

// ── Queue and playback state ────────────────────────────────────────

var (
	Mu   sync.Mutex
	Queue []map[string]interface{}

	CurrentIndex  *int
	DisplayIndex  *int
	NextIndex     int
	AutoplayEnabled = true
	RepeatMode      = "off"
	ExternalPlayback bool
	BotExpectingWS   int

	// Radio unexpected stop tracking
	LastPlayedRadio   map[string]string // {"url": ..., "title": ...}
	ExpectedStop      bool
	OnUnexpectedRadioStop func(url, title string) // async callback
	CancelReconnectCB     func()

	// Optimistic display window
	PlayIndexTS               float64
	PlayIndexOptimisticWindow = 8.0

	// Progress tracking
	LastProgressTS    float64
	LastProgressTime  map[string]interface{}
	LastProgressTotal map[string]interface{}
	LastProgressIndex *int

	// Resume
	ResumeAttempts      = make(map[int]int)
	ResumeMaxAttempts   = 8
	ResumeMinRemainingSec = 10
	ResumeSeekWaitSec   = 20
	ResumeStaleProgressSec float64 = 12

	ListDirty bool

	scheduleNowPlayingRefresh func()

	// YouTube title cache
	ytTitleCache     = make(map[string]ytCacheEntry)
	ytTitleCacheTTL  = 3600.0
	ytTitleCacheMu   sync.Mutex

	// SoundCloud regexes
	SCDisplayRE = regexp.MustCompile(`^https?://(www\.)?soundcloud\.com/([^/]+)/([^/?#]+)`)
	SCTrackRE   = regexp.MustCompile(`^https?://(www\.)?soundcloud\.com/[^/]+/[^/?#]+`)
	SCSetRE     = regexp.MustCompile(`^https?://(www\.)?soundcloud\.com/[^/]+/sets/[^/?#]+`)
	SCBaseRE    = regexp.MustCompile(`^https?://(www\.)?soundcloud\.com/`)
	SCShortRE   = regexp.MustCompile(`^https?://on\.soundcloud\.com/[A-Za-z0-9]+`)
	SCHTMLRE    = regexp.MustCompile(`https?://soundcloud\.com/[^\s"'<>]+`)

	SCPluginStartTimeoutS = 4.0
	SCPluginStartPollS    = 0.25

	// Cleanup callback (set by telegram media)
	CleanupActiveImageSession func()
	// Autoplay
	autoplayThreadStarted bool
)

type ytCacheEntry struct {
	Title string
	TS    float64
}

var httpClient = &http.Client{Timeout: 10 * time.Second}

// SetUICallbacks registers the UI refresh callback.
func SetUICallbacks(scheduleRefresh func(), onUnexpectedStop func(url, title string), cancelReconnect func()) {
	scheduleNowPlayingRefresh = scheduleRefresh
	if onUnexpectedStop != nil {
		OnUnexpectedRadioStop = onUnexpectedStop
	}
	if cancelReconnect != nil {
		CancelReconnectCB = cancelReconnect
	}
}

// SetCleanupCallback sets the image session cleanup callback.
func SetCleanupCallback(cb func()) {
	CleanupActiveImageSession = cb
}

func ScheduleNowPlayingRefresh() {
	if scheduleNowPlayingRefresh != nil {
		scheduleNowPlayingRefresh()
	}
}

// ── YouTube title cache ─────────────────────────────────────────────

func GetCachedYoutubeTitle(vid string) string {
	now := KA.Now()
	ytTitleCacheMu.Lock()
	defer ytTitleCacheMu.Unlock()
	hit, ok := ytTitleCache[vid]
	if !ok {
		return ""
	}
	if now-hit.TS > ytTitleCacheTTL {
		delete(ytTitleCache, vid)
		return ""
	}
	return hit.Title
}

func CacheYoutubeTitle(vid, title string) {
	if vid == "" || title == "" {
		return
	}
	ytTitleCacheMu.Lock()
	ytTitleCache[vid] = ytCacheEntry{Title: title, TS: KA.Now()}
	ytTitleCacheMu.Unlock()
}

// ── Radio state ─────────────────────────────────────────────────────

func SetLastPlayedRadio(url, title string) {
	Mu.Lock()
	LastPlayedRadio = map[string]string{"url": url, "title": title}
	ExpectedStop = false
	Mu.Unlock()
	log.Printf("INFO Radio state registered: '%s' (%s)", title, url)
}

func ClearRadioReconnectState() {
	if CancelReconnectCB != nil {
		CancelReconnectCB()
	}
	Mu.Lock()
	LastPlayedRadio = nil
	ExpectedStop = true
	Mu.Unlock()
	log.Printf("INFO Radio reconnect state cleared")
}

// ── Thread-safe WS expectation helpers ──────────────────────────────

func SetExpectingWS(n int) {
	Mu.Lock()
	BotExpectingWS = n
	Mu.Unlock()
}

func DecrementExpectingWS() int {
	Mu.Lock()
	if BotExpectingWS > 0 {
		BotExpectingWS--
	}
	val := BotExpectingWS
	Mu.Unlock()
	return val
}

func GetExpectingWS() int {
	Mu.Lock()
	val := BotExpectingWS
	Mu.Unlock()
	return val
}

// ── WS callback handlers ───────────────────────────────────────────

func HandleWSPlay(item map[string]interface{}, itemParams map[string]interface{}) {
	Mu.Lock()
	wasExpecting := BotExpectingWS > 0
	if wasExpecting {
		BotExpectingWS--
	}
	Mu.Unlock()
	if wasExpecting {
		log.Printf("DEBUG WS play expected, remaining=%d", BotExpectingWS)
		return
	}
	// External play – check mismatch
	Mu.Lock()
	var qitem map[string]interface{}
	if DisplayIndex != nil && *DisplayIndex >= 0 && *DisplayIndex < len(Queue) {
		qitem = Queue[*DisplayIndex]
	}
	Mu.Unlock()
	if !KA.KodiItemMatchesQueue(item, qitem) {
		log.Printf("INFO WS mismatch clear_bot_playback_state")
		ClearBotPlaybackState()
		ScheduleNowPlayingRefresh()
	}
}

func HandleWSPause() {
	ScheduleNowPlayingRefresh()
}

func HandleWSResume() {
	ScheduleNowPlayingRefresh()
}

func HandleWSStop(itemParams, playerParams map[string]interface{}) {
	ScheduleNowPlayingRefresh()

	playerID := -1
	if playerParams != nil {
		if v, ok := playerParams["playerid"]; ok {
			switch n := v.(type) {
			case float64:
				playerID = int(n)
			case int:
				playerID = n
			}
		}
	}
	stoppedType := ""
	if itemParams != nil {
		if v, ok := itemParams["type"].(string); ok {
			stoppedType = v
		}
	}

	isAudioStop := true
	if playerID >= 0 && playerID != 0 {
		isAudioStop = false
	} else if stoppedType == "picture" {
		isAudioStop = false
	}

	Mu.Lock()
	unexpected := !ExpectedStop && isAudioStop
	radioInfo := LastPlayedRadio
	if isAudioStop {
		ExpectedStop = true
	}
	Mu.Unlock()

	if unexpected && radioInfo != nil && OnUnexpectedRadioStop != nil {
		log.Printf("INFO Unexpected stop detected for radio: %v. Triggering reconnect...", radioInfo)
		go OnUnexpectedRadioStop(radioInfo["url"], radioInfo["title"])
	}
}

func HandleWSPlaybackRefresh() {
	SchedulePlaybackRefresh()
}

// RegisterWSCallbacks registers event handlers with kodiapi's WS callback system.
func RegisterWSCallbacks() {
	KA.SetWSHandlers(HandleWSPlay, HandleWSPause, HandleWSResume, HandleWSStop, HandleWSPlaybackRefresh)
}

// ── State helpers ───────────────────────────────────────────────────

func MarkListDirty() {
	ListDirty = true
}

func ClearBotPlaybackState() {
	Mu.Lock()
	AutoplayEnabled = false
	CurrentIndex = nil
	DisplayIndex = nil
	ExternalPlayback = true
	ResumeAttempts = make(map[int]int)
	Mu.Unlock()
	MarkListDirty()
}

func SchedulePlaybackRefresh() {
	MarkListDirty()
	ScheduleNowPlayingRefresh()
}

// ── Seek ────────────────────────────────────────────────────────────

func SeekRelativeSeconds(deltaSec int) bool {
	pid := KA.GetActivePlayerID()
	if pid < 0 {
		return false
	}
	res := KA.KodiCall("Player.GetProperties", map[string]interface{}{
		"playerid":   pid,
		"properties": []string{"time", "totaltime", "canseek"},
	})
	result, _ := res["result"].(map[string]interface{})
	if result == nil {
		return false
	}
	canseek, _ := result["canseek"].(bool)
	if !canseek {
		return false
	}
	curMap, _ := result["time"].(map[string]interface{})
	totalMap, _ := result["totaltime"].(map[string]interface{})
	curSec := KA.KodiTimeSeconds(curMap)
	totalSec := KA.KodiTimeSeconds(totalMap)
	if curSec < 0 {
		return false
	}
	if totalSec >= 0 && deltaSec > 0 && curSec+deltaSec >= totalSec {
		return SkipQueue()
	}
	if totalSec < 0 {
		totalSec = max(curSec+1, 1)
	}
	newSec := max(0, min(curSec+deltaSec, totalSec))
	h := newSec / 3600
	m := (newSec % 3600) / 60
	s := newSec % 60
	KA.KodiCall("Player.Seek", map[string]interface{}{
		"playerid": pid,
		"value":    map[string]interface{}{"time": map[string]interface{}{"hours": h, "minutes": m, "seconds": s}},
	})
	return true
}

func SeekPercent(percent int) bool {
	pid := KA.GetActivePlayerID()
	if pid < 0 {
		return false
	}
	res := KA.KodiCall("Player.GetProperties", map[string]interface{}{
		"playerid":   pid,
		"properties": []string{"canseek"},
	})
	result, _ := res["result"].(map[string]interface{})
	if result == nil {
		return false
	}
	canseek, _ := result["canseek"].(bool)
	if !canseek {
		return false
	}
	KA.KodiCall("Player.Seek", map[string]interface{}{
		"playerid": pid,
		"value":    map[string]interface{}{"percentage": percent},
	})
	return true
}

func SeekWhenPlayerReady(t map[string]interface{}, context string) {
	go func() {
		end := time.Now().Add(time.Duration(ResumeSeekWaitSec) * time.Second)
		for time.Now().Before(end) {
			players := KA.GetActivePlayers()
			if len(players) > 0 {
				pid := KA.PickPlayerID(players)
				if pid >= 0 {
					KA.KodiCall("Player.Seek", map[string]interface{}{
						"playerid": pid,
						"value":    map[string]interface{}{"time": t},
					})
				}
				return
			}
			time.Sleep(300 * time.Millisecond)
		}
		log.Printf("WARN Resume seek gave up: no playerid ctx=%s", context)
	}()
}

// ── Item constructors ───────────────────────────────────────────────

func MakeItem(title, urlStr, kind string, link string, resolver string) map[string]interface{} {
	return map[string]interface{}{
		"title":    title,
		"url":      urlStr,
		"kind":     kind,
		"link":     link,
		"resolver": resolver,
	}
}

func FetchYoutubeTitle(vid string) string {
	cached := GetCachedYoutubeTitle(vid)
	if cached != "" {
		return cached
	}
	ytURL := "https://youtu.be/" + vid
	// Try oembed first
	resp, err := httpClient.Get(fmt.Sprintf("https://www.youtube.com/oembed?url=%s&format=json", url.QueryEscape(ytURL)))
	if err == nil {
		defer resp.Body.Close()
		if resp.StatusCode == 200 {
			var data map[string]interface{}
			if json.NewDecoder(resp.Body).Decode(&data) == nil {
				author, _ := data["author_name"].(string)
				title, _ := data["title"].(string)
				if author != "" && title != "" {
					out := author + " - " + title
					CacheYoutubeTitle(vid, out)
					return out
				}
				if title != "" {
					CacheYoutubeTitle(vid, title)
					return title
				}
			}
		}
	}
	// Fallback: yt-dlp
	cmd := exec.Command("yt-dlp", "--skip-download", "--print", "%(uploader)s\t%(title)s", "--no-warnings", ytURL)
	out, err := cmd.CombinedOutput()
	if err == nil {
		lines := strings.Split(strings.TrimSpace(string(out)), "\n")
		if len(lines) > 0 {
			parts := strings.SplitN(strings.TrimSpace(lines[0]), "\t", 2)
			if len(parts) == 2 && parts[0] != "" && parts[1] != "" {
				result := parts[0] + " - " + parts[1]
				CacheYoutubeTitle(vid, result)
				return result
			}
			if parts[len(parts)-1] != "" {
				CacheYoutubeTitle(vid, parts[len(parts)-1])
				return parts[len(parts)-1]
			}
		}
	}
	return ytURL
}

func MakeYoutube(vid string, title string) map[string]interface{} {
	link := "https://youtu.be/" + vid
	if title == "" {
		title = link
	}
	return MakeItem(title, "plugin://plugin.video.youtube/play/?video_id="+vid, "video", link, "")
}

func SoundcloudDisplayTitle(cleanURL string) string {
	m := SCDisplayRE.FindStringSubmatch(cleanURL)
	if m == nil {
		return cleanURL
	}
	artist, _ := url.QueryUnescape(m[2])
	artist = strings.ReplaceAll(artist, "-", " ")
	track, _ := url.QueryUnescape(m[3])
	track = strings.ReplaceAll(track, "-", " ")
	return strings.TrimSpace(artist + " - " + track)
}

func MakeSoundcloud(scURL string) map[string]interface{} {
	clean := regexp.MustCompile(`\?.*$`).ReplaceAllString(scURL, "")
	return MakeItem(
		SoundcloudDisplayTitle(clean),
		"plugin://plugin.audio.soundcloud/play/?url="+clean,
		"audio",
		clean,
		"soundcloud",
	)
}

// ── SoundCloud helpers ──────────────────────────────────────────────

func IsSCTrackURL(u string) bool {
	return SCTrackRE.MatchString(u) && !strings.Contains(u, "discover/sets")
}

func IsSCSetURL(u string) bool {
	return SCSetRE.MatchString(u) && !strings.Contains(u, "discover/sets")
}

func IsSoundcloudItem(item map[string]interface{}) bool {
	if item == nil {
		return false
	}
	if r, _ := item["resolver"].(string); r == "soundcloud" {
		return true
	}
	if link, _ := item["link"].(string); IsSCTrackURL(link) {
		return true
	}
	if u, _ := item["url"].(string); strings.HasPrefix(u, "plugin://plugin.audio.soundcloud/") {
		return true
	}
	return false
}

func ResolveSCShort(u string) string {
	client := &http.Client{
		Timeout: 8 * time.Second,
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			return nil
		},
	}
	req, _ := http.NewRequest("GET", u, nil)
	req.Header.Set("User-Agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")
	resp, err := client.Do(req)
	if err != nil {
		log.Printf("WARN SC short resolve error=%v", err)
		return ""
	}
	defer resp.Body.Close()
	finalURL := resp.Request.URL.String()
	if SCBaseRE.MatchString(finalURL) && !strings.Contains(finalURL, "discover/sets") {
		return finalURL
	}
	return ""
}

func KodiAddToPlaylist(fileURL string, playlistID int) {
	KA.KodiCall("Playlist.Add", map[string]interface{}{
		"playlistid": playlistID,
		"item":       map[string]interface{}{"file": fileURL},
	})
}

func ExpandSoundcloudSet(scURL string) []string {
	clean := regexp.MustCompile(`\?.*$`).ReplaceAllString(scURL, "")
	cmd := exec.Command("yt-dlp", "--flat-playlist", "--print", "%(webpage_url)s", "--no-warnings", clean)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return nil
	}
	var urls []string
	for _, line := range strings.Split(strings.TrimSpace(string(out)), "\n") {
		line = strings.TrimSpace(line)
		if line != "" && strings.HasPrefix(line, "http") && IsSCTrackURL(line) {
			urls = append(urls, line)
		}
	}
	return urls
}

func QueueSoundcloudSet(scURL string) int {
	tracks := ExpandSoundcloudSet(scURL)
	for _, t := range tracks {
		QueueItem(MakeSoundcloud(t))
	}
	MarkListDirty()
	return len(tracks)
}

func ExpandPlaylist(pid string) []string {
	ytURL := "https://www.youtube.com/playlist?list=" + pid
	cmd := exec.Command("yt-dlp", "--flat-playlist", "--print", "id", "--no-warnings", ytURL)
	out, err := cmd.CombinedOutput()
	if err != nil {
		log.Printf("WARN yt-dlp playlist expansion failed: %v", err)
		return nil
	}
	var vids []string
	for _, line := range strings.Split(strings.TrimSpace(string(out)), "\n") {
		line = strings.TrimSpace(line)
		if line != "" {
			vids = append(vids, line)
		}
	}
	return vids
}

// ── Queue operations ────────────────────────────────────────────────

func QueueItem(item map[string]interface{}) {
	Mu.Lock()
	Queue = append(Queue, item)
	Mu.Unlock()
	MarkListDirty()
}

func QueueVideo(vid string, title string) {
	Mu.Lock()
	Queue = append(Queue, MakeYoutube(vid, title))
	Mu.Unlock()
	MarkListDirty()
}

func QueuePlaylist(pid string) {
	for _, vid := range ExpandPlaylist(pid) {
		QueueVideo(vid, "")
	}
	MarkListDirty()
}

func ClearQueue() {
	Mu.Lock()
	Queue = nil
	CurrentIndex = nil
	DisplayIndex = nil
	NextIndex = 0
	LastProgressTS = 0
	LastProgressTime = nil
	LastProgressTotal = nil
	LastProgressIndex = nil
	ExternalPlayback = false
	BotExpectingWS = 0
	ResumeAttempts = make(map[int]int)
	Mu.Unlock()
	MarkListDirty()
}

func DeleteIndex(i int) (bool, string) {
	Mu.Lock()
	defer Mu.Unlock()
	if i < 0 || i >= len(Queue) {
		return false, "Invalid index."
	}
	if DisplayIndex != nil && i == *DisplayIndex {
		return false, "You cannot delete the currently playing title. Use /skip or /stop first."
	}

	Queue = append(Queue[:i], Queue[i+1:]...)

	if DisplayIndex != nil && i < *DisplayIndex {
		v := *DisplayIndex - 1
		DisplayIndex = &v
	}
	if CurrentIndex != nil && i < *CurrentIndex {
		v := *CurrentIndex - 1
		CurrentIndex = &v
	}
	if i < NextIndex {
		NextIndex--
	}

	MarkListDirty()
	return true, ""
}

// ── Play ────────────────────────────────────────────────────────────

func PlayItem(item map[string]interface{}, resumeTime map[string]interface{}) {
	if CancelReconnectCB != nil {
		CancelReconnectCB()
	}
	Mu.Lock()
	ExpectedStop = true
	LastPlayedRadio = nil
	Mu.Unlock()

	if CleanupActiveImageSession != nil {
		CleanupActiveImageSession()
	}
	KA.StopAllPlayers()
	KA.KodiClearAllPlaylists()

	kind, _ := item["kind"].(string)
	if kind == "" {
		kind = "video"
	}
	SetExpectingWS(2)

	log.Printf("INFO play_item start kind=%s title=%s url=%s", kind, item["title"], item["url"])

	itemURL, _ := item["url"].(string)

	if kind == "audio" && IsSoundcloudItem(item) {
		sourceLink, _ := item["link"].(string)
		if sourceLink == "" {
			sourceLink = KA.ExtractSoundcloudURL(itemURL)
		}
		if sourceLink == "" {
			sourceLink = itemURL
		}
		KA.MaybeCacheSoundcloudURL(sourceLink)
		log.Printf("INFO play_item soundcloud open_mode=plugin_direct title=%s source=%s", item["title"], sourceLink)
		KA.KodiCall("Player.Open", map[string]interface{}{"item": map[string]interface{}{"file": itemURL}})
		ScheduleSoundcloudPluginFallback(item, sourceLink, resumeTime)
		SchedulePlaybackRefresh()
		if resumeTime != nil {
			SeekWhenPlayerReady(resumeTime, "soundcloud")
		}
	} else if kind == "audio" {
		KodiAddToPlaylist(itemURL, 0)
		KA.KodiCall("Player.Open", map[string]interface{}{"item": map[string]interface{}{"playlistid": 0, "position": 0}})
		SchedulePlaybackRefresh()
		if resumeTime != nil {
			SeekWhenPlayerReady(resumeTime, "audio")
		}
	} else {
		KodiAddToPlaylist(itemURL, 1)
		KA.KodiCall("Player.Open", map[string]interface{}{"item": map[string]interface{}{"playlistid": 1}})
		SchedulePlaybackRefresh()
		if resumeTime != nil {
			SeekWhenPlayerReady(resumeTime, "video")
		}
	}
}

func ResumeItemAtTime(item map[string]interface{}, t map[string]interface{}) {
	if t == nil {
		PlayItem(item, nil)
		return
	}
	PlayItem(item, t)
}

func SoundcloudPlaybackStarted(sourceLink string) bool {
	pid := KA.GetActivePlayerID()
	if pid < 0 {
		return false
	}
	res := KA.KodiCall("Player.GetItem", map[string]interface{}{
		"playerid":   pid,
		"properties": []string{"file", "title", "artist"},
	})
	result, _ := res["result"].(map[string]interface{})
	item, _ := result["item"].(map[string]interface{})
	if item == nil {
		return false
	}
	fileURL, _ := item["file"].(string)
	if fileURL == "" {
		return false
	}
	scURL := KA.ExtractSoundcloudURL(fileURL)
	if scURL != "" && sourceLink != "" && scURL == sourceLink {
		return true
	}
	return KA.IsSoundcloudStreamURL(fileURL)
}

func ScheduleSoundcloudPluginFallback(item map[string]interface{}, sourceLink string, resumeTime map[string]interface{}) {
	go func() {
		end := time.Now().Add(time.Duration(SCPluginStartTimeoutS * float64(time.Second)))
		for time.Now().Before(end) {
			if SoundcloudPlaybackStarted(sourceLink) {
				return
			}
			time.Sleep(time.Duration(SCPluginStartPollS * float64(time.Second)))
		}
		log.Printf("WARN play_item soundcloud plugin_direct stalled title=%s source=%s", item["title"], sourceLink)
	}()
}

func HardStopAndClear() {
	active := KA.GetActivePlayers()
	hasPicture := false
	hasAudio := false
	for _, p := range active {
		if t, _ := p["type"].(string); t == "picture" {
			hasPicture = true
		}
		if t, _ := p["type"].(string); t == "audio" {
			hasAudio = true
		}
	}

	if hasPicture && hasAudio {
		for _, p := range active {
			if t, _ := p["type"].(string); t == "picture" {
				if pid, ok := p["playerid"]; ok {
					pidInt := 0
					switch v := pid.(type) {
					case float64:
						pidInt = int(v)
					case int:
						pidInt = v
					}
					KA.KodiCall("Player.Stop", map[string]interface{}{"playerid": pidInt})
				}
			}
		}
		if CleanupActiveImageSession != nil {
			CleanupActiveImageSession()
		}
		SchedulePlaybackRefresh()
		return
	}

	if CancelReconnectCB != nil {
		CancelReconnectCB()
	}
	if CleanupActiveImageSession != nil {
		CleanupActiveImageSession()
	}
	KA.StopAllPlayers()
	KA.KodiClearAllPlaylists()

	Mu.Lock()
	AutoplayEnabled = false
	CurrentIndex = nil
	DisplayIndex = nil
	NextIndex = 0
	LastProgressTS = 0
	LastProgressTime = nil
	LastProgressTotal = nil
	LastProgressIndex = nil
	ExternalPlayback = false
	BotExpectingWS = 0
	ResumeAttempts = make(map[int]int)
	ExpectedStop = true
	LastPlayedRadio = nil
	Mu.Unlock()
	PlayIndexTS = 0
	SchedulePlaybackRefresh()
}

func SkipQueue() bool {
	Mu.Lock()
	if len(Queue) == 0 {
		AutoplayEnabled = false
		CurrentIndex = nil
		DisplayIndex = nil
		NextIndex = 0
		Mu.Unlock()
		KA.StopPlayerAndClearPlaylists()
		return false
	}

	var i int
	if RepeatMode == "one" && DisplayIndex != nil {
		i = *DisplayIndex
	} else if DisplayIndex == nil {
		i = 0
	} else {
		i = *DisplayIndex + 1
	}

	if i >= len(Queue) {
		if RepeatMode == "all" {
			i = 0
		} else {
			AutoplayEnabled = false
			CurrentIndex = nil
			DisplayIndex = nil
			NextIndex = 0
			Mu.Unlock()
			KA.StopPlayerAndClearPlaylists()
			return false
		}
	}
	Mu.Unlock()

	PlayIndex(i)
	return true
}

func BackQueue() bool {
	Mu.Lock()
	if len(Queue) == 0 {
		Mu.Unlock()
		return false
	}
	var i int
	if RepeatMode == "one" && DisplayIndex != nil {
		i = *DisplayIndex
	} else if DisplayIndex == nil {
		if RepeatMode == "all" {
			i = len(Queue) - 1
		} else {
			i = 0
		}
	} else {
		i = *DisplayIndex - 1
		if i < 0 {
			if RepeatMode == "all" {
				i = len(Queue) - 1
			} else {
				i = 0
			}
		}
	}
	Mu.Unlock()

	PlayIndex(i)
	return true
}

func PlayIndex(i int) {
	Mu.Lock()
	if i < 0 || i >= len(Queue) {
		Mu.Unlock()
		return
	}
	CurrentIndex = &i
	DisplayIndex = &i
	NextIndex = i + 1
	AutoplayEnabled = true
	ExternalPlayback = false
	item := Queue[i]
	ResumeAttempts = make(map[int]int)
	Mu.Unlock()

	PlayIndexTS = KA.Now()
	MarkListDirty()
	PlayItem(item, nil)
}

func IsRequestedTrackAlreadyPlaying(i int) bool {
	Mu.Lock()
	if DisplayIndex == nil || i != *DisplayIndex {
		Mu.Unlock()
		return false
	}
	Mu.Unlock()
	if GetExpectingWS() > 0 {
		return true
	}
	return KA.WSPlaying
}

// ── Autoplay loop ───────────────────────────────────────────────────

func AutoplayLoop() {
	for {
		func() {
			defer func() {
				if r := recover(); r != nil {
					log.Printf("ERROR Autoplay panic: %v", r)
				}
			}()

			now := KA.Now()
			playbackState := KA.WSState

			if !KA.WSConnected {
				time.Sleep(500 * time.Millisecond)
				return
			}
			if !AutoplayEnabled {
				time.Sleep(500 * time.Millisecond)
				return
			}
			if GetExpectingWS() > 0 {
				time.Sleep(200 * time.Millisecond)
				return
			}

			// Stale progress fallback
			if playbackState != "stopped" && DisplayIndex != nil {
				var freshnessTS float64
				if LastProgressTS > 0 {
					freshnessTS = LastProgressTS
				}
				if KA.WSLastEventTS > freshnessTS {
					freshnessTS = KA.WSLastEventTS
				}
				stale := freshnessTS > 0 && (now-freshnessTS) >= ResumeStaleProgressSec
				if stale {
					players := KA.GetActivePlayers()
					if len(players) == 0 {
						log.Printf("INFO Resume inferred stop state=%s idx=%d stale_for=%.1fs",
							playbackState, *DisplayIndex, now-freshnessTS)
						playbackState = "stopped"
					}
				}
			}

			if playbackState == "playing" || playbackState == "paused" {
				time.Sleep(500 * time.Millisecond)
				return
			}

			// Resume logic
			if playbackState == "stopped" && DisplayIndex != nil {
				Mu.Lock()
				dispIdx := *DisplayIndex
				hasProgress := LastProgressIndex != nil && *LastProgressIndex == dispIdx && LastProgressTime != nil
				var remaining *int
				if hasProgress && LastProgressTotal != nil {
					curSec := KA.KodiTimeSeconds(LastProgressTime)
					totalSec := KA.KodiTimeSeconds(LastProgressTotal)
					if curSec >= 0 && totalSec >= 0 {
						r := max(totalSec-curSec, 0)
						remaining = &r
					}
				}
				Mu.Unlock()

				if hasProgress {
					if remaining != nil && *remaining <= ResumeMinRemainingSec {
						// Track finished naturally
						Mu.Lock()
						if RepeatMode == "one" {
							NextIndex = dispIdx
						}
						CurrentIndex = nil
						DisplayIndex = nil
						LastProgressTime = nil
						LastProgressIndex = nil
						LastProgressTotal = nil
						Mu.Unlock()
						MarkListDirty()
						time.Sleep(300 * time.Millisecond)
						return
					}

					attempts := ResumeAttempts[dispIdx]
					if attempts < ResumeMaxAttempts {
						ResumeAttempts[dispIdx] = attempts + 1
						log.Printf("INFO Resume attempt idx=%d attempt=%d remaining=%v", dispIdx, attempts+1, remaining)
						Mu.Lock()
						var item map[string]interface{}
						if dispIdx < len(Queue) {
							item = Queue[dispIdx]
						}
						progressTime := LastProgressTime
						Mu.Unlock()
						if item != nil {
							ResumeItemAtTime(item, progressTime)
							time.Sleep(300 * time.Millisecond)
							return
						}
					} else {
						Mu.Lock()
						CurrentIndex = nil
						DisplayIndex = nil
						Mu.Unlock()
						MarkListDirty()
					}
				}
			}

			if playbackState == "stopped" {
				Mu.Lock()
				if CurrentIndex != nil {
					if RepeatMode == "one" {
						NextIndex = *CurrentIndex
					}
					CurrentIndex = nil
					Mu.Unlock()
					time.Sleep(300 * time.Millisecond)
					return
				}

				if NextIndex < len(Queue) {
					idx := NextIndex
					CurrentIndex = &idx
					DisplayIndex = &idx
					item := Queue[idx]
					NextIndex++
					Mu.Unlock()
					MarkListDirty()
					PlayItem(item, nil)
				} else {
					if RepeatMode == "all" {
						NextIndex = 0
						CurrentIndex = nil
						DisplayIndex = nil
					} else {
						AutoplayEnabled = false
						CurrentIndex = nil
						DisplayIndex = nil
					}
					Mu.Unlock()
				}
			}
		}()

		time.Sleep(1 * time.Second)
	}
}

func StartAutoplayThread() {
	if autoplayThreadStarted {
		return
	}
	autoplayThreadStarted = true
	go AutoplayLoop()
}

// ── Helpers ─────────────────────────────────────────────────────────

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func max(a, b int) int {
	if a > b {
		return a
	}
	return b
}
