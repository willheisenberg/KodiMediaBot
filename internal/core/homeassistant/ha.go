// Package homeassistant provides Home Assistant REST API integration for light control.
package homeassistant

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/willheisenberg/KodiMediaBot/internal/config"
)

const haTimeout = 8 * time.Second

// HAAvailable returns true when Home Assistant integration is configured.
func HAAvailable() bool {
	cfg := config.Get()
	return cfg.HAHost != "" && cfg.HAToken != "" && cfg.HALightID != ""
}

// ResolveHAWebappURL returns the HTTPS URL for the HA color Mini App, or "".
func ResolveHAWebappURL() string {
	cfg := config.Get()
	explicit := strings.TrimRight(strings.TrimSpace(cfg.HAWebappURL), "/")
	if explicit != "" {
		if strings.HasPrefix(explicit, "https://") {
			return explicit
		}
		return ""
	}
	baseURL := strings.TrimRight(cfg.ResolveMediaBaseURL(), "/")
	if !strings.HasPrefix(baseURL, "https://") {
		return ""
	}
	return baseURL + "/app/ha-color"
}

// HAWebappAvailable returns true when the HA Mini App can be launched.
func HAWebappAvailable() bool {
	return HAAvailable() && ResolveHAWebappURL() != ""
}

func headers() map[string]string {
	cfg := config.Get()
	return map[string]string{
		"Authorization": "Bearer " + cfg.HAToken,
		"Content-Type":  "application/json",
	}
}

func haURL(path string) string {
	cfg := config.Get()
	return cfg.HABaseURL() + path
}

func haRequest(method, path string, body interface{}) (*http.Response, error) {
	var bodyReader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		bodyReader = bytes.NewReader(data)
	}
	req, err := http.NewRequest(method, haURL(path), bodyReader)
	if err != nil {
		return nil, err
	}
	for k, v := range headers() {
		req.Header.Set(k, v)
	}
	client := &http.Client{Timeout: haTimeout}
	return client.Do(req)
}

// BrightnessPercentFromHA converts HA brightness (0-255) to percent.
func BrightnessPercentFromHA(brightness interface{}) *int {
	if brightness == nil {
		return nil
	}
	var raw int
	switch v := brightness.(type) {
	case float64:
		raw = int(v)
	case int:
		raw = v
	default:
		return nil
	}
	if raw < 0 {
		raw = 0
	}
	if raw > 255 {
		raw = 255
	}
	pct := int(float64(raw) / 255.0 * 100.0 + 0.5)
	return &pct
}

// BrightnessPercentToHA converts percent brightness (0-100) to HA's 0-255 scale.
func BrightnessPercentToHA(percent interface{}) *int {
	if percent == nil {
		return nil
	}
	var pct int
	switch v := percent.(type) {
	case float64:
		pct = int(v)
	case int:
		pct = v
	default:
		return nil
	}
	if pct < 0 {
		pct = 0
	}
	if pct > 100 {
		pct = 100
	}
	if pct <= 0 {
		zero := 0
		return &zero
	}
	val := int(float64(pct) / 100.0 * 255.0 + 0.5)
	if val < 1 {
		val = 1
	}
	if val > 255 {
		val = 255
	}
	return &val
}

// GetLightState fetches the current state of the configured light entity.
func GetLightState() map[string]interface{} {
	if !HAAvailable() {
		return nil
	}
	cfg := config.Get()
	resp, err := haRequest("GET", "/api/states/"+cfg.HALightID, nil)
	if err != nil {
		log.Printf("WARN HA get_light_state fail entity=%s err=%v", cfg.HALightID, err)
		return nil
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		log.Printf("WARN HA get_light_state fail entity=%s status=%d", cfg.HALightID, resp.StatusCode)
		return nil
	}
	var data map[string]interface{}
	if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
		return nil
	}
	attrs, _ := data["attributes"].(map[string]interface{})
	if attrs == nil {
		attrs = map[string]interface{}{}
	}
	friendlyName, _ := attrs["friendly_name"].(string)
	if friendlyName == "" {
		friendlyName = cfg.HALightID
	}
	return map[string]interface{}{
		"state":         data["state"],
		"rgb_color":     attrs["rgb_color"],
		"brightness":    attrs["brightness"],
		"friendly_name": friendlyName,
	}
}

// ToggleLight toggles the light on/off. Returns (success, newState).
func ToggleLight() (bool, string) {
	if !HAAvailable() {
		return false, "not configured"
	}
	cfg := config.Get()
	state := GetLightState()
	current := "off"
	if state != nil {
		if s, ok := state["state"].(string); ok {
			current = s
		}
	}
	service := "turn_on"
	if current == "on" {
		service = "turn_off"
	}
	resp, err := haRequest("POST", "/api/services/light/"+service,
		map[string]interface{}{"entity_id": cfg.HALightID})
	if err != nil {
		log.Printf("WARN HA toggle_light fail entity=%s err=%v", cfg.HALightID, err)
		return false, "error"
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return false, "error"
	}
	newState := "on"
	if service == "turn_off" {
		newState = "off"
	}
	log.Printf("INFO HA toggle entity=%s %s -> %s", cfg.HALightID, current, newState)
	return true, newState
}

// SetLightColor sets the light to the given RGB color.
func SetLightColor(r, g, b int, brightnessPct *int) bool {
	if !HAAvailable() {
		return false
	}
	cfg := config.Get()
	payload := map[string]interface{}{
		"entity_id": cfg.HALightID,
		"rgb_color": []int{r, g, b},
	}
	if brightnessPct != nil {
		brightness := BrightnessPercentToHA(*brightnessPct)
		if brightness == nil {
			return false
		}
		if *brightness <= 0 {
			resp, err := haRequest("POST", "/api/services/light/turn_off",
				map[string]interface{}{"entity_id": cfg.HALightID})
			if err != nil {
				log.Printf("WARN HA set_color fail entity=%s err=%v", cfg.HALightID, err)
				return false
			}
			resp.Body.Close()
			return resp.StatusCode >= 200 && resp.StatusCode < 300
		}
		payload["brightness"] = *brightness
	}
	resp, err := haRequest("POST", "/api/services/light/turn_on", payload)
	if err != nil {
		log.Printf("WARN HA set_color fail entity=%s err=%v", cfg.HALightID, err)
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode >= 200 && resp.StatusCode < 300
}

// SetLightBrightness sets light brightness in percent. 0 turns the light off.
func SetLightBrightness(percent int) bool {
	if !HAAvailable() {
		return false
	}
	cfg := config.Get()
	brightness := BrightnessPercentToHA(percent)
	if brightness == nil {
		return false
	}
	if *brightness <= 0 {
		resp, err := haRequest("POST", "/api/services/light/turn_off",
			map[string]interface{}{"entity_id": cfg.HALightID})
		if err != nil {
			log.Printf("WARN HA set_brightness fail entity=%s err=%v", cfg.HALightID, err)
			return false
		}
		resp.Body.Close()
		return resp.StatusCode >= 200 && resp.StatusCode < 300
	}
	resp, err := haRequest("POST", "/api/services/light/turn_on",
		map[string]interface{}{"entity_id": cfg.HALightID, "brightness": *brightness})
	if err != nil {
		log.Printf("WARN HA set_brightness fail entity=%s err=%v", cfg.HALightID, err)
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode >= 200 && resp.StatusCode < 300
}

// SetLightEffect enables a named HA light effect.
func SetLightEffect(effectName string) bool {
	if !HAAvailable() {
		return false
	}
	effectName = strings.TrimSpace(effectName)
	if effectName == "" {
		return false
	}
	cfg := config.Get()
	resp, err := haRequest("POST", "/api/services/light/turn_on",
		map[string]interface{}{"entity_id": cfg.HALightID, "effect": effectName})
	if err != nil {
		log.Printf("WARN HA set_effect fail entity=%s effect=%s err=%v", cfg.HALightID, effectName, err)
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode >= 200 && resp.StatusCode < 300
}

// ── Saved colors persistence ────────────────────────────────────────

func loadColorsFile() []map[string]interface{} {
	cfg := config.Get()
	data, err := os.ReadFile(cfg.HAColorsFile)
	if err != nil {
		return nil
	}
	var colors []map[string]interface{}
	if err := json.Unmarshal(data, &colors); err != nil {
		log.Printf("WARN HA load colors fail file=%s err=%v", cfg.HAColorsFile, err)
		return nil
	}
	return colors
}

func saveColorsFile(colors []map[string]interface{}) bool {
	cfg := config.Get()
	dir := filepath.Dir(cfg.HAColorsFile)
	if dir != "" {
		os.MkdirAll(dir, 0755)
	}
	data, err := json.MarshalIndent(colors, "", "  ")
	if err != nil {
		log.Printf("WARN HA save colors fail file=%s err=%v", cfg.HAColorsFile, err)
		return false
	}
	tmp := cfg.HAColorsFile + ".tmp"
	if err := os.WriteFile(tmp, data, 0644); err != nil {
		log.Printf("WARN HA save colors fail file=%s err=%v", cfg.HAColorsFile, err)
		return false
	}
	if err := os.Rename(tmp, cfg.HAColorsFile); err != nil {
		log.Printf("WARN HA save colors fail file=%s err=%v", cfg.HAColorsFile, err)
		return false
	}
	return true
}

// LoadSavedColors returns the list of saved colors.
func LoadSavedColors() []map[string]interface{} {
	colors := loadColorsFile()
	if colors == nil {
		return []map[string]interface{}{}
	}
	return colors
}

// SaveColor saves a named color to the JSON file.
func SaveColor(name string, r, g, b int) bool {
	colors := loadColorsFile()
	if colors == nil {
		colors = []map[string]interface{}{}
	}
	nameLower := strings.ToLower(name)
	var filtered []map[string]interface{}
	for _, c := range colors {
		cName, _ := c["name"].(string)
		if strings.ToLower(cName) != nameLower {
			filtered = append(filtered, c)
		}
	}
	filtered = append(filtered, map[string]interface{}{
		"name": name, "r": r, "g": g, "b": b,
	})
	return saveColorsFile(filtered)
}

// DeleteSavedColor deletes a saved color by name.
func DeleteSavedColor(name string) bool {
	colors := loadColorsFile()
	if colors == nil {
		return false
	}
	nameLower := strings.ToLower(name)
	var filtered []map[string]interface{}
	for _, c := range colors {
		cName, _ := c["name"].(string)
		if strings.ToLower(cName) != nameLower {
			filtered = append(filtered, c)
		}
	}
	if len(filtered) == len(colors) {
		return false
	}
	return saveColorsFile(filtered)
}

// ParseHexColor parses a hex color string like "#FF5500" or "FF5500".
func ParseHexColor(text string) (int, int, int, bool) {
	text = strings.TrimSpace(strings.TrimPrefix(text, "#"))
	if len(text) != 6 {
		return 0, 0, 0, false
	}
	r, err1 := parseHexByte(text[0:2])
	g, err2 := parseHexByte(text[2:4])
	b, err3 := parseHexByte(text[4:6])
	if err1 != nil || err2 != nil || err3 != nil {
		return 0, 0, 0, false
	}
	return r, g, b, true
}

func parseHexByte(s string) (int, error) {
	var v int
	_, err := fmt.Sscanf(s, "%x", &v)
	return v, err
}


