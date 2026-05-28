// Package playlist provides on-disk playlist persistence.
package playlist

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

// EnsurePlaylistDir ensures the playlist directory exists.
func EnsurePlaylistDir(path string) bool {
	if err := os.MkdirAll(path, 0755); err != nil {
		return false
	}
	return true
}

// SanitizePlaylistName sanitizes a playlist name for use as a filename.
func SanitizePlaylistName(name string) string {
	re := regexp.MustCompile(`[^A-Za-z0-9._-]+`)
	safe := re.ReplaceAllString(strings.TrimSpace(name), "_")
	safe = strings.Trim(safe, "._-")
	if safe == "" {
		return "playlist"
	}
	return safe
}

// UniquePlaylistPath returns a unique file path for a playlist.
func UniquePlaylistPath(dirPath, baseName string) string {
	base := SanitizePlaylistName(baseName)
	path := filepath.Join(dirPath, base+".json")
	if _, err := os.Stat(path); os.IsNotExist(err) {
		return path
	}
	for i := 2; i < 1000; i++ {
		path = filepath.Join(dirPath, fmt.Sprintf("%s-%d.json", base, i))
		if _, err := os.Stat(path); os.IsNotExist(err) {
			return path
		}
	}
	return filepath.Join(dirPath, fmt.Sprintf("%s-%d.json", base, time.Now().Unix()))
}

// PlaylistPathForName returns the canonical path for a playlist name.
func PlaylistPathForName(dirPath, name string) string {
	base := SanitizePlaylistName(name)
	return filepath.Join(dirPath, base+".json")
}

// SavePlaylistToDisk saves a playlist to disk with a unique filename.
func SavePlaylistToDisk(dirPath, name string, items []map[string]interface{}) (bool, string) {
	if !EnsurePlaylistDir(dirPath) {
		return false, "Playlist directory is not available."
	}
	if len(items) == 0 {
		return false, "Queue empty."
	}
	path := UniquePlaylistPath(dirPath, name)
	data := map[string]interface{}{
		"name":     name,
		"saved_at": time.Now().Format("2006-01-02T15:04:05"),
		"queue":    items,
	}
	content, err := json.MarshalIndent(data, "", "  ")
	if err != nil {
		return false, fmt.Sprintf("Save failed: %v", err)
	}
	if err := os.WriteFile(path, content, 0644); err != nil {
		return false, fmt.Sprintf("Save failed: %v", err)
	}
	return true, filepath.Base(path)
}

// SavePlaylistToDiskOverwrite saves a playlist, overwriting if it exists.
func SavePlaylistToDiskOverwrite(dirPath, name string, items []map[string]interface{}) (bool, string) {
	if !EnsurePlaylistDir(dirPath) {
		return false, "Playlist directory is not available."
	}
	if len(items) == 0 {
		return false, "Queue empty."
	}
	path := PlaylistPathForName(dirPath, name)
	data := map[string]interface{}{
		"name":     name,
		"saved_at": time.Now().Format("2006-01-02T15:04:05"),
		"queue":    items,
	}
	content, err := json.MarshalIndent(data, "", "  ")
	if err != nil {
		return false, fmt.Sprintf("Save failed: %v", err)
	}
	if err := os.WriteFile(path, content, 0644); err != nil {
		return false, fmt.Sprintf("Save failed: %v", err)
	}
	return true, filepath.Base(path)
}

// ListPlaylistFiles returns sorted list of .json files in the directory.
func ListPlaylistFiles(dirPath string) []string {
	if !EnsurePlaylistDir(dirPath) {
		return nil
	}
	entries, err := os.ReadDir(dirPath)
	if err != nil {
		return nil
	}
	var files []string
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(strings.ToLower(e.Name()), ".json") {
			files = append(files, e.Name())
		}
	}
	sort.Slice(files, func(i, j int) bool {
		return strings.ToLower(files[i]) < strings.ToLower(files[j])
	})
	return files
}

// LoadPlaylistFromDisk loads a playlist from disk.
func LoadPlaylistFromDisk(dirPath, filename string) (bool, interface{}) {
	path := filepath.Join(dirPath, filename)
	if _, err := os.Stat(path); os.IsNotExist(err) {
		return false, "Playlist file not found."
	}
	content, err := os.ReadFile(path)
	if err != nil {
		return false, fmt.Sprintf("Load failed: %v", err)
	}
	var data map[string]interface{}
	if err := json.Unmarshal(content, &data); err != nil {
		return false, fmt.Sprintf("Load failed: %v", err)
	}
	items, ok := data["queue"].([]interface{})
	if !ok {
		return false, "Invalid playlist format."
	}
	// Convert to []map[string]interface{}
	result := make([]map[string]interface{}, 0, len(items))
	for _, item := range items {
		if m, ok := item.(map[string]interface{}); ok {
			result = append(result, m)
		}
	}
	return true, result
}

// DeletePlaylistFromDisk deletes a playlist file.
func DeletePlaylistFromDisk(dirPath, filename string) (bool, string) {
	path := filepath.Join(dirPath, filename)
	if _, err := os.Stat(path); os.IsNotExist(err) {
		return false, "Playlist file not found."
	}
	if err := os.Remove(path); err != nil {
		return false, fmt.Sprintf("Delete failed: %v", err)
	}
	return true, filename
}
