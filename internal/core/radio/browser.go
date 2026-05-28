// Package radio provides Radio Browser API integration and M3U management.
package radio

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strings"
	"time"

	"github.com/willheisenberg/KodiMediaBot/internal/config"
)

// GetAPIURL returns the configured Radio Browser API URL.
func GetAPIURL() string {
	return config.Get().RadioAPIURL
}

// SearchStations searches for radio stations by name or tag.
func SearchStations(query string, limit int) []map[string]interface{} {
	apiURL := fmt.Sprintf("%s/stations/search?name=%s&hidebroken=true&limit=%d&order=clickcount&reverse=true",
		GetAPIURL(), url.QueryEscape(query), limit)

	client := &http.Client{Timeout: 10 * time.Second}
	req, _ := http.NewRequest("GET", apiURL, nil)
	req.Header.Set("User-Agent", "KodiMediaBot/1.0")

	resp, err := client.Do(req)
	if err != nil {
		log.Printf("ERROR Radio search failed: %v", err)
		return nil
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		log.Printf("ERROR Radio search failed: status=%d", resp.StatusCode)
		return nil
	}

	var results []map[string]interface{}
	if err := json.NewDecoder(resp.Body).Decode(&results); err != nil {
		log.Printf("ERROR Radio search parse failed: %v", err)
		return nil
	}
	return results
}

// GetStationInfoByURL performs a reverse lookup to find station info by stream URL.
func GetStationInfoByURL(streamURL string) map[string]interface{} {
	apiURL := fmt.Sprintf("%s/stations/byurl?url=%s", GetAPIURL(), url.QueryEscape(streamURL))
	client := &http.Client{Timeout: 5 * time.Second}
	req, _ := http.NewRequest("GET", apiURL, nil)
	req.Header.Set("User-Agent", "KodiMediaBot/1.0")

	resp, err := client.Do(req)
	if err != nil {
		return nil
	}
	defer resp.Body.Close()

	var results []map[string]interface{}
	if err := json.NewDecoder(resp.Body).Decode(&results); err != nil || len(results) == 0 {
		return nil
	}
	return results[0]
}

// ReportClick reports a click/play to the API.
func ReportClick(stationUUID string) {
	apiURL := fmt.Sprintf("%s/url/%s", GetAPIURL(), stationUUID)
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Get(apiURL)
	if err != nil {
		log.Printf("WARN Could not report click for %s: %v", stationUUID, err)
		return
	}
	resp.Body.Close()
}

// AppendToM3U appends a station to the kodi.m3u file if not already present.
func AppendToM3U(name, streamURL, logoURL string) bool {
	path := config.Get().RadioM3UPath
	if _, err := os.Stat(path); os.IsNotExist(err) {
		os.WriteFile(path, []byte("#EXTM3U\n"), 0644)
	}

	content, err := os.ReadFile(path)
	if err != nil {
		log.Printf("ERROR Error reading M3U: %v", err)
		return false
	}
	if strings.Contains(string(content), streamURL) {
		log.Printf("INFO Station %s already in M3U.", name)
		return false
	}

	logoPart := ""
	if logoURL != "" {
		logoPart = fmt.Sprintf(` tvg-logo="%s"`, logoURL)
	}
	entry := fmt.Sprintf("#EXTINF:-1 group-title=\"Radio Browser\"%s,%s\n%s\n", logoPart, name, streamURL)

	f, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0644)
	if err != nil {
		log.Printf("ERROR Could not write to M3U: %v", err)
		return false
	}
	defer f.Close()
	if _, err := f.WriteString(entry); err != nil {
		log.Printf("ERROR Could not write to M3U: %v", err)
		return false
	}
	log.Printf("INFO Added %s to %s", name, path)
	return true
}

// M3UStation represents a station from the M3U file.
type M3UStation struct {
	Name string `json:"name"`
	URL  string `json:"url"`
	Logo string `json:"logo"`
}

// ListM3UStations returns all stations from the kodi.m3u file.
func ListM3UStations() []M3UStation {
	path := config.Get().RadioM3UPath
	if _, err := os.Stat(path); os.IsNotExist(err) {
		return nil
	}

	content, err := os.ReadFile(path)
	if err != nil {
		log.Printf("ERROR Error reading M3U for listing: %v", err)
		return nil
	}

	lines := strings.Split(string(content), "\n")
	var stations []M3UStation
	currentName := ""
	currentLogo := ""

	for _, line := range lines {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "#EXTINF:") {
			if idx := strings.Index(line, `tvg-logo="`); idx >= 0 {
				rest := line[idx+len(`tvg-logo="`):]
				if end := strings.Index(rest, `"`); end >= 0 {
					currentLogo = rest[:end]
				}
			}
			if idx := strings.LastIndex(line, ","); idx >= 0 {
				currentName = line[idx+1:]
			}
		} else if line != "" && !strings.HasPrefix(line, "#") {
			name := currentName
			if name == "" {
				name = "Unbekannt"
			}
			stations = append(stations, M3UStation{Name: name, URL: line, Logo: currentLogo})
			currentName = ""
			currentLogo = ""
		}
	}
	return stations
}

// RemoveFromM3U removes the station at the given index from kodi.m3u.
func RemoveFromM3U(index int) bool {
	path := config.Get().RadioM3UPath
	stations := ListM3UStations()
	if index < 0 || index >= len(stations) {
		return false
	}

	// Remove the station
	remaining := make([]M3UStation, 0, len(stations)-1)
	remaining = append(remaining, stations[:index]...)
	remaining = append(remaining, stations[index+1:]...)

	var buf strings.Builder
	buf.WriteString("#EXTM3U\n")
	for _, s := range remaining {
		logoPart := ""
		if s.Logo != "" {
			logoPart = fmt.Sprintf(` tvg-logo="%s"`, s.Logo)
		}
		buf.WriteString(fmt.Sprintf("#EXTINF:-1 group-title=\"Radio Browser\"%s,%s\n%s\n", logoPart, s.Name, s.URL))
	}

	if err := os.WriteFile(path, []byte(buf.String()), 0644); err != nil {
		log.Printf("ERROR Error writing M3U after removal: %v", err)
		return false
	}
	return true
}

// SortedStationNames returns station names sorted case-insensitively.
func SortedStationNames(stations []M3UStation) []string {
	names := make([]string, len(stations))
	for i, s := range stations {
		names[i] = s.Name
	}
	sort.Slice(names, func(i, j int) bool {
		return strings.ToLower(names[i]) < strings.ToLower(names[j])
	})
	return names
}
