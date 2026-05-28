// Package kodiapi provides the Kodi JSON-RPC client, WebSocket listener,
// HiFi/CEC/Denon device control, metadata helpers, and library access.
package kodiapi

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os/exec"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/willheisenberg/KodiMediaBot/internal/config"
)

// ── Compiled regexes (mirrors kodi_api.py top-level patterns) ───────

var (
	YTRE       = regexp.MustCompile(`(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_\-]{11})`)
	PLRE       = regexp.MustCompile(`(?:[?&]list=)([A-Za-z0-9_\-]+)`)
	SCRE       = regexp.MustCompile(`https?://(www\.)?soundcloud\.com/[^/]+/[^/?#]+`)
	SCSetRE    = regexp.MustCompile(`https?://(www\.)?soundcloud\.com/[^/]+/sets/[^/?#]+`)
	SCShortRE  = regexp.MustCompile(`https?://on\.soundcloud\.com/[A-Za-z0-9]+`)
	YTIDRegex  = regexp.MustCompile(`^[A-Za-z0-9_\-]{11}$`)
	IMDBIDRegex = regexp.MustCompile(`^tt\d+$`)
	PlayMediaRE = regexp.MustCompile(`(?i)^\s*PlayMedia\((.*)\)\s*$`)
)

const (
	CECCmdVolUp   = "0x41"
	CECCmdVolDown = "0x42"
)

// ── Global mutable state (protected by mu) ──────────────────────────

var (
	mu sync.Mutex

	WSConnected    bool
	WSPlaying      bool
	WSLastEventTS  float64
	WSState        = "unknown"

	LastWSItem       = make(map[string]interface{})
	LastWSPlayerID   *int
	LastWSYTID       string
	LastWSPlayingFile string
	LastWSSCURL      string
	LastWSSCTrackID  string
	LastWSSCLookupTS float64
	LastWSSCProbeTS  float64
	LastWSSCProbeActive bool

	SCClientIDCache  string
	SCClientIDTS     float64
	SCPermalinkCache = make(map[string]cachedString)
	SCPermalinkTTL   = 3600.0
	RadioStreamMapCache *map[string]string
	RadioM3UMapCache    *map[string]string
	ICYTitleCache    = make(map[string]cachedString)
	YTSearchCache    = make(map[string]cachedString)
	SCSearchCache    = make(map[string]cachedString)
	lastKodiErrorLogTS float64
)

type cachedString struct {
	Value string
	TS    float64
}

var PlayerGetItemProperties = []string{"title", "artist", "file"}

// ── WebSocket callback registry ─────────────────────────────────────

var (
	wsOnPlay            func(item map[string]interface{}, itemParams map[string]interface{})
	wsOnPause           func()
	wsOnResume          func()
	wsOnStop            func(itemParams, playerParams map[string]interface{})
	wsOnPlaybackRefresh func()
)

// SetWSHandlers registers callbacks for WebSocket events.
func SetWSHandlers(
	onPlay func(item map[string]interface{}, itemParams map[string]interface{}),
	onPause func(),
	onResume func(),
	onStop func(itemParams, playerParams map[string]interface{}),
	onPlaybackRefresh func(),
) {
	wsOnPlay = onPlay
	wsOnPause = onPause
	wsOnResume = onResume
	wsOnStop = onStop
	wsOnPlaybackRefresh = onPlaybackRefresh
}

// httpClient is a shared HTTP client with connection pooling.
var httpClient = &http.Client{
	Timeout: 5 * time.Second,
	Transport: &http.Transport{
		MaxIdleConns:        20,
		MaxIdleConnsPerHost: 10,
		IdleConnTimeout:     90 * time.Second,
	},
}

// KodiCall sends a JSON-RPC request to Kodi and returns the response.
func KodiCall(method string, params map[string]interface{}) map[string]interface{} {
	cfg := config.Get()
	payload := map[string]interface{}{
		"jsonrpc": "2.0",
		"method":  method,
		"id":      1,
	}
	if params != nil {
		payload["params"] = params
	}

	body, err := json.Marshal(payload)
	if err != nil {
		return errorResult(method, err)
	}

	req, err := http.NewRequest("POST", cfg.KodiURL(), bytes.NewReader(body))
	if err != nil {
		return errorResult(method, err)
	}
	req.Header.Set("Content-Type", "application/json")
	user, pass := cfg.KodiAuth()
	req.SetBasicAuth(user, pass)

	resp, err := httpClient.Do(req)
	if err != nil {
		now := float64(time.Now().Unix())
		mu.Lock()
		shouldLog := now-lastKodiErrorLogTS >= cfg.KodiErrorLogInterval
		if shouldLog {
			lastKodiErrorLogTS = now
		}
		mu.Unlock()
		if shouldLog {
			log.Printf("ERROR Kodi call failed: method=%s host=%s port=%d err=%v", method, cfg.KodiHost, cfg.KodiPort, err)
		}
		return errorResult(method, err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return errorResult(method, err)
	}

	var result map[string]interface{}
	if err := json.Unmarshal(respBody, &result); err != nil {
		return errorResult(method, err)
	}
	return result
}

func errorResult(method string, err error) map[string]interface{} {
	return map[string]interface{}{
		"error": map[string]interface{}{
			"message": err.Error(),
			"type":    fmt.Sprintf("%T", err),
			"method":  method,
		},
	}
}

// KodiCallWithProps calls Kodi with fallback on property errors.
func KodiCallWithProps(method, idKey string, idValue interface{}, properties []string) map[string]interface{} {
	props := make([]string, len(properties))
	copy(props, properties)
	for len(props) > 0 {
		res := KodiCall(method, map[string]interface{}{idKey: idValue, "properties": props})
		if _, hasErr := res["error"]; !hasErr {
			return res
		}
		props = props[:len(props)-1]
	}
	return KodiCall(method, map[string]interface{}{idKey: idValue, "properties": []string{}})
}

// ── Player helpers ──────────────────────────────────────────────────

// GetActivePlayers returns the list of active Kodi players.
func GetActivePlayers() []map[string]interface{} {
	res := KodiCall("Player.GetActivePlayers", nil)
	result, ok := res["result"]
	if !ok {
		return nil
	}
	arr, ok := result.([]interface{})
	if !ok {
		return nil
	}
	players := make([]map[string]interface{}, 0, len(arr))
	for _, p := range arr {
		if pm, ok := p.(map[string]interface{}); ok {
			players = append(players, pm)
		}
	}
	return players
}

// GetActivePlayer returns the first active Kodi player, or nil.
func GetActivePlayer() map[string]interface{} {
	players := GetActivePlayers()
	if len(players) == 0 {
		return nil
	}
	return players[0]
}

// GetActivePlayerID returns the active player id, or -1 if none.
func GetActivePlayerID() int {
	p := GetActivePlayer()
	if p == nil {
		return -1
	}
	return jsonInt(p, "playerid", -1)
}

// IsPicturePlayerActive returns true if any picture player is active.
func IsPicturePlayerActive() bool {
	for _, p := range GetActivePlayers() {
		if jsonStr(p, "type") == "picture" {
			return true
		}
	}
	return false
}

// WaitForPicturePlayerActive polls until picture player is active or timeout.
func WaitForPicturePlayerActive(timeoutS, intervalS float64) bool {
	deadline := time.Now().Add(time.Duration(timeoutS * float64(time.Second)))
	for time.Now().Before(deadline) {
		if IsPicturePlayerActive() {
			return true
		}
		time.Sleep(time.Duration(intervalS * float64(time.Second)))
	}
	return IsPicturePlayerActive()
}

// StopAllPlayers stops all active Kodi players.
func StopAllPlayers() {
	for _, p := range GetActivePlayers() {
		pid := jsonInt(p, "playerid", -1)
		if pid >= 0 {
			KodiCall("Player.Stop", map[string]interface{}{"playerid": pid})
		}
	}
}

// StopVideoPlayers stops only active video players.
func StopVideoPlayers() {
	for _, p := range GetActivePlayers() {
		if jsonStr(p, "type") == "video" {
			pid := jsonInt(p, "playerid", -1)
			if pid >= 0 {
				KodiCall("Player.Stop", map[string]interface{}{"playerid": pid})
			}
		}
	}
	KodiCall("Playlist.Clear", map[string]interface{}{"playlistid": 1})
}

// StopPlayerAndClearPlaylists stops playback and clears playlists.
func StopPlayerAndClearPlaylists() {
	StopAllPlayers()
	KodiClearAllPlaylists()
}

// KodiClearAllPlaylists clears both audio and video playlists.
func KodiClearAllPlaylists() {
	KodiCall("Playlist.Clear", map[string]interface{}{"playlistid": 0})
	KodiCall("Playlist.Clear", map[string]interface{}{"playlistid": 1})
}

// PickPlayerID picks the best player ID from a list of players.
func PickPlayerID(players []map[string]interface{}) int {
	if len(players) == 0 {
		return -1
	}
	for _, p := range players {
		if jsonStr(p, "type") == "video" {
			return jsonInt(p, "playerid", -1)
		}
	}
	return jsonInt(players[0], "playerid", -1)
}

// ── Favourites ──────────────────────────────────────────────────────

// GetPlayableFavourites returns Kodi favourites that can be opened as playable media.
func GetPlayableFavourites() []map[string]interface{} {
	attempts := []map[string]interface{}{
		{"type": "media", "properties": []string{"path", "windowparameter"}},
		{"type": "media"},
		{"properties": []string{"path", "windowparameter"}},
		nil,
	}
	var rawFavs []interface{}
	for _, params := range attempts {
		res := KodiCall("Favourites.GetFavourites", params)
		if _, hasErr := res["error"]; hasErr {
			continue
		}
		result := jsonMap(res, "result")
		if result == nil {
			continue
		}
		if favs, ok := result["favourites"].([]interface{}); ok {
			rawFavs = favs
			break
		}
	}

	var out []map[string]interface{}
	for _, f := range rawFavs {
		fav, ok := f.(map[string]interface{})
		if !ok {
			continue
		}
		target := FavouriteMediaTarget(fav)
		if target == "" {
			continue
		}
		title := jsonStr(fav, "title")
		if title == "" {
			title = jsonStr(fav, "name")
		}
		if title == "" {
			title = target
		}
		out = append(out, map[string]interface{}{
			"title":  title,
			"target": target,
		})
	}
	return out
}

// FavouriteMediaTarget extracts the playable URL from a favourite.
func FavouriteMediaTarget(fav map[string]interface{}) string {
	if fav == nil {
		return ""
	}
	path := jsonStr(fav, "path")
	if path != "" {
		return path
	}
	wp := jsonStr(fav, "windowparameter")
	if wp != "" {
		decoded, err := url.QueryUnescape(wp)
		if err == nil {
			wp = strings.TrimSpace(decoded)
		}
		prefixes := []string{"plugin://", "http://", "https://", "smb://", "nfs://", "file://", "musicdb://", "videodb://", "special://"}
		for _, p := range prefixes {
			if strings.HasPrefix(wp, p) {
				return wp
			}
		}
	}
	favCmd := jsonStr(fav, "favourite")
	if favCmd != "" {
		m := PlayMediaRE.FindStringSubmatch(favCmd)
		if m != nil {
			raw := strings.TrimSpace(m[1])
			raw = strings.Trim(raw, "\"'")
			if raw != "" {
				decoded, err := url.QueryUnescape(raw)
				if err == nil {
					return decoded
				}
				return raw
			}
		}
	}
	return ""
}

// FindFavouriteLabelByPath searches favourites for a matching URL and returns its name.
func FindFavouriteLabelByPath(path string) string {
	if path == "" {
		return ""
	}
	favs := GetPlayableFavourites()
	for _, f := range favs {
		if jsonStr(f, "target") == path {
			return jsonStr(f, "title")
		}
	}
	return ""
}

// PlayFavouriteTarget plays a favourite by its target URL.
func PlayFavouriteTarget(target, title string) bool {
	if target == "" {
		return false
	}
	StopPlayerAndClearPlaylists()
	res := KodiCall("Player.Open", map[string]interface{}{"item": map[string]interface{}{"file": target}})
	_, hasErr := res["error"]
	return !hasErr
}

// AddToFavourites adds a media item to Kodi favourites.
func AddToFavourites(title, path, thumbnail string) bool {
	params := map[string]interface{}{
		"title": title,
		"type":  "media",
		"path":  path,
	}
	if thumbnail != "" {
		if strings.HasPrefix(thumbnail, "http") {
			encoded := url.QueryEscape(thumbnail)
			thumbnail = fmt.Sprintf("image://%s/", encoded)
		}
		params["thumbnail"] = thumbnail
	}
	res := KodiCall("Favourites.AddFavourite", params)
	_, hasErr := res["error"]
	return !hasErr
}

// GetFavourites returns raw favourites list.
func GetFavourites() []interface{} {
	res := KodiCall("Favourites.GetFavourites", map[string]interface{}{"properties": []string{"path", "thumbnail"}})
	result := jsonMap(res, "result")
	if result == nil {
		return nil
	}
	favs, _ := result["favourites"].([]interface{})
	return favs
}

// RemoveFavourite removes a favourite by title via SSH + profile reload.
func RemoveFavourite(title string) bool {
	if title == "" {
		return false
	}
	cfg := config.Get()
	escapedTitle := regexp.QuoteMeta(title)
	escapedTitle = strings.ReplaceAll(escapedTitle, "/", `\/`)
	remoteCmd := fmt.Sprintf(`sed -i '/name="%s"/d' /storage/.kodi/userdata/favourites.xml`, escapedTitle)
	sshCmd := fmt.Sprintf("ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@%s %s", cfg.KodiHost, remoteCmd)

	log.Printf("INFO Executing SSH cleanup for favorite: %s", title)
	cmd := exec.Command("bash", "-c", sshCmd)
	out, err := cmd.CombinedOutput()
	if err != nil {
		log.Printf("ERROR SSH cleanup failed: %v output=%s", err, string(out))
		return false
	}

	res := KodiCall("Profiles.LoadProfile", map[string]interface{}{"profile": "Master user"})
	log.Printf("INFO Profile reload response: %v", res)
	return true
}

// ── Picture playback ────────────────────────────────────────────────

// PlayPicture opens a single picture file in Kodi.
func PlayPicture(filePath string) bool {
	if filePath == "" {
		return false
	}
	StopVideoPlayers()
	res := KodiCall("Player.Open", map[string]interface{}{"item": map[string]interface{}{"file": filePath}})
	_, hasErr := res["error"]
	return !hasErr
}

// PlayPictureSlideshow starts a picture slideshow from a directory.
func PlayPictureSlideshow(directoryPath string) bool {
	if directoryPath == "" {
		return false
	}
	StopVideoPlayers()
	res := KodiCall("Player.Open", map[string]interface{}{
		"item": map[string]interface{}{
			"directory": directoryPath,
			"media":     "pictures",
			"recursive": false,
		},
	})
	_, hasErr := res["error"]
	return !hasErr
}

// ── AV settings ─────────────────────────────────────────────────────

// GetAVSettings returns audio/subtitle stream info for the active player.
func GetAVSettings() map[string]interface{} {
	playerID := GetActivePlayerID()
	if playerID < 0 {
		return map[string]interface{}{"playerid": nil, "error": "nothing_playing"}
	}
	res := KodiCall("Player.GetProperties", map[string]interface{}{
		"playerid": playerID,
		"properties": []string{
			"audiostreams", "currentaudiostream",
			"subtitles", "currentsubtitle", "subtitleenabled",
		},
	})
	if _, hasErr := res["error"]; hasErr {
		return map[string]interface{}{"playerid": playerID, "error": res["error"]}
	}
	result := jsonMap(res, "result")
	if result == nil {
		result = make(map[string]interface{})
	}
	return map[string]interface{}{
		"playerid":           playerID,
		"audiostreams":       result["audiostreams"],
		"currentaudiostream": result["currentaudiostream"],
		"subtitles":          result["subtitles"],
		"currentsubtitle":    result["currentsubtitle"],
		"subtitleenabled":    result["subtitleenabled"],
	}
}

// SetAudioStream sets the audio stream for the active player.
func SetAudioStream(streamIndex int) bool {
	playerID := GetActivePlayerID()
	if playerID < 0 {
		return false
	}
	res := KodiCall("Player.SetAudioStream", map[string]interface{}{"playerid": playerID, "stream": streamIndex})
	_, hasErr := res["error"]
	return !hasErr
}

// SetSubtitleStream sets and enables a subtitle stream.
func SetSubtitleStream(subtitleIndex int) bool {
	playerID := GetActivePlayerID()
	if playerID < 0 {
		return false
	}
	res := KodiCall("Player.SetSubtitle", map[string]interface{}{
		"playerid": playerID,
		"subtitle": subtitleIndex,
		"enable":   true,
	})
	_, hasErr := res["error"]
	return !hasErr
}

// DisableSubtitles disables subtitles on the active player.
func DisableSubtitles() bool {
	playerID := GetActivePlayerID()
	if playerID < 0 {
		return false
	}
	res := KodiCall("Player.SetSubtitle", map[string]interface{}{
		"playerid": playerID,
		"subtitle": "off",
		"enable":   false,
	})
	_, hasErr := res["error"]
	return !hasErr
}

// ── JSON helpers ────────────────────────────────────────────────────

func jsonStr(m map[string]interface{}, key string) string {
	if m == nil {
		return ""
	}
	v, ok := m[key]
	if !ok || v == nil {
		return ""
	}
	s, ok := v.(string)
	if !ok {
		return ""
	}
	return s
}

func jsonInt(m map[string]interface{}, key string, def int) int {
	if m == nil {
		return def
	}
	v, ok := m[key]
	if !ok || v == nil {
		return def
	}
	switch n := v.(type) {
	case float64:
		return int(n)
	case int:
		return n
	case json.Number:
		i, err := n.Int64()
		if err != nil {
			return def
		}
		return int(i)
	}
	return def
}

func jsonMap(m map[string]interface{}, key string) map[string]interface{} {
	if m == nil {
		return nil
	}
	v, ok := m[key]
	if !ok || v == nil {
		return nil
	}
	sub, ok := v.(map[string]interface{})
	if !ok {
		return nil
	}
	return sub
}

func jsonArr(m map[string]interface{}, key string) []interface{} {
	if m == nil {
		return nil
	}
	v, ok := m[key]
	if !ok || v == nil {
		return nil
	}
	arr, ok := v.([]interface{})
	if !ok {
		return nil
	}
	return arr
}

// JsonStrSlice extracts a string slice from a JSON array field.
func JsonStrSlice(m map[string]interface{}, key string) []string {
	arr := jsonArr(m, key)
	if arr == nil {
		return nil
	}
	out := make([]string, 0, len(arr))
	for _, v := range arr {
		if s, ok := v.(string); ok {
			out = append(out, s)
		}
	}
	return out
}

// Now returns the current time as a float64 (seconds since epoch).
func Now() float64 {
	return float64(time.Now().UnixMilli()) / 1000.0
}
