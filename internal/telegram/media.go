package telegram

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"mime"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/PaulSonOfLars/gotgbot/v2"
	"github.com/willheisenberg/KodiMediaBot/internal/config"
	ha "github.com/willheisenberg/KodiMediaBot/internal/core/homeassistant"
)

// SocialVideoDomains contains domains we can download via yt-dlp.
var SocialVideoDomains = []string{
	"tiktok.com", "instagram.com", "facebook.com", "fb.watch", "x.com", "twitter.com",
}

// ── Media server state ──────────────────────────────────────────────

var (
	serverLock    sync.Mutex
	serverStarted bool

	tempMediaMu      sync.Mutex
	tempMediaKeys    = make(map[string]int)     // norm key → entry_id
	tempMediaEntries = make(map[int]*tempEntry)
	tempEntryCounter int

	imageSessionMu sync.Mutex
	imageSession   *ImageSession
)

type tempEntry struct {
	ID           int
	Title        string
	Kind         string
	Keys         map[string]bool
	CleanupPaths []string
	CleanupDirs  []string
}

// ImageSession tracks an active photo slideshow session.
type ImageSession struct {
	LocalDir   string
	KodiDir    string
	Count      int
	ImagePaths []string
	Title      string
}

// MediaDownloadError is returned when a media download fails.
type MediaDownloadError struct {
	UserMessage string
	Detail      string
}

func (e *MediaDownloadError) Error() string {
	return e.Detail
}

// ── Path helpers ────────────────────────────────────────────────────

func EnsureUploadDir() {
	cfg := config.Get()
	os.MkdirAll(cfg.UploadDir, 0755)
}

func ResolveKodiMediaPath(localPath string) string {
	cfg := config.Get()
	absLocal, _ := filepath.Abs(localPath)
	absUpload, _ := filepath.Abs(cfg.UploadDir)
	rel, err := filepath.Rel(absUpload, absLocal)
	if err != nil || strings.HasPrefix(rel, "..") {
		return localPath
	}
	return filepath.Join(cfg.KodiUploadDir, rel)
}

func ResolveMediaBaseURL() string {
	return config.Get().ResolveMediaBaseURL()
}

func BuildMediaURL(filename string) string {
	return fmt.Sprintf("%s/media/%s", ResolveMediaBaseURL(), url.PathEscape(filename))
}

func NormalizeMediaURL(u string) string {
	parsed, err := url.Parse(u)
	if err != nil || parsed.Scheme == "" || parsed.Host == "" {
		return u
	}
	return fmt.Sprintf("%s://%s%s", parsed.Scheme, parsed.Host, parsed.Path)
}

func SanitizeStem(name string) string {
	re := regexp.MustCompile(`[^A-Za-z0-9._-]+`)
	safe := re.ReplaceAllString(strings.TrimSpace(name), "_")
	safe = strings.Trim(safe, "._-")
	if safe == "" {
		return "upload"
	}
	return safe
}

func FormatBytes(size int64) string {
	if size < 0 {
		return "unknown size"
	}
	units := []string{"B", "KB", "MB", "GB", "TB"}
	value := float64(size)
	for _, unit := range units {
		if value < 1024 || unit == "TB" {
			if unit == "B" {
				return fmt.Sprintf("%d %s", int(value), unit)
			}
			return fmt.Sprintf("%.1f %s", value, unit)
		}
		value /= 1024
	}
	return fmt.Sprintf("%.1f TB", value)
}

func ChooseExtension(fileName, mimeType, fallback string) string {
	if fileName != "" {
		ext := filepath.Ext(fileName)
		if ext != "" {
			return strings.ToLower(ext)
		}
	}
	if mimeType != "" {
		exts, _ := mime.ExtensionsByType(mimeType)
		if len(exts) > 0 {
			return strings.ToLower(exts[0])
		}
	}
	return fallback
}

func BuildStorageName(prefix, fileName, mimeType, fallbackExt string) string {
	stem := SanitizeStem(prefix)
	ext := ChooseExtension(fileName, mimeType, fallbackExt)
	ts := time.Now().UnixMilli()
	return fmt.Sprintf("%s_%d%s", stem, ts, ext)
}

// ── Temp media registration ─────────────────────────────────────────

func normalizeMediaKey(key string) string {
	if key == "" {
		return ""
	}
	if strings.Contains(key, "://") {
		return NormalizeMediaURL(key)
	}
	abs, err := filepath.Abs(key)
	if err != nil {
		return key
	}
	return abs
}

func RegisterTempMedia(path, title string) map[string]interface{} {
	fileName := filepath.Base(path)
	mediaURL := BuildMediaURL(fileName)
	keys := []string{normalizeMediaKey(mediaURL), normalizeMediaKey(path)}
	registerTempEntry(keys, title, "video", []string{path}, nil)
	return map[string]interface{}{
		"title": title,
		"url":   mediaURL,
		"kind":  "video",
		"link":  mediaURL,
	}
}

func registerTempEntry(keys []string, title, kind string, cleanupPaths, cleanupDirs []string) *tempEntry {
	tempMediaMu.Lock()
	defer tempMediaMu.Unlock()
	tempEntryCounter++
	entry := &tempEntry{
		ID:           tempEntryCounter,
		Title:        title,
		Kind:         kind,
		Keys:         make(map[string]bool),
		CleanupPaths: cleanupPaths,
		CleanupDirs:  cleanupDirs,
	}
	tempMediaEntries[entry.ID] = entry
	for _, key := range keys {
		if key == "" {
			continue
		}
		norm := normalizeMediaKey(key)
		tempMediaKeys[norm] = entry.ID
		entry.Keys[norm] = true
	}
	return entry
}

// GetTempMediaTitle returns the title for a temp media file, or "".
func GetTempMediaTitle(key string) string {
	norm := normalizeMediaKey(key)
	tempMediaMu.Lock()
	defer tempMediaMu.Unlock()
	eid, ok := tempMediaKeys[norm]
	if !ok {
		return ""
	}
	entry := tempMediaEntries[eid]
	if entry == nil {
		return ""
	}
	return entry.Title
}

func cleanupTempEntry(eid int) {
	entry := tempMediaEntries[eid]
	if entry == nil {
		return
	}
	for _, p := range entry.CleanupPaths {
		os.Remove(p)
	}
	for _, d := range entry.CleanupDirs {
		os.RemoveAll(d)
	}
	for k := range entry.Keys {
		delete(tempMediaKeys, k)
	}
	delete(tempMediaEntries, eid)
}

// ── Image session ───────────────────────────────────────────────────

func createImageSessionDir() *ImageSession {
	EnsureUploadDir()
	cfg := config.Get()
	ts := time.Now().UnixMilli()
	localDir := filepath.Join(cfg.UploadDir, fmt.Sprintf("slideshow_%d", ts))
	os.MkdirAll(localDir, 0755)
	return &ImageSession{
		LocalDir:   localDir,
		KodiDir:    ResolveKodiMediaPath(localDir),
		Count:      0,
		ImagePaths: nil,
		Title:      "Photo slideshow",
	}
}

func CleanupActiveImageSession() {
	imageSessionMu.Lock()
	session := imageSession
	imageSession = nil
	imageSessionMu.Unlock()
	if session != nil {
		os.RemoveAll(session.LocalDir)
	}
}

// ── Media download ──────────────────────────────────────────────────

// ClassifyMessage classifies a Telegram message as a media type.
func ClassifyMessage(msg *gotgbot.Message) (string, string) {
	if msg.Voice != nil {
		return "voice", msg.Voice.FileId
	}
	if msg.Audio != nil {
		return "audio", msg.Audio.FileId
	}
	if msg.Video != nil {
		return "video", msg.Video.FileId
	}
	if msg.VideoNote != nil {
		return "videonote", msg.VideoNote.FileId
	}
	if len(msg.Photo) > 0 {
		best := msg.Photo[len(msg.Photo)-1]
		return "photo", best.FileId
	}
	if msg.Document != nil {
		mimeType := msg.Document.MimeType
		if strings.HasPrefix(mimeType, "audio/") {
			return "audio", msg.Document.FileId
		}
		if strings.HasPrefix(mimeType, "video/") {
			return "video", msg.Document.FileId
		}
		if strings.HasPrefix(mimeType, "image/") {
			return "photo", msg.Document.FileId
		}
		return "document", msg.Document.FileId
	}
	return "", ""
}

// DownloadMediaItem downloads a Telegram file and stores it locally.
func DownloadMediaItem(bot *gotgbot.Bot, fileID, title, mimeType string) (string, error) {
	cfg := config.Get()
	EnsureUploadDir()

	file, err := bot.GetFile(fileID, nil)
	if err != nil {
		return "", &MediaDownloadError{UserMessage: "Could not get file info.", Detail: err.Error()}
	}

	if file.FileSize > cfg.TelegramDownloadSizeLimit {
		return "", &MediaDownloadError{
			UserMessage: fmt.Sprintf("File too large (%s, max %s).",
				FormatBytes(file.FileSize), FormatBytes(cfg.TelegramDownloadSizeLimit)),
			Detail: "file too large",
		}
	}

	ext := ChooseExtension(file.FilePath, mimeType, ".bin")
	storageName := BuildStorageName(title, file.FilePath, mimeType, ext)
	localPath := filepath.Join(cfg.UploadDir, storageName)

	// Download file via Telegram API
	apiBase := "https://api.telegram.org"
	if cfg.TelegramBaseFileURL != "" {
		apiBase = strings.TrimRight(cfg.TelegramBaseFileURL, "/")
	} else if cfg.TelegramBaseURL != "" {
		apiBase = strings.TrimRight(cfg.TelegramBaseURL, "/")
	}
	fileURL := fmt.Sprintf("%s/file/bot%s/%s", apiBase, bot.Token, file.FilePath)
	resp, err := http.Get(fileURL)
	if err != nil {
		return "", &MediaDownloadError{UserMessage: "Download failed.", Detail: err.Error()}
	}
	defer resp.Body.Close()

	out, err := os.Create(localPath)
	if err != nil {
		return "", &MediaDownloadError{UserMessage: "Storage error.", Detail: err.Error()}
	}
	defer out.Close()

	_, err = io.Copy(out, resp.Body)
	if err != nil {
		os.Remove(localPath)
		return "", &MediaDownloadError{UserMessage: "Download failed.", Detail: err.Error()}
	}

	return localPath, nil
}

// IsSocialVideoURL checks if a URL is from a supported social media platform.
func IsSocialVideoURL(u string) bool {
	parsed, err := url.Parse(u)
	if err != nil {
		return false
	}
	host := strings.ToLower(parsed.Hostname())
	for _, domain := range SocialVideoDomains {
		if host == domain || strings.HasSuffix(host, "."+domain) {
			return true
		}
	}
	return false
}

// DownloadSocialVideo downloads a video from a social media URL using yt-dlp.
func DownloadSocialVideo(videoURL, title string) (string, error) {
	cfg := config.Get()
	EnsureUploadDir()

	stem := SanitizeStem(title)
	ts := time.Now().UnixMilli()
	outTemplate := filepath.Join(cfg.UploadDir, fmt.Sprintf("%s_%d.%%(ext)s", stem, ts))

	cmd := exec.Command("yt-dlp",
		"--no-warnings",
		"-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
		"--merge-output-format", "mp4",
		"-o", outTemplate,
		videoURL,
	)
	out, err := cmd.CombinedOutput()
	if err != nil {
		log.Printf("WARN yt-dlp download failed: url=%s err=%v out=%s", videoURL, err, string(out))
		return "", &MediaDownloadError{UserMessage: "Video download failed.", Detail: string(out)}
	}

	// Find the output file
	pattern := filepath.Join(cfg.UploadDir, fmt.Sprintf("%s_%d.*", stem, ts))
	matches, _ := filepath.Glob(pattern)
	if len(matches) == 0 {
		return "", &MediaDownloadError{UserMessage: "Downloaded file not found.", Detail: "glob returned no matches"}
	}
	return matches[0], nil
}

// MaybeFaststartMP4 runs ffmpeg -movflags faststart on an MP4 file.
func MaybeFaststartMP4(path string) string {
	if !strings.HasSuffix(strings.ToLower(path), ".mp4") {
		return path
	}
	tmpPath := path + ".faststart.mp4"
	cmd := exec.Command("ffmpeg", "-y", "-i", path, "-c", "copy", "-movflags", "faststart", tmpPath)
	err := cmd.Run()
	if err != nil {
		os.Remove(tmpPath)
		return path
	}
	os.Remove(path)
	os.Rename(tmpPath, path)
	return path
}

// MaybeCompressImage compresses an image if it's too large.
func MaybeCompressImage(path string, maxBytes int64) string {
	info, err := os.Stat(path)
	if err != nil || info.Size() <= maxBytes {
		return path
	}
	ext := strings.ToLower(filepath.Ext(path))
	if ext != ".jpg" && ext != ".jpeg" && ext != ".png" {
		return path
	}
	tmpPath := path + ".compressed" + ext
	cmd := exec.Command("ffmpeg", "-y", "-i", path, "-q:v", "5", tmpPath)
	err = cmd.Run()
	if err != nil {
		os.Remove(tmpPath)
		return path
	}
	os.Remove(path)
	os.Rename(tmpPath, path)
	return path
}

// CleanupStaleTempMedia removes leftover temp files at startup.
func CleanupStaleTempMedia() {
	cfg := config.Get()
	entries, err := os.ReadDir(cfg.UploadDir)
	if err != nil {
		return
	}
	now := time.Now()
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		age := now.Sub(info.ModTime())
		if age > 24*time.Hour {
			os.Remove(filepath.Join(cfg.UploadDir, e.Name()))
		}
	}
}

// ── HTTP Media Server ───────────────────────────────────────────────

// StartMediaServer starts the HTTP media server in a goroutine.
func StartMediaServer() {
	serverLock.Lock()
	if serverStarted {
		serverLock.Unlock()
		return
	}
	serverStarted = true
	serverLock.Unlock()

	cfg := config.Get()
	EnsureUploadDir()

	mux := http.NewServeMux()
	mux.HandleFunc("/health", handleHealth)
	mux.HandleFunc("/media/", handleMedia)
	mux.HandleFunc("/app/ha-color", handleHAColorPage)
	mux.HandleFunc("/app/ha-color/state", handleHAColorState)
	mux.HandleFunc("/app/ha-color/apply", handleHAColorApply)
	mux.HandleFunc("/app/ha-color/save", handleHAColorSave)

	addr := fmt.Sprintf("%s:%d", cfg.MediaServerHost, cfg.MediaServerPort)
	log.Printf("INFO Media server listening on %s", addr)

	go func() {
		server := &http.Server{
			Addr:         addr,
			Handler:      mux,
			ReadTimeout:  30 * time.Second,
			WriteTimeout: 30 * time.Second,
		}
		if err := server.ListenAndServe(); err != nil {
			log.Fatalf("ERROR Media server failed: %v", err)
		}
	}()
}

func handleHealth(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/plain")
	w.WriteHeader(200)
	w.Write([]byte("ok"))
}

func handleMedia(w http.ResponseWriter, r *http.Request) {
	cfg := config.Get()
	// Strip /media/ prefix
	filename := strings.TrimPrefix(r.URL.Path, "/media/")
	filename, _ = url.PathUnescape(filename)
	if filename == "" || strings.Contains(filename, "..") {
		http.Error(w, "Not Found", 404)
		return
	}

	filePath := filepath.Join(cfg.UploadDir, filename)
	info, err := os.Stat(filePath)
	if err != nil {
		http.Error(w, "Not Found", 404)
		return
	}

	// Support Range requests for video streaming
	f, err := os.Open(filePath)
	if err != nil {
		http.Error(w, "Internal Server Error", 500)
		return
	}
	defer f.Close()

	contentType := mime.TypeByExtension(filepath.Ext(filename))
	if contentType == "" {
		contentType = "application/octet-stream"
	}
	w.Header().Set("Content-Type", contentType)
	http.ServeContent(w, r, filename, info.ModTime(), f)
}

// ── HA Webapp Route Handlers ────────────────────────────────────────

func requestOrigin(r *http.Request) string {
	forwardedProto := strings.TrimSpace(strings.SplitN(r.Header.Get("X-Forwarded-Proto"), ",", 2)[0])
	forwardedHost := strings.TrimSpace(strings.SplitN(r.Header.Get("X-Forwarded-Host"), ",", 2)[0])
	host := forwardedHost
	if host == "" {
		host = strings.TrimSpace(strings.SplitN(r.Header.Get("Host"), ",", 2)[0])
	}
	proto := strings.ToLower(forwardedProto)
	if host != "" && (proto == "http" || proto == "https") {
		return fmt.Sprintf("%s://%s", proto, host)
	}
	if host != "" {
		scheme := "http"
		return fmt.Sprintf("%s://%s", scheme, host)
	}
	resolved := ha.ResolveHAWebappURL()
	if resolved != "" {
		parsed, err := url.Parse(resolved)
		if err == nil && parsed.Scheme != "" && parsed.Host != "" {
			return fmt.Sprintf("%s://%s", parsed.Scheme, parsed.Host)
		}
	}
	return ""
}

func haWebappBaseURL(r *http.Request) string {
	origin := strings.TrimRight(requestOrigin(r), "/")
	if origin != "" {
		return origin + "/app/ha-color"
	}
	return strings.TrimRight(ha.ResolveHAWebappURL(), "/")
}

func readJSONBody(r *http.Request) map[string]interface{} {
	body, err := io.ReadAll(r.Body)
	if err != nil {
		return nil
	}
	var data map[string]interface{}
	if json.Unmarshal(body, &data) != nil {
		return nil
	}
	return data
}

func sendJSON(w http.ResponseWriter, status int, data interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(data)
}

func sendHTML(w http.ResponseWriter, status int, html string) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(status)
	w.Write([]byte(html))
}

func handleHAColorPage(w http.ResponseWriter, r *http.Request) {
	if r.Method != "GET" {
		http.Error(w, "Method Not Allowed", 405)
		return
	}
	if !ha.HAAvailable() {
		sendHTML(w, 503, "<!doctype html><html><body><p>Home Assistant is not configured.</p></body></html>")
		return
	}
	appBaseURL := haWebappBaseURL(r)
	log.Printf("INFO HA webapp page served base_url=%s path=%s", appBaseURL, r.URL.Path)
	sendHTML(w, 200, BuildHAColorWebappHTML(appBaseURL))
}

func handleHAColorState(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		http.Error(w, "Method Not Allowed", 405)
		return
	}
	payload, ok := validateWebappPayload(w, r)
	if !ok {
		return
	}
	_ = payload
	log.Printf("INFO HA webapp state request accepted")
	lightState := ha.GetLightState()
	if lightState == nil {
		lightState = map[string]interface{}{}
	}
	sendJSON(w, 200, map[string]interface{}{
		"ok":           true,
		"light_state":  lightState,
		"saved_colors": ha.LoadSavedColors(),
	})
}

func handleHAColorApply(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		http.Error(w, "Method Not Allowed", 405)
		return
	}
	payload, ok := validateWebappPayload(w, r)
	if !ok {
		return
	}
	rgb := ParseRGBTriplet(payload)
	if rgb == nil {
		sendJSON(w, 400, map[string]interface{}{"ok": false, "error": "Invalid RGB values."})
		return
	}
	brightnessProvided := payload["brightness_pct"] != nil
	brightnessPct := ParseBrightnessPercent(payload)
	if brightnessProvided && brightnessPct == nil {
		sendJSON(w, 400, map[string]interface{}{"ok": false, "error": "Invalid brightness value."})
		return
	}
	log.Printf("INFO HA webapp apply rgb=%v brightness_pct=%v", rgb, brightnessPct)
	ok2 := ha.SetLightColor(rgb[0], rgb[1], rgb[2], brightnessPct)
	if !ok2 {
		sendJSON(w, 502, map[string]interface{}{"ok": false, "error": "Color could not be applied."})
		return
	}
	lightState := ha.GetLightState()
	if lightState == nil {
		lightState = map[string]interface{}{
			"state":         "on",
			"rgb_color":     rgb,
			"friendly_name": config.Get().HALightID,
		}
	}
	sendJSON(w, 200, map[string]interface{}{
		"ok":           true,
		"light_state":  lightState,
		"saved_colors": ha.LoadSavedColors(),
	})
}

func handleHAColorSave(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		http.Error(w, "Method Not Allowed", 405)
		return
	}
	payload, ok := validateWebappPayload(w, r)
	if !ok {
		return
	}
	rgb := ParseRGBTriplet(payload)
	if rgb == nil {
		sendJSON(w, 400, map[string]interface{}{"ok": false, "error": "Invalid RGB values."})
		return
	}
	name, _ := payload["name"].(string)
	name = strings.TrimSpace(name)
	if name == "" {
		sendJSON(w, 400, map[string]interface{}{"ok": false, "error": "Preset name is required."})
		return
	}
	log.Printf("INFO HA webapp save name=%s rgb=%v", name, rgb)
	ok2 := ha.SaveColor(name, rgb[0], rgb[1], rgb[2])
	if !ok2 {
		sendJSON(w, 502, map[string]interface{}{"ok": false, "error": "Preset could not be saved."})
		return
	}
	sendJSON(w, 200, map[string]interface{}{
		"ok":           true,
		"saved_colors": ha.LoadSavedColors(),
	})
}

func validateWebappPayload(w http.ResponseWriter, r *http.Request) (map[string]interface{}, bool) {
	if !ha.HAAvailable() {
		sendJSON(w, 503, map[string]interface{}{"ok": false, "error": "Home Assistant is not configured."})
		return nil, false
	}
	payload := readJSONBody(r)
	if payload == nil {
		sendJSON(w, 400, map[string]interface{}{"ok": false, "error": "Invalid JSON payload."})
		return nil, false
	}
	initData, _ := payload["init_data"].(string)
	if !ValidateWebappInitData(initData, config.Get().TGToken, config.Get().HAWebappMaxAge) {
		sendJSON(w, 403, map[string]interface{}{"ok": false, "error": "Telegram Mini App validation failed."})
		return nil, false
	}
	return payload, true
}

// ParseRGBTriplet extracts [r, g, b] from a payload.
func ParseRGBTriplet(payload map[string]interface{}) []int {
	rVal := toInt(payload["r"])
	gVal := toInt(payload["g"])
	bVal := toInt(payload["b"])
	if rVal == nil || gVal == nil || bVal == nil {
		return nil
	}
	r, g, b := *rVal, *gVal, *bVal
	if r < 0 || r > 255 || g < 0 || g > 255 || b < 0 || b > 255 {
		return nil
	}
	return []int{r, g, b}
}

// ParseBrightnessPercent extracts brightness_pct from a payload.
func ParseBrightnessPercent(payload map[string]interface{}) *int {
	raw := payload["brightness_pct"]
	if raw == nil {
		return nil
	}
	v := toInt(raw)
	if v == nil {
		return nil
	}
	val := *v
	if val < 0 || val > 100 {
		return nil
	}
	return &val
}

func toInt(v interface{}) *int {
	if v == nil {
		return nil
	}
	switch n := v.(type) {
	case float64:
		i := int(n)
		return &i
	case int:
		return &n
	case string:
		i, err := strconv.Atoi(n)
		if err != nil {
			return nil
		}
		return &i
	}
	return nil
}
