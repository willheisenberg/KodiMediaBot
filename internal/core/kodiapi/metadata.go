package kodiapi

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"regexp"
	"strings"
	"time"
	"unicode"

	"github.com/willheisenberg/KodiMediaBot/internal/config"
	"golang.org/x/text/unicode/norm"
)

// FormatKodiTime formats a Kodi time dict as "MM:SS" or "H:MM:SS".
func FormatKodiTime(t map[string]interface{}) string {
	if t == nil {
		return "00:00"
	}
	h := jsonInt(t, "hours", 0)
	m := jsonInt(t, "minutes", 0)
	s := jsonInt(t, "seconds", 0)
	total := h*3600 + m*60 + s
	if total >= 3600 {
		return fmt.Sprintf("%d:%02d:%02d", h, m, s)
	}
	return fmt.Sprintf("%02d:%02d", m, s)
}

// KodiTimeSeconds converts a Kodi time dict to total seconds, or -1 if nil.
func KodiTimeSeconds(t map[string]interface{}) int {
	if t == nil {
		return -1
	}
	return jsonInt(t, "hours", 0)*3600 + jsonInt(t, "minutes", 0)*60 + jsonInt(t, "seconds", 0)
}

// NormalizeTitle normalizes a title for comparison.
func NormalizeTitle(s string) string {
	if s == "" {
		return ""
	}
	re := regexp.MustCompile(`\s+`)
	return strings.ToLower(strings.TrimSpace(re.ReplaceAllString(s, " ")))
}

// NormalizeRadioTrackTitle cleans up a radio track title for display/search.
func NormalizeRadioTrackTitle(trackTitle string) string {
	if trackTitle == "" {
		return ""
	}
	re := regexp.MustCompile(`\s+`)
	raw := strings.TrimSpace(re.ReplaceAllString(trackTitle, " "))
	if raw == "" {
		return ""
	}
	parts := strings.Split(raw, "|")
	var candidates []string
	for _, p := range parts {
		p = strings.TrimSpace(p)
		if p != "" {
			candidates = append(candidates, p)
		}
	}
	if len(candidates) == 0 {
		candidates = []string{raw}
	}
	var dashed []string
	for _, p := range candidates {
		if strings.Contains(p, " - ") {
			dashed = append(dashed, p)
		}
	}
	pick := candidates[0]
	if len(dashed) > 0 {
		pick = dashed[0]
	}

	if strings.Contains(pick, " - ") {
		parts := strings.SplitN(pick, " - ", 2)
		left := strings.TrimSpace(parts[0])
		right := strings.TrimSpace(parts[1])
		if left != "" && right != "" {
			return left + " - " + right
		}
	}
	return pick
}

// NormalizeMatchText strips accents, brackets, and noise words for fuzzy matching.
func NormalizeMatchText(text string) string {
	if text == "" {
		return ""
	}
	// NFKD decomposition
	n := norm.NFKD.String(text)
	// Strip non-ASCII
	var buf strings.Builder
	for _, r := range n {
		if r < 128 {
			buf.WriteRune(unicode.ToLower(r))
		}
	}
	n = buf.String()

	// Remove bracketed content
	bracketRE := regexp.MustCompile(`\[[^\]]*\]|\([^)]*\)|\{[^}]*\}`)
	n = bracketRE.ReplaceAllString(n, " ")
	// Normalize feat
	featRE := regexp.MustCompile(`\b(ft|feat)\.?\b`)
	n = featRE.ReplaceAllString(n, " feat ")
	// Remove noise words
	noiseRE := regexp.MustCompile(`\b(official|audio|video|lyrics?|lyric|visualizer|remaster(?:ed)?|hd|4k|hq|topic|vevo)\b`)
	n = noiseRE.ReplaceAllString(n, " ")
	// Keep only alnum
	alnumRE := regexp.MustCompile(`[^a-z0-9]+`)
	n = alnumRE.ReplaceAllString(n, " ")
	spaceRE := regexp.MustCompile(`\s+`)
	return strings.TrimSpace(spaceRE.ReplaceAllString(n, " "))
}

// YoutubeResultMatchesRadioTrack checks if a YouTube result matches a radio track.
func YoutubeResultMatchesRadioTrack(trackTitle, resultTitle string) bool {
	cleanTrack := NormalizeRadioTrackTitle(trackTitle)
	trackNorm := NormalizeMatchText(cleanTrack)
	resultNorm := NormalizeMatchText(resultTitle)
	if trackNorm == "" || resultNorm == "" {
		return false
	}
	if strings.Contains(trackNorm, resultNorm) || strings.Contains(resultNorm, trackNorm) {
		return true
	}
	if !strings.Contains(cleanTrack, " - ") {
		return false
	}
	parts := strings.SplitN(cleanTrack, " - ", 2)
	artistNorm := NormalizeMatchText(parts[0])
	titleNorm := NormalizeMatchText(parts[1])
	if artistNorm == "" || titleNorm == "" {
		return false
	}
	return strings.Contains(resultNorm, artistNorm) && strings.Contains(resultNorm, titleNorm)
}

// SoundcloudResultMatchesRadioTrack checks if a SoundCloud result matches a radio track.
func SoundcloudResultMatchesRadioTrack(trackTitle, resultTitle, resultArtist string) bool {
	cleanTrack := NormalizeRadioTrackTitle(trackTitle)
	trackNorm := NormalizeMatchText(cleanTrack)
	titleNorm := NormalizeMatchText(resultTitle)
	artistNorm := NormalizeMatchText(resultArtist)
	var combinedParts []string
	if artistNorm != "" {
		combinedParts = append(combinedParts, artistNorm)
	}
	if titleNorm != "" {
		combinedParts = append(combinedParts, titleNorm)
	}
	combinedNorm := strings.Join(combinedParts, " ")
	if trackNorm == "" || combinedNorm == "" {
		return false
	}
	if strings.Contains(combinedNorm, trackNorm) {
		return true
	}
	if !strings.Contains(cleanTrack, " - ") {
		return strings.Contains(titleNorm, trackNorm)
	}
	parts := strings.SplitN(cleanTrack, " - ", 2)
	expArtistNorm := NormalizeMatchText(parts[0])
	expTitleNorm := NormalizeMatchText(parts[1])
	if expArtistNorm == "" || expTitleNorm == "" {
		return false
	}
	return strings.Contains(combinedNorm, expArtistNorm) && strings.Contains(combinedNorm, expTitleNorm)
}

// KodiItemName returns a display name for a Kodi item.
func KodiItemName(item map[string]interface{}) string {
	if item == nil {
		return ""
	}
	artists := JsonStrSlice(item, "artist")
	title := jsonStr(item, "title")
	label := jsonStr(item, "label")
	if len(artists) > 0 && title != "" {
		return strings.Join(artists, ", ") + " - " + title
	}
	if label != "" {
		return label
	}
	return title
}

// ExtractYoutubeID extracts a YouTube video ID from a URL.
func ExtractYoutubeID(u string) string {
	if u == "" {
		return ""
	}
	m := YTRE.FindStringSubmatch(u)
	if m != nil {
		return m[1]
	}
	parsed, err := url.Parse(u)
	if err != nil {
		return ""
	}
	qs := parsed.Query()
	vidParam := qs.Get("video_id")
	if vidParam != "" && YTIDRegex.MatchString(vidParam) {
		return vidParam
	}
	fileParam := qs.Get("file")
	if fileParam != "" {
		parts := strings.Split(fileParam, "/")
		base := parts[len(parts)-1]
		if idx := strings.Index(base, "."); idx >= 0 {
			base = base[:idx]
		}
		if YTIDRegex.MatchString(base) {
			return base
		}
	}
	for _, part := range strings.Split(parsed.Path, "/") {
		if YTIDRegex.MatchString(part) {
			return part
		}
	}
	return ""
}

// SoundcloudSlug generates a slug from text for SoundCloud URL guessing.
func SoundcloudSlug(text string) string {
	if text == "" {
		return ""
	}
	n := norm.NFKD.String(text)
	var buf strings.Builder
	for _, r := range n {
		if r < 128 {
			buf.WriteRune(r)
		}
	}
	lower := strings.ToLower(buf.String())
	slugRE := regexp.MustCompile(`[^a-z0-9]+`)
	slug := slugRE.ReplaceAllString(lower, "-")
	return strings.Trim(slug, "-")
}

// SoundcloudTrackSlugFromURL extracts the track slug from a SoundCloud URL.
func SoundcloudTrackSlugFromURL(u string) string {
	if u == "" {
		return ""
	}
	re := regexp.MustCompile(`^https?://(www\.)?soundcloud\.com/[^/]+/([^/?#]+)`)
	m := re.FindStringSubmatch(u)
	if m == nil {
		return ""
	}
	return m[2]
}

// SoundcloudDisplayTitleFromURL derives a display title from a SoundCloud URL.
func SoundcloudDisplayTitleFromURL(u string) string {
	if u == "" {
		return ""
	}
	re := regexp.MustCompile(`^https?://(www\.)?soundcloud\.com/([^/]+)/([^/?#]+)`)
	m := re.FindStringSubmatch(u)
	if m == nil {
		return ""
	}
	artist, _ := url.QueryUnescape(m[2])
	artist = strings.ReplaceAll(artist, "-", " ")
	artist = strings.TrimSpace(artist)
	track, _ := url.QueryUnescape(m[3])
	track = strings.ReplaceAll(track, "-", " ")
	track = strings.TrimSpace(track)
	if artist != "" && track != "" {
		return artist + " - " + track
	}
	if artist != "" {
		return artist
	}
	return track
}

// GuessSoundcloudLink guesses a SoundCloud URL from artist and title.
func GuessSoundcloudLink(artist interface{}, title string) string {
	var artistStr string
	switch a := artist.(type) {
	case string:
		artistStr = a
	case []interface{}:
		if len(a) > 0 {
			if s, ok := a[0].(string); ok {
				artistStr = s
			}
		}
	case []string:
		if len(a) > 0 {
			artistStr = a[0]
		}
	}
	if artistStr == "" || title == "" {
		return ""
	}
	a := SoundcloudSlug(artistStr)
	t := SoundcloudSlug(title)
	if a == "" || t == "" {
		return ""
	}
	return fmt.Sprintf("https://soundcloud.com/%s/%s", a, t)
}

// ReadSoundcloudClientID returns the cached or fresh SoundCloud client ID.
func ReadSoundcloudClientID() string {
	cfg := config.Get()
	now := Now()
	mu.Lock()
	if SCClientIDCache != "" && now-SCClientIDTS < 300 {
		val := SCClientIDCache
		mu.Unlock()
		return val
	}
	mu.Unlock()

	if cfg.SCClientID != "" {
		mu.Lock()
		SCClientIDCache = cfg.SCClientID
		SCClientIDTS = now
		mu.Unlock()
		return cfg.SCClientID
	}

	data, err := os.ReadFile(cfg.SCClientIDFile)
	if err != nil {
		return ""
	}
	val := strings.TrimSpace(string(data))
	mu.Lock()
	SCClientIDCache = val
	SCClientIDTS = now
	mu.Unlock()
	return val
}

// ExtractSoundcloudTrackID extracts a SoundCloud track ID from text.
func ExtractSoundcloudTrackID(text string) string {
	if text == "" {
		return ""
	}
	re1 := regexp.MustCompile(`soundcloud:tracks:(\d+)`)
	m := re1.FindStringSubmatch(text)
	if m != nil {
		return m[1]
	}
	re2 := regexp.MustCompile(`/tracks/(\d+)`)
	m = re2.FindStringSubmatch(text)
	if m != nil {
		return m[1]
	}
	return ""
}

// NormalizeChannelName normalizes a channel name for comparison.
func NormalizeChannelName(name string) string {
	if name == "" {
		return ""
	}
	re := regexp.MustCompile(`\s+`)
	return strings.ToLower(strings.TrimSpace(re.ReplaceAllString(name, " ")))
}

// ── Radio stream map ────────────────────────────────────────────────

// ReadRadioStreamMap parses the RADIO_STREAM_MAP env var.
func ReadRadioStreamMap() map[string]string {
	cfg := config.Get()
	raw := cfg.RadioStreamMapRaw
	if raw == "" {
		return map[string]string{}
	}
	var data map[string]interface{}
	if err := json.Unmarshal([]byte(raw), &data); err != nil {
		return map[string]string{}
	}
	out := make(map[string]string)
	for k, v := range data {
		vs, ok := v.(string)
		if !ok {
			continue
		}
		nk := NormalizeChannelName(k)
		vv := strings.TrimSpace(vs)
		if nk == "" || (!strings.HasPrefix(vv, "http://") && !strings.HasPrefix(vv, "https://")) {
			continue
		}
		out[nk] = vv
	}
	return out
}

// ReadRadioStreamMapFromM3U reads channel-to-URL mapping from an M3U file.
func ReadRadioStreamMapFromM3U(path string) map[string]string {
	data, err := os.ReadFile(path)
	if err != nil {
		return map[string]string{}
	}
	lines := strings.Split(string(data), "\n")
	out := make(map[string]string)
	lastInf := ""
	for _, line := range lines {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		if strings.HasPrefix(line, "#EXTINF:") {
			lastInf = line
			continue
		}
		if strings.HasPrefix(line, "#") {
			continue
		}
		if !strings.HasPrefix(line, "http://") && !strings.HasPrefix(line, "https://") {
			continue
		}
		name := ""
		if idx := strings.LastIndex(lastInf, ","); idx >= 0 {
			name = strings.TrimSpace(lastInf[idx+1:])
		}
		if name == "" {
			continue
		}
		norm := NormalizeChannelName(name)
		if norm == "" {
			continue
		}
		if _, exists := out[norm]; !exists {
			out[norm] = line
		}
	}
	return out
}

// GetRadioStreamM3UMap returns the cached M3U map, loading it once.
func GetRadioStreamM3UMap() map[string]string {
	mu.Lock()
	defer mu.Unlock()
	if RadioM3UMapCache == nil {
		cfg := config.Get()
		m := ReadRadioStreamMapFromM3U(cfg.RadioM3UPath)
		RadioM3UMapCache = &m
	}
	return *RadioM3UMapCache
}

// GetRadioStreamURL looks up a stream URL for a channel name.
func GetRadioStreamURL(channel string) string {
	mu.Lock()
	if RadioStreamMapCache == nil {
		m := ReadRadioStreamMap()
		RadioStreamMapCache = &m
	}
	cache := *RadioStreamMapCache
	mu.Unlock()

	key := NormalizeChannelName(channel)
	if key == "" {
		return ""
	}
	if hit, ok := cache[key]; ok && hit != "" {
		return hit
	}
	m3u := GetRadioStreamM3UMap()
	if hit, ok := m3u[key]; ok {
		return hit
	}
	return ""
}

// ── ICY title ───────────────────────────────────────────────────────

// GetCachedICYTitle returns a cached ICY title for a stream URL.
func GetCachedICYTitle(streamURL string) string {
	if streamURL == "" {
		return ""
	}
	cfg := config.Get()
	mu.Lock()
	defer mu.Unlock()
	hit, ok := ICYTitleCache[streamURL]
	if !ok {
		return ""
	}
	if Now()-hit.TS > cfg.ICYTitleTTL {
		delete(ICYTitleCache, streamURL)
		return ""
	}
	return hit.Value
}

// CacheICYTitle stores an ICY title in the cache.
func CacheICYTitle(streamURL, title string) {
	if streamURL == "" || title == "" {
		return
	}
	mu.Lock()
	ICYTitleCache[streamURL] = cachedString{Value: title, TS: Now()}
	mu.Unlock()
}

// FetchICYTitle fetches the ICY metadata title from a stream.
func FetchICYTitle(streamURL string) string {
	if streamURL == "" || (!strings.HasPrefix(streamURL, "http://") && !strings.HasPrefix(streamURL, "https://")) {
		return ""
	}
	cached := GetCachedICYTitle(streamURL)
	if cached != "" {
		return cached
	}
	cfg := config.Get()
	client := &http.Client{Timeout: time.Duration(cfg.ICYTimeout * float64(time.Second))}
	req, err := http.NewRequest("GET", streamURL, nil)
	if err != nil {
		return ""
	}
	req.Header.Set("Icy-MetaData", "1")
	req.Header.Set("User-Agent", "KodiMediaBot/1.0")

	resp, err := client.Do(req)
	if err != nil {
		return ""
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return ""
	}
	metaintStr := resp.Header.Get("icy-metaint")
	if metaintStr == "" {
		return ""
	}
	metaint := 0
	fmt.Sscanf(metaintStr, "%d", &metaint)
	if metaint <= 0 {
		return ""
	}

	// Skip audio data
	buf := make([]byte, metaint)
	if _, err := io.ReadFull(resp.Body, buf); err != nil {
		return ""
	}
	// Read length byte
	lenBuf := make([]byte, 1)
	if _, err := io.ReadFull(resp.Body, lenBuf); err != nil {
		return ""
	}
	n := int(lenBuf[0]) * 16
	if n <= 0 {
		return ""
	}
	metaBuf := make([]byte, n)
	if _, err := io.ReadFull(resp.Body, metaBuf); err != nil {
		return ""
	}
	meta := string(metaBuf)
	titleRE := regexp.MustCompile(`StreamTitle='([^']*)';`)
	m := titleRE.FindStringSubmatch(meta)
	title := ""
	if m != nil {
		title = strings.TrimSpace(m[1])
	}
	if title != "" {
		CacheICYTitle(streamURL, title)
	}
	return title
}

// ── YouTube / SoundCloud search ─────────────────────────────────────

// GetCachedYoutubeLink returns a cached YouTube link for a query key.
func GetCachedYoutubeLink(queryKey string) (string, bool) {
	if queryKey == "" {
		return "", false
	}
	cfg := config.Get()
	mu.Lock()
	defer mu.Unlock()
	hit, ok := YTSearchCache[queryKey]
	if !ok {
		return "", false
	}
	ttl := cfg.YTSearchTTL
	if hit.Value == "" {
		ttl = cfg.YTSearchFailTTL
	}
	if Now()-hit.TS > ttl {
		delete(YTSearchCache, queryKey)
		return "", false
	}
	return hit.Value, true
}

// CacheYoutubeLink stores a YouTube link in the cache.
func CacheYoutubeLink(queryKey, link string) {
	if queryKey == "" {
		return
	}
	mu.Lock()
	YTSearchCache[queryKey] = cachedString{Value: link, TS: Now()}
	mu.Unlock()
}

// GetCachedSoundcloudLink returns a cached SoundCloud link for a query key.
func GetCachedSoundcloudLink(queryKey string) (string, bool) {
	if queryKey == "" {
		return "", false
	}
	cfg := config.Get()
	mu.Lock()
	defer mu.Unlock()
	hit, ok := SCSearchCache[queryKey]
	if !ok {
		return "", false
	}
	ttl := cfg.SCSearchTTL
	if hit.Value == "" {
		ttl = cfg.SCSearchFailTTL
	}
	if Now()-hit.TS > ttl {
		delete(SCSearchCache, queryKey)
		return "", false
	}
	return hit.Value, true
}

// CacheSoundcloudLink stores a SoundCloud link in the cache.
func CacheSoundcloudLink(queryKey, link string) {
	if queryKey == "" {
		return
	}
	mu.Lock()
	SCSearchCache[queryKey] = cachedString{Value: link, TS: Now()}
	mu.Unlock()
}

// SearchYoutubeLink searches for a YouTube video matching a query.
func SearchYoutubeLink(query, expectedTitle string) string {
	if query == "" {
		return ""
	}
	cfg := config.Get()
	queryKey := NormalizeTitle(query)
	if cached, ok := GetCachedYoutubeLink(queryKey); ok {
		return cached
	}
	cmd := exec.Command("yt-dlp", "--skip-download", "--print", "%(id)s\t%(title)s", fmt.Sprintf("ytsearch1:%s", query))
	out, err := runWithTimeout(cmd, time.Duration(cfg.YTSearchTimeout*float64(time.Second)))
	link := ""
	if err == nil {
		lines := strings.Split(strings.TrimSpace(out), "\n")
		if len(lines) > 0 {
			first := strings.TrimSpace(lines[0])
			parts := strings.SplitN(first, "\t", 2)
			vid := strings.TrimSpace(parts[0])
			resultTitle := ""
			if len(parts) > 1 {
				resultTitle = strings.TrimSpace(parts[1])
			}
			if YTIDRegex.MatchString(vid) {
				candidate := "https://youtu.be/" + vid
				if expectedTitle == "" || YoutubeResultMatchesRadioTrack(expectedTitle, resultTitle) {
					link = candidate
				}
			}
		}
	}
	CacheYoutubeLink(queryKey, link)
	return link
}

// SearchSoundcloudLink searches for a SoundCloud track matching a query.
func SearchSoundcloudLink(query, expectedTitle string) string {
	if query == "" {
		return ""
	}
	cfg := config.Get()
	queryKey := NormalizeTitle(query)
	if cached, ok := GetCachedSoundcloudLink(queryKey); ok {
		return cached
	}
	cmd := exec.Command("yt-dlp", "--skip-download", "--print", "%(webpage_url)s\t%(uploader)s\t%(title)s", fmt.Sprintf("scsearch1:%s", query))
	out, err := runWithTimeout(cmd, time.Duration(cfg.SCSearchTimeout*float64(time.Second)))
	link := ""
	if err == nil {
		lines := strings.Split(strings.TrimSpace(out), "\n")
		if len(lines) > 0 {
			first := strings.TrimSpace(lines[0])
			parts := strings.SplitN(first, "\t", 3)
			pageURL := parts[0]
			// Remove query params
			if idx := strings.Index(pageURL, "?"); idx >= 0 {
				pageURL = pageURL[:idx]
			}
			pageURL = strings.TrimSpace(pageURL)
			uploader := ""
			resultTitle := ""
			if len(parts) > 1 {
				uploader = strings.TrimSpace(parts[1])
			}
			if len(parts) > 2 {
				resultTitle = strings.TrimSpace(parts[2])
			}
			if SCRE.MatchString(pageURL) {
				if expectedTitle == "" || SoundcloudResultMatchesRadioTrack(expectedTitle, resultTitle, uploader) {
					link = pageURL
				}
			}
		}
	}
	CacheSoundcloudLink(queryKey, link)
	return link
}

// RadioTitleToYoutubeLink searches YouTube for a radio track title.
func RadioTitleToYoutubeLink(trackTitle string) string {
	if trackTitle == "" {
		return ""
	}
	clean := NormalizeRadioTrackTitle(trackTitle)
	query := clean
	if strings.Contains(clean, " - ") {
		query = clean + " official audio"
	} else if clean == "" {
		query = trackTitle
	}
	expected := clean
	if expected == "" {
		expected = trackTitle
	}
	return SearchYoutubeLink(query, expected)
}

// RadioTitleToSoundcloudLink searches SoundCloud for a radio track title.
func RadioTitleToSoundcloudLink(trackTitle string) string {
	if trackTitle == "" {
		return ""
	}
	clean := NormalizeRadioTrackTitle(trackTitle)
	query := clean
	if query == "" {
		query = trackTitle
	}
	expected := clean
	if expected == "" {
		expected = trackTitle
	}
	return SearchSoundcloudLink(query, expected)
}

// ResolveRadioTitle resolves a radio channel's current track title and link.
func ResolveRadioTitle(channel, fallbackTitle string) (string, string) {
	streamURL := GetRadioStreamURL(channel)
	if streamURL == "" {
		return "", ""
	}
	title := FetchICYTitle(streamURL)
	if title == "" {
		return "", streamURL
	}
	if fallbackTitle != "" && NormalizeTitle(title) == NormalizeTitle(fallbackTitle) {
		return "", streamURL
	}
	ytLink := RadioTitleToYoutubeLink(title)
	if ytLink != "" {
		return title, ytLink
	}
	scLink := RadioTitleToSoundcloudLink(title)
	if scLink != "" {
		return title, scLink
	}
	return title, streamURL
}

// ── SoundCloud permalink ────────────────────────────────────────────

// GetCachedSoundcloudPermalink returns a cached permalink for a track ID.
func GetCachedSoundcloudPermalink(trackID string) string {
	if trackID == "" {
		return ""
	}
	mu.Lock()
	defer mu.Unlock()
	hit, ok := SCPermalinkCache[trackID]
	if !ok {
		return ""
	}
	if Now()-hit.TS > SCPermalinkTTL {
		delete(SCPermalinkCache, trackID)
		return ""
	}
	return hit.Value
}

// CacheSoundcloudPermalink stores a permalink in the cache.
func CacheSoundcloudPermalink(trackID, u string) {
	if trackID == "" || u == "" {
		return
	}
	mu.Lock()
	SCPermalinkCache[trackID] = cachedString{Value: u, TS: Now()}
	mu.Unlock()
}

// FetchSoundcloudPermalink fetches the permalink URL for a SoundCloud track ID.
func FetchSoundcloudPermalink(trackID string) string {
	if trackID == "" {
		return ""
	}
	cached := GetCachedSoundcloudPermalink(trackID)
	if cached != "" {
		return cached
	}
	clientID := ReadSoundcloudClientID()
	if clientID == "" {
		return ""
	}
	apiURL := fmt.Sprintf("https://api-v2.soundcloud.com/tracks/%s?client_id=%s", trackID, clientID)
	client := &http.Client{Timeout: 6 * time.Second}
	resp, err := client.Get(apiURL)
	if err != nil {
		return ""
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return ""
	}
	var data map[string]interface{}
	if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
		return ""
	}
	u := jsonStr(data, "permalink_url")
	if u != "" {
		CacheSoundcloudPermalink(trackID, u)
	}
	return u
}

// MaybeCacheSoundcloudURL caches a SoundCloud URL from a file URL.
func MaybeCacheSoundcloudURL(fileURL string) {
	scURL := ExtractSoundcloudURL(fileURL)
	if scURL == "" {
		// Try as a direct SoundCloud URL
		idx := strings.Index(fileURL, "?")
		clean := fileURL
		if idx >= 0 {
			clean = fileURL[:idx]
		}
		clean = strings.TrimSpace(clean)
		if SCRE.MatchString(clean) {
			scURL = clean
		}
	}
	if scURL != "" {
		mu.Lock()
		LastWSSCURL = scURL
		mu.Unlock()
	}
}

// ResolveSoundcloudLinkFromKodi tries to find the SoundCloud URL from the active Kodi player.
func ResolveSoundcloudLinkFromKodi() string {
	pid := GetActivePlayerID()
	if pid < 0 {
		return ""
	}
	curTitle := ""
	res := KodiCall("Player.GetItem", map[string]interface{}{
		"playerid":   pid,
		"properties": []string{"file"},
	})
	result := jsonMap(res, "result")
	item := jsonMap(result, "item")
	if item != nil {
		curTitle = jsonStr(item, "title")
		if curTitle == "" {
			curTitle = jsonStr(item, "label")
		}
		fileURL := jsonStr(item, "file")
		sc := ExtractSoundcloudURL(fileURL)
		if sc != "" {
			return sc
		}
		trackID := ExtractSoundcloudTrackID(fileURL)
		if trackID != "" {
			link := FetchSoundcloudPermalink(trackID)
			if link != "" {
				mu.Lock()
				LastWSSCTrackID = trackID
				LastWSSCURL = link
				mu.Unlock()
				return link
			}
		}
	}

	// Try playlist items
	plRes := KodiCall("Playlist.GetItems", map[string]interface{}{
		"playlistid": 0,
		"properties": []string{"file"},
	})
	plResult := jsonMap(plRes, "result")
	if plResult == nil {
		return ""
	}
	items := jsonArr(plResult, "items")
	want := NormalizeTitle(curTitle)
	var matched map[string]interface{}
	for _, it := range items {
		itm, ok := it.(map[string]interface{})
		if !ok {
			continue
		}
		if want != "" {
			label := jsonStr(itm, "label")
			if label == "" {
				label = jsonStr(itm, "title")
			}
			if NormalizeTitle(label) == want {
				matched = itm
				break
			}
		}
	}
	searchItems := items
	if matched != nil {
		searchItems = []interface{}{matched}
	}
	for _, it := range searchItems {
		itm, ok := it.(map[string]interface{})
		if !ok {
			continue
		}
		fileURL := jsonStr(itm, "file")
		sc := ExtractSoundcloudURL(fileURL)
		if sc != "" {
			return sc
		}
		if strings.Contains(fileURL, "media_url=") {
			parsed, err := url.Parse(fileURL)
			if err == nil {
				mediaURL := parsed.Query().Get("media_url")
				trackID := ExtractSoundcloudTrackID(mediaURL)
				if trackID != "" {
					link := FetchSoundcloudPermalink(trackID)
					if link != "" {
						mu.Lock()
						LastWSSCTrackID = trackID
						LastWSSCURL = link
						mu.Unlock()
						return link
					}
				}
			}
		}
	}
	return ""
}

// ScheduleSoundcloudPermalinkProbe starts a background goroutine to probe for SoundCloud permalink.
func ScheduleSoundcloudPermalinkProbe(timeoutS, intervalS float64) {
	now := Now()
	mu.Lock()
	if LastWSSCProbeActive && now-LastWSSCProbeTS < timeoutS {
		mu.Unlock()
		return
	}
	LastWSSCProbeTS = now
	LastWSSCProbeActive = true
	mu.Unlock()

	go func() {
		defer func() {
			mu.Lock()
			LastWSSCProbeActive = false
			mu.Unlock()
		}()
		end := time.Now().Add(time.Duration(timeoutS * float64(time.Second)))
		for time.Now().Before(end) {
			pid := GetActivePlayerID()
			if pid < 0 {
				time.Sleep(time.Duration(intervalS * float64(time.Second)))
				continue
			}
			res := KodiCall("Player.GetItem", map[string]interface{}{
				"playerid":   pid,
				"properties": []string{"file"},
			})
			result := jsonMap(res, "result")
			item := jsonMap(result, "item")
			if item != nil {
				fileURL := jsonStr(item, "file")
				sc := ExtractSoundcloudURL(fileURL)
				if sc != "" {
					mu.Lock()
					LastWSSCURL = sc
					mu.Unlock()
					return
				}
				trackID := ExtractSoundcloudTrackID(fileURL)
				if trackID != "" {
					link := FetchSoundcloudPermalink(trackID)
					if link != "" {
						mu.Lock()
						LastWSSCURL = link
						LastWSSCTrackID = trackID
						mu.Unlock()
						return
					}
				}
			}
			time.Sleep(time.Duration(intervalS * float64(time.Second)))
		}
	}()
}

// ── External item display ───────────────────────────────────────────
// ExternalItemDisplay is very large — see external_item_display.go
// For now it's included here:

// ExternalItemDisplay returns (displayName, link) for an externally playing item.
func ExternalItemDisplay(item map[string]interface{}) (string, string) {
	if item == nil {
		return "", ""
	}
	itype := strings.ToLower(jsonStr(item, "type"))
	title := jsonStr(item, "title")
	label := jsonStr(item, "label")
	imdbnumber := jsonStr(item, "imdbnumber")
	uniqueid := jsonMap(item, "uniqueid")
	imdbID := ""
	if uniqueid != nil {
		imdbID = jsonStr(uniqueid, "imdb")
	}
	fileURL := jsonStr(item, "file")
	showtitle := jsonStr(item, "showtitle")
	season := jsonInt(item, "season", -999)
	episode := jsonInt(item, "episode", -999)
	artist := jsonArr(item, "artist")
	album := jsonStr(item, "album")
	channel := jsonStr(item, "channel")

	link := ""

	// Check temp media title
	// Note: this requires the media package — we'll use the callback pattern
	// For now, we check via the wsOnStop pattern

	// YouTube plugin URL
	if strings.HasPrefix(fileURL, "plugin://plugin.video.youtube/") {
		ytID := ExtractYoutubeID(fileURL)
		if ytID != "" {
			link = "https://youtu.be/" + ytID
		}
	}

	ytIDFromFile := ExtractYoutubeID(fileURL)
	if ytIDFromFile != "" && strings.Contains(fileURL, "/youtube/manifest/") {
		link = "https://youtu.be/" + ytIDFromFile
	}

	scFromPlugin := ExtractSoundcloudURL(fileURL)
	if scFromPlugin != "" {
		link = scFromPlugin
	}

	if strings.HasPrefix(fileURL, "http") {
		link = fileURL
		ytID := ExtractYoutubeID(link)
		if ytID != "" {
			link = "https://youtu.be/" + ytID
		} else if IsSoundcloudStreamURL(link) {
			mu.Lock()
			scURL := LastWSSCURL
			scTrackID := LastWSSCTrackID
			mu.Unlock()
			if scURL != "" {
				link = scURL
				displayName := label
				if displayName == "" {
					displayName = title
				}
				if displayName != "" && strings.ToLower(displayName) != "playlist.m3u8" {
					return displayName, link
				}
				fallbackName := SoundcloudDisplayTitleFromURL(scURL)
				if fallbackName != "" {
					return fallbackName, link
				}
			}
			trackID := ExtractSoundcloudTrackID(fileURL)
			if scURL != "" && scTrackID != "" && trackID != "" && trackID == scTrackID {
				link = scURL
				name := label
				if name == "" {
					name = title
				}
				return name, link
			}
			ScheduleSoundcloudPermalinkProbe(2.0, 0.2)
			now := Now()
			mu.Lock()
			shouldLookup := now-LastWSSCLookupTS > 2.0
			if shouldLookup {
				LastWSSCLookupTS = now
			}
			mu.Unlock()
			if shouldLookup {
				sc := ResolveSoundcloudLinkFromKodi()
				if sc != "" {
					mu.Lock()
					LastWSSCURL = sc
					mu.Unlock()
					link = sc
					name := label
					if name == "" {
						name = title
					}
					return name, link
				}
			}
			scLink := GuessSoundcloudLink(artist, title)
			if scLink != "" {
				link = scLink
			} else {
				link = ""
			}
		} else if strings.Contains(link, "/youtube/manifest/") && (strings.Contains(link, "127.0.0.1") || strings.Contains(link, "localhost")) {
			ytID := ExtractYoutubeID(link)
			if ytID != "" {
				link = "https://youtu.be/" + ytID
			} else {
				link = ""
			}
		}
	}

	mu.Lock()
	lastYTID := LastWSYTID
	mu.Unlock()
	if link == "" && (itype == "video" || itype == "movie") && lastYTID != "" {
		if strings.Contains(fileURL, "youtube") || strings.Contains(fileURL, "manifest") {
			link = "https://youtu.be/" + lastYTID
		}
	}

	// IMDB links
	if link == "" || (!strings.Contains(link, "youtu") && !strings.Contains(link, "soundcloud")) {
		if imdbnumber != "" && IMDBIDRegex.MatchString(imdbnumber) {
			link = "https://www.imdb.com/title/" + imdbnumber + "/"
		} else if imdbID != "" && IMDBIDRegex.MatchString(imdbID) {
			link = "https://www.imdb.com/title/" + imdbID + "/"
		} else if itype == "movie" || itype == "episode" || itype == "tvshow" {
			q := showtitle
			if q == "" {
				q = title
			}
			if q == "" {
				q = label
			}
			if q != "" {
				link = "https://www.imdb.com/find?q=" + url.QueryEscape(q)
			}
		}
	}

	// Episode formatting
	if itype == "episode" {
		base := showtitle
		if base == "" {
			base = label
		}
		if base == "" {
			base = title
		}
		epTitle := title
		if base != "" {
			if season != -999 && episode != -999 {
				return fmt.Sprintf("%s S%02dE%02d – %s", base, season, episode, epTitle), link
			}
			return strings.TrimRight(base+" – "+epTitle, " –"), link
		}
	}

	if itype == "movie" {
		name := title
		if name == "" {
			name = label
		}
		if name == "" {
			name = "Unknown"
		}
		return name, link
	}

	// Artist + title
	if len(artist) > 0 && title != "" {
		var artistStrs []string
		for _, a := range artist {
			if s, ok := a.(string); ok {
				artistStrs = append(artistStrs, s)
			}
		}
		if len(artistStrs) > 0 {
			return strings.Join(artistStrs, ", ") + " - " + title, link
		}
	}

	if album != "" && title != "" {
		return album + " - " + title, link
	}

	// Channel (radio)
	if itype == "channel" && channel != "" {
		radioTitle, radioLink := ResolveRadioTitle(channel, channel)
		if link == "" && radioLink != "" {
			link = radioLink
		}
		if radioTitle != "" {
			return channel + " || " + radioTitle, link
		}
	}
	if channel != "" {
		return channel, link
	}

	// Favourite fallback
	if len(artist) == 0 && channel == "" && fileURL != "" {
		favName := FindFavouriteLabelByPath(fileURL)
		if favName != "" {
			radioTitle := FetchICYTitle(fileURL)
			if radioTitle != "" {
				ytLink := RadioTitleToYoutubeLink(radioTitle)
				if ytLink != "" {
					return favName + " || " + radioTitle, ytLink
				}
				scLink := RadioTitleToSoundcloudLink(radioTitle)
				if scLink != "" {
					return favName + " || " + radioTitle, scLink
				}
				return favName + " || " + radioTitle, link
			}
			return favName, link
		}
	}

	name := label
	if name == "" {
		name = title
	}
	return name, link
}

// KodiItemMatchesQueue checks if a Kodi item matches a queue item.
func KodiItemMatchesQueue(item, qitem map[string]interface{}) bool {
	if item == nil || qitem == nil {
		return false
	}
	itemFile := jsonStr(item, "file")
	qURL := jsonStr(qitem, "url")
	if itemFile != "" && qURL != "" && itemFile == qURL {
		return true
	}
	qLink := jsonStr(qitem, "link")
	if qLink != "" && strings.Contains(qLink, "soundcloud.com") {
		if itemFile != "" && strings.Contains(itemFile, "sndcdn") {
			return true
		}
		itemSCURL := ExtractSoundcloudURL(itemFile)
		if itemSCURL != "" && itemSCURL == qLink {
			return true
		}
		mu.Lock()
		lastSC := LastWSSCURL
		mu.Unlock()
		if lastSC != "" && lastSC == qLink && IsSoundcloudStreamURL(itemFile) {
			return true
		}
		itemTitle := jsonStr(item, "title")
		if itemTitle == "" {
			itemTitle = jsonStr(item, "label")
		}
		qSlug := SoundcloudTrackSlugFromURL(qLink)
		tSlug := SoundcloudSlug(itemTitle)
		if qSlug != "" && tSlug != "" && (qSlug == tSlug || strings.Contains(qSlug, tSlug) || strings.Contains(tSlug, qSlug)) {
			return true
		}
	}
	itemName := NormalizeTitle(KodiItemName(item))
	qTitle := NormalizeTitle(jsonStr(qitem, "title"))
	if itemName == "" || qTitle == "" {
		return false
	}
	return strings.Contains(itemName, qTitle) || strings.Contains(qTitle, itemName)
}

// FetchLibraryItem fetches details for a library item.
func FetchLibraryItem(itemType string, itemID interface{}) map[string]interface{} {
	if itemType == "" || itemID == nil {
		return nil
	}
	itype := strings.ToLower(itemType)
	switch itype {
	case "movie":
		res := KodiCallWithProps("VideoLibrary.GetMovieDetails", "movieid", itemID,
			[]string{"title", "year", "originaltitle", "uniqueid", "imdbnumber"})
		result := jsonMap(res, "result")
		return jsonMap(result, "moviedetails")
	case "episode":
		res := KodiCallWithProps("VideoLibrary.GetEpisodeDetails", "episodeid", itemID,
			[]string{"title", "showtitle", "season", "episode", "uniqueid", "imdbnumber"})
		result := jsonMap(res, "result")
		return jsonMap(result, "episodedetails")
	case "tvshow":
		res := KodiCallWithProps("VideoLibrary.GetTVShowDetails", "tvshowid", itemID,
			[]string{"title", "year", "uniqueid", "imdbnumber"})
		result := jsonMap(res, "result")
		return jsonMap(result, "tvshowdetails")
	}
	return nil
}

// BuildIMDBLink builds an IMDB link for an item.
func BuildIMDBLink(item map[string]interface{}) string {
	if item == nil {
		return ""
	}
	getLink := func(obj map[string]interface{}) string {
		imdbnumber := jsonStr(obj, "imdbnumber")
		if IMDBIDRegex.MatchString(imdbnumber) {
			return "https://www.imdb.com/title/" + imdbnumber + "/"
		}
		uniqueid := jsonMap(obj, "uniqueid")
		if uniqueid != nil {
			imdbID := jsonStr(uniqueid, "imdb")
			if IMDBIDRegex.MatchString(imdbID) {
				return "https://www.imdb.com/title/" + imdbID + "/"
			}
		}
		return ""
	}

	link := getLink(item)
	if link != "" {
		return link
	}

	episodeid := jsonInt(item, "episodeid", -1)
	if episodeid >= 0 {
		details := FetchLibraryItem("episode", episodeid)
		link = getLink(details)
		if link != "" {
			return link
		}
	}

	title := jsonStr(item, "title")
	if title == "" {
		title = jsonStr(item, "showtitle")
	}
	if title != "" {
		return "https://www.imdb.com/find?q=" + url.QueryEscape(title)
	}
	return ""
}

// runWithTimeout runs a command with a timeout and returns stdout.
func runWithTimeout(cmd *exec.Cmd, timeout time.Duration) (string, error) {
	done := make(chan error, 1)
	var out []byte
	var cmdErr error

	go func() {
		var e error
		out, e = cmd.CombinedOutput()
		done <- e
	}()

	select {
	case err := <-done:
		cmdErr = err
	case <-time.After(timeout):
		if cmd.Process != nil {
			cmd.Process.Kill()
		}
		return "", fmt.Errorf("command timed out after %v", timeout)
	}
	return string(out), cmdErr
}


