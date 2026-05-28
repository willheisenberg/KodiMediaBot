// Package tv provides IPTV channel search by downloading and parsing M3U playlists.
package tv

import (
	"io"
	"log"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/willheisenberg/KodiMediaBot/internal/config"
)

// Channel represents an IPTV channel.
type Channel struct {
	Name string `json:"name"`
	URL  string `json:"url"`
	Logo string `json:"logo"`
}

// SearchTVChannels downloads M3U playlists and searches for matching channels.
func SearchTVChannels(query string, limit int) []Channel {
	cfg := config.Get()
	rawURLs := cfg.IPTVM3UURL
	if rawURLs == "" {
		log.Printf("WARN IPTV M3U URL is not configured.")
		return nil
	}

	urls := strings.Split(rawURLs, ",")
	var results []Channel
	seenURLs := make(map[string]bool)
	queryLower := strings.ToLower(strings.TrimSpace(query))
	logoRE := regexp.MustCompile(`tvg-logo="([^"]*)"`)

	for _, u := range urls {
		u = strings.TrimSpace(u)
		if u == "" {
			continue
		}

		log.Printf("INFO Downloading IPTV M3U list from: %s", u)
		client := &http.Client{Timeout: 10 * time.Second}
		resp, err := client.Get(u)
		if err != nil {
			log.Printf("ERROR Failed to fetch IPTV playlist from %s: %v", u, err)
			continue
		}
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if resp.StatusCode < 200 || resp.StatusCode >= 300 {
			log.Printf("ERROR Failed to fetch IPTV playlist from %s: status=%d", u, resp.StatusCode)
			continue
		}

		lines := strings.Split(string(body), "\n")
		for i := 0; i < len(lines); i++ {
			line := strings.TrimSpace(lines[i])
			if !strings.HasPrefix(line, "#EXTINF:") {
				continue
			}

			// Extract channel name
			var name string
			if commaIdx := strings.LastIndex(line, ","); commaIdx >= 0 {
				name = strings.TrimSpace(line[commaIdx+1:])
			} else {
				name = strings.TrimSpace(line[8:])
			}

			// Extract logo
			logo := ""
			if m := logoRE.FindStringSubmatch(line); m != nil {
				logo = m[1]
			}

			// Find stream URL
			urlLine := ""
			for j := i + 1; j < len(lines); j++ {
				candidate := strings.TrimSpace(lines[j])
				if candidate == "" {
					continue
				}
				if strings.HasPrefix(candidate, "#") {
					break
				}
				urlLine = candidate
				break
			}

			if urlLine != "" && name != "" {
				if strings.Contains(strings.ToLower(name), queryLower) && !seenURLs[urlLine] {
					seenURLs[urlLine] = true
					results = append(results, Channel{Name: name, URL: urlLine, Logo: logo})
					if len(results) >= limit {
						log.Printf("INFO Fuzzy search for '%s' reached limit of %d channels.", query, limit)
						return results
					}
				}
			}
		}
	}

	log.Printf("INFO Fuzzy search for '%s' returned %d IPTV channels across all lists.", query, len(results))
	return results
}
