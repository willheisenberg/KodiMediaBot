package kodiapi

import (
	"fmt"
	"log"
	"os/exec"
	"strconv"
	"strings"

	"github.com/willheisenberg/KodiMediaBot/internal/config"
)

// ScanVideoLibrary triggers a video library scan in Kodi.
func ScanVideoLibrary() bool {
	res := KodiCall("VideoLibrary.Scan", nil)
	if _, hasErr := res["error"]; hasErr {
		log.Printf("WARN Video library scan failed: error=%v", res["error"])
		return false
	}
	log.Printf("INFO Video library scan ok: res=%v", res)
	return true
}

// GetCTimesViaSSH fetches file creation times via SSH.
func GetCTimesViaSSH(files []string) map[string]int64 {
	cfg := config.Get()
	if len(files) == 0 || cfg.CECHost == "" {
		return map[string]int64{}
	}

	var fileList strings.Builder
	for _, f := range files {
		if f != "" {
			fileList.WriteString(f)
			fileList.WriteString("\n")
		}
	}

	script := `import os, sys
for f in sys.stdin.read().splitlines():
    if not f or not os.path.exists(f): continue
    try:
        st = os.stat(f)
        ct = st.st_ctime
        if os.path.isdir(f):
            for root, dirs, files in os.walk(f):
                for name in dirs + files:
                    try: ct = max(ct, os.stat(os.path.join(root, name)).st_ctime)
                    except: pass
        print(f'{f}|{int(ct)}')
    except: pass
`
	remoteCmd := fmt.Sprintf("python3 -c %s", shellQuote(script))
	sshCmd := fmt.Sprintf("ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@%s %s",
		cfg.CECHost, shellQuote(remoteCmd))

	cmd := exec.Command("bash", "-c", sshCmd)
	cmd.Stdin = strings.NewReader(fileList.String())
	out, err := cmd.CombinedOutput()
	if err != nil && len(out) == 0 {
		log.Printf("WARN SSH ctime fetch failed: %v", err)
		return map[string]int64{}
	}

	ctimeMap := make(map[string]int64)
	for _, line := range strings.Split(string(out), "\n") {
		parts := strings.SplitN(line, "|", 2)
		if len(parts) == 2 {
			ts, err := strconv.ParseInt(strings.TrimSpace(parts[1]), 10, 64)
			if err == nil {
				ctimeMap[parts[0]] = ts
			}
		}
	}
	return ctimeMap
}

// ListMovies returns all movies from the Kodi library.
func ListMovies() []map[string]interface{} {
	res := KodiCall("VideoLibrary.GetMovies", map[string]interface{}{
		"properties": []string{"title", "year", "originaltitle", "uniqueid", "imdbnumber", "dateadded", "file"},
		"sort":       map[string]interface{}{"method": "title"},
	})
	result := jsonMap(res, "result")
	if result == nil {
		return nil
	}
	movies := jsonArr(result, "movies")
	if movies == nil {
		return nil
	}

	out := make([]map[string]interface{}, 0, len(movies))
	var files []string
	for _, m := range movies {
		mm, ok := m.(map[string]interface{})
		if !ok {
			continue
		}
		out = append(out, mm)
		f := jsonStr(mm, "file")
		if f != "" {
			files = append(files, f)
		}
	}

	ctimeMap := GetCTimesViaSSH(files)
	for _, m := range out {
		f := jsonStr(m, "file")
		if f != "" {
			if ct, ok := ctimeMap[f]; ok {
				m["ctime"] = ct
			}
		}
	}
	return out
}

// ListTVShows returns all TV shows from the Kodi library.
func ListTVShows() []map[string]interface{} {
	res := KodiCall("VideoLibrary.GetTVShows", map[string]interface{}{
		"properties": []string{"title", "year", "uniqueid", "imdbnumber", "dateadded", "file"},
		"sort":       map[string]interface{}{"method": "title"},
	})
	result := jsonMap(res, "result")
	if result == nil {
		return nil
	}
	shows := jsonArr(result, "tvshows")
	if shows == nil {
		return nil
	}

	out := make([]map[string]interface{}, 0, len(shows))
	var files []string
	for _, s := range shows {
		sm, ok := s.(map[string]interface{})
		if !ok {
			continue
		}
		out = append(out, sm)
		f := jsonStr(sm, "file")
		if f != "" {
			files = append(files, f)
		}
	}

	if len(files) > 0 {
		ctimeMap := GetCTimesViaSSH(files)
		for _, s := range out {
			f := jsonStr(s, "file")
			if f != "" {
				if ct, ok := ctimeMap[f]; ok {
					s["ctime"] = ct
				}
			}
		}
	}
	return out
}

// ListTVShowEpisodes returns episodes for a TV show.
func ListTVShowEpisodes(tvshowID *int, showtitle string) []map[string]interface{} {
	var attempts []map[string]interface{}

	if tvshowID != nil {
		attempts = append(attempts,
			map[string]interface{}{
				"tvshowid":   *tvshowID,
				"properties": []string{"title", "showtitle", "season", "episode", "uniqueid", "imdbnumber", "dateadded", "file"},
				"sort":       map[string]interface{}{"method": "episode"},
			},
			map[string]interface{}{
				"tvshowid":   *tvshowID,
				"properties": []string{"title", "showtitle", "season", "episode", "uniqueid", "imdbnumber", "dateadded", "file"},
			},
			map[string]interface{}{
				"tvshowid":   *tvshowID,
				"properties": []string{"title", "showtitle", "season", "episode", "dateadded", "file"},
			},
		)
	}
	if showtitle != "" {
		attempts = append(attempts,
			map[string]interface{}{
				"properties": []string{"title", "showtitle", "season", "episode", "uniqueid", "imdbnumber", "dateadded", "file"},
				"sort":       map[string]interface{}{"method": "episode"},
			},
			map[string]interface{}{
				"properties": []string{"title", "showtitle", "season", "episode", "dateadded", "file"},
			},
		)
	}

	want := NormalizeTitle(showtitle)
	for _, params := range attempts {
		res := KodiCall("VideoLibrary.GetEpisodes", params)
		result := jsonMap(res, "result")
		if result == nil {
			continue
		}
		episodes := jsonArr(result, "episodes")
		if episodes == nil {
			continue
		}

		var out []map[string]interface{}
		_, hasTVShowID := params["tvshowid"]
		for _, ep := range episodes {
			epm, ok := ep.(map[string]interface{})
			if !ok {
				continue
			}
			if want != "" && !hasTVShowID {
				epShow := NormalizeTitle(jsonStr(epm, "showtitle"))
				if epShow != want {
					continue
				}
			}
			out = append(out, epm)
		}

		if len(out) > 0 {
			var files []string
			for _, e := range out {
				f := jsonStr(e, "file")
				if f != "" {
					files = append(files, f)
				}
			}
			ctimeMap := GetCTimesViaSSH(files)
			for _, e := range out {
				f := jsonStr(e, "file")
				if f != "" {
					if ct, ok := ctimeMap[f]; ok {
						e["ctime"] = ct
					}
				}
			}
			return out
		}
	}
	return nil
}

// PlayMovie plays a movie by its library ID.
func PlayMovie(movieID int, resume bool) bool {
	StopPlayerAndClearPlaylists()
	res := KodiCall("Player.Open", map[string]interface{}{
		"item":    map[string]interface{}{"movieid": movieID},
		"options": map[string]interface{}{"resume": resume},
	})
	_, hasErr := res["error"]
	return !hasErr
}

// PlayEpisode plays an episode by its library ID.
func PlayEpisode(episodeID int, resume bool) bool {
	StopPlayerAndClearPlaylists()
	res := KodiCall("Player.Open", map[string]interface{}{
		"item":    map[string]interface{}{"episodeid": episodeID},
		"options": map[string]interface{}{"resume": resume},
	})
	_, hasErr := res["error"]
	return !hasErr
}

// PlayAllEpisodes queues and plays all episodes.
func PlayAllEpisodes(episodeIDs []int) bool {
	if len(episodeIDs) == 0 {
		return false
	}
	StopPlayerAndClearPlaylists()
	for _, eid := range episodeIDs {
		res := KodiCall("Playlist.Add", map[string]interface{}{
			"playlistid": 1,
			"item":       map[string]interface{}{"episodeid": eid},
		})
		if _, hasErr := res["error"]; hasErr {
			return false
		}
	}
	res := KodiCall("Player.Open", map[string]interface{}{
		"item": map[string]interface{}{"playlistid": 1, "position": 0},
	})
	_, hasErr := res["error"]
	return !hasErr
}

func shellQuote(s string) string {
	return "'" + strings.ReplaceAll(s, "'", "'\\''") + "'"
}
