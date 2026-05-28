package kodiapi

import (
	"fmt"
	"log"
	"net/http"
	"os/exec"
	"regexp"
	"strings"
	"time"

	"github.com/willheisenberg/KodiMediaBot/internal/config"
)

// RunCECVolume sends CEC volume commands via SSH.
func RunCECVolume(times int, cmdHex string) bool {
	cfg := config.Get()
	host := cfg.CECHost
	ssh := fmt.Sprintf("ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@%s", host)
	cmd := fmt.Sprintf("%s seq %d | %s xargs -Iz cec-ctl --user-control-pressed ui-cmd=%s -t5", ssh, times, ssh, cmdHex)

	out, err := exec.Command("bash", "-c", cmd).CombinedOutput()
	if err != nil {
		log.Printf("WARN CEC command failed: err=%v stderr=%s", err, strings.TrimSpace(string(out)))
		return false
	}
	return true
}

// RunDenonVolumeDelta adjusts Denon volume by the given number of points.
func RunDenonVolumeDelta(points int) bool {
	cfg := config.Get()
	if cfg.DenonHost == "" {
		return false
	}
	if points == 0 {
		return true
	}
	cmd := "MVUP"
	if points < 0 {
		cmd = "MVDOWN"
	}
	steps := abs(points) * cfg.DenonVolumeStepCommands
	urlStr := fmt.Sprintf("http://%s/goform/formiPhoneAppDirect.xml?%s", cfg.DenonHost, cmd)

	client := &http.Client{Timeout: 4 * time.Second}
	for i := 0; i < steps; i++ {
		resp, err := client.Get(urlStr)
		if err != nil {
			log.Printf("WARN Denon volume error: host=%s points=%d err=%v", cfg.DenonHost, points, err)
			return false
		}
		resp.Body.Close()
		if resp.StatusCode != 200 {
			log.Printf("WARN Denon volume failed: status=%d host=%s points=%d cmd=%s", resp.StatusCode, cfg.DenonHost, points, cmd)
			return false
		}
		time.Sleep(50 * time.Millisecond)
	}
	return true
}

// RunVolumeDelta adjusts volume via Denon (if configured) or CEC.
func RunVolumeDelta(points int) bool {
	cfg := config.Get()
	if cfg.DenonHost != "" {
		return RunDenonVolumeDelta(points)
	}
	if points == 0 {
		return true
	}
	cmdHex := CECCmdVolUp
	if points < 0 {
		cmdHex = CECCmdVolDown
	}
	times := abs(points) * 2
	return RunCECVolume(times, cmdHex)
}

// RunDenonPower controls Denon power via HTTP.
func RunDenonPower(on bool) bool {
	cfg := config.Get()
	if cfg.DenonHost == "" {
		return false
	}
	action := "PowerOn"
	if !on {
		action = "PowerStandby"
	}
	urlStr := fmt.Sprintf("http://%s/goform/formiPhoneAppPower.xml?1+%s", cfg.DenonHost, action)
	client := &http.Client{Timeout: 4 * time.Second}
	resp, err := client.Get(urlStr)
	if err != nil {
		log.Printf("WARN Denon power error: host=%s action=%s err=%v", cfg.DenonHost, action, err)
		return false
	}
	resp.Body.Close()
	if resp.StatusCode != 200 {
		log.Printf("WARN Denon power failed: status=%d host=%s action=%s", resp.StatusCode, cfg.DenonHost, action)
		return false
	}
	return true
}

// RunCECPower controls power via Denon (with CEC fallback).
func RunCECPower(on bool) bool {
	cfg := config.Get()
	if cfg.DenonHost != "" {
		if RunDenonPower(on) {
			return true
		}
		log.Printf("INFO Denon power command failed, falling back to CEC")
	}
	host := cfg.CECHost
	ssh := fmt.Sprintf("ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@%s", host)
	var cmd string
	if on {
		cmd = fmt.Sprintf("%s cec-ctl --user-control-pressed ui-cmd=power-on-function -t0 && %s cec-ctl --user-control-pressed ui-cmd=power-on-function -t5", ssh, ssh)
	} else {
		cmd = fmt.Sprintf("%s cec-ctl --standby -t0 && %s cec-ctl --standby -t5", ssh, ssh)
	}

	out, err := exec.Command("bash", "-c", cmd).CombinedOutput()
	if err != nil {
		log.Printf("WARN CEC command failed: rc=%v stderr=%s", err, strings.TrimSpace(string(out)))
		return false
	}
	return true
}

// RunAirplayKill sends a CEC active source command to switch from AirPlay.
func RunAirplayKill() bool {
	cfg := config.Get()
	host := cfg.CECHost
	ssh := fmt.Sprintf("ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@%s", host)
	cmd := fmt.Sprintf("%s cec-ctl --active-source phys-addr=1.5.0.0 -t0", ssh)

	out, err := exec.Command("bash", "-c", cmd).CombinedOutput()
	if err != nil {
		log.Printf("WARN CEC command failed: rc=%v stderr=%s", err, strings.TrimSpace(string(out)))
		return false
	}
	return true
}

// GetHiFiPowerStatus returns the HiFi power status ("On", "Standby", or "").
func GetHiFiPowerStatus() string {
	cfg := config.Get()
	if cfg.DenonHost != "" {
		urlStr := fmt.Sprintf("http://%s/goform/formMainZone_MainZoneXml.xml", cfg.DenonHost)
		client := &http.Client{Timeout: 4 * time.Second}
		resp, err := client.Get(urlStr)
		if err != nil {
			log.Printf("WARN Denon power status error: host=%s err=%v", cfg.DenonHost, err)
			return ""
		}
		defer resp.Body.Close()
		if resp.StatusCode != 200 {
			log.Printf("WARN Denon power status failed: status=%d host=%s", resp.StatusCode, cfg.DenonHost)
			return ""
		}
		body, _ := readBody(resp)
		re := regexp.MustCompile(`(?i)<Power>\s*<value>\s*(ON|OFF|STANDBY)\s*</value>\s*</Power>`)
		m := re.FindStringSubmatch(body)
		if m == nil {
			return ""
		}
		state := strings.ToUpper(m[1])
		if state == "ON" {
			return "On"
		}
		if state == "OFF" || state == "STANDBY" {
			return "Standby"
		}
		return ""
	}

	// CEC fallback
	host := cfg.CECHost
	ssh := fmt.Sprintf("ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@%s", host)
	cmd := fmt.Sprintf("%s cec-ctl --show-topology | awk '/Audio System/ {f=1} f && /Power Status/ {print $NF; exit}'", ssh)

	out, err := exec.Command("bash", "-c", cmd).CombinedOutput()
	if err != nil {
		log.Printf("WARN CEC command failed: err=%v stderr=%s", err, strings.TrimSpace(string(out)))
		return ""
	}
	val := strings.TrimSpace(string(out))
	if val == "On" || val == "Standby" {
		return val
	}
	return ""
}

// GetAirplayStatus returns the AirPlay status ("On", "Off", or "").
func GetAirplayStatus() string {
	cfg := config.Get()
	if cfg.DenonHost == "" {
		return ""
	}
	urlStr := fmt.Sprintf("http://%s/goform/formNetAudio_StatusXml.xml", cfg.DenonHost)
	client := &http.Client{Timeout: 4 * time.Second}
	resp, err := client.Get(urlStr)
	if err != nil {
		log.Printf("WARN AirPlay error: host=%s err=%v", cfg.DenonHost, err)
		return ""
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		log.Printf("WARN AirPlay failed: status=%d host=%s", resp.StatusCode, cfg.DenonHost)
		return ""
	}
	body, _ := readBody(resp)
	szLineRE := regexp.MustCompile(`(?is)<szLine>(.*?)</szLine>`)
	m := szLineRE.FindStringSubmatch(body)
	if m == nil {
		return ""
	}
	valueRE := regexp.MustCompile(`(?is)<value>(.*?)</value>`)
	values := valueRE.FindAllStringSubmatch(m[1], -1)
	line1 := ""
	line2 := ""
	if len(values) >= 1 {
		line1 = strings.TrimSpace(values[0][1])
	}
	if len(values) >= 2 {
		line2 = strings.TrimSpace(values[1][1])
	}
	if line1 == "Now Playing" && line2 == "AirPlay" {
		return "On"
	}
	return "Off"
}

// GetDenonMainzoneVolume returns the Denon main zone volume as a string, or "".
func GetDenonMainzoneVolume() string {
	cfg := config.Get()
	if cfg.DenonHost == "" {
		return ""
	}
	urlStr := fmt.Sprintf("http://%s/goform/formMainZone_MainZoneXml.xml", cfg.DenonHost)
	client := &http.Client{Timeout: 4 * time.Second}
	resp, err := client.Get(urlStr)
	if err != nil {
		log.Printf("WARN Denon volume error: host=%s err=%v", cfg.DenonHost, err)
		return ""
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		log.Printf("WARN Denon volume failed: status=%d host=%s", resp.StatusCode, cfg.DenonHost)
		return ""
	}
	body, _ := readBody(resp)

	masterVolRE := regexp.MustCompile(`(?i)<MasterVolume>\s*<value>\s*([+-]?\d+(?:\.\d+)?)\s*</value>`)
	m := masterVolRE.FindStringSubmatch(body)
	if m != nil {
		return m[1]
	}
	valueRE := regexp.MustCompile(`(?i)<value>\s*([+-]?\d+(?:\.\d+)?)\s*</value>`)
	values := valueRE.FindAllStringSubmatch(body, -1)
	if len(values) == 0 {
		return ""
	}
	for _, v := range values {
		if strings.HasPrefix(v[1], "-") {
			return v[1]
		}
	}
	return values[0][1]
}

func readBody(resp *http.Response) (string, error) {
	body := make([]byte, 0, 16384)
	buf := make([]byte, 4096)
	for {
		n, err := resp.Body.Read(buf)
		if n > 0 {
			body = append(body, buf[:n]...)
		}
		if err != nil {
			break
		}
	}
	return string(body), nil
}

func abs(x int) int {
	if x < 0 {
		return -x
	}
	return x
}
