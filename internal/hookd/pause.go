package hookd

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/yasyf/captain-hook/internal/wireproto"
)

const (
	pauseFile = "pause.json"
	maxPause  = time.Hour
)

type pause struct {
	Until  int64  `json:"until"`
	Reason string `json:"reason"`
}

// activePause reads the pause `capt-hook pause` writes into the state dir.
// A missing, unreadable, expired, or over-cap pause leaves every hook running.
func activePause(now time.Time) (pause, bool) {
	dir := os.Getenv("CAPTAIN_HOOK_STATE_DIR")
	if dir == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return pause{}, false
		}
		dir = filepath.Join(home, ".claude", "state")
	}
	data, err := os.ReadFile(filepath.Join(dir, pauseFile))
	if err != nil {
		return pause{}, false
	}
	var p pause
	if json.Unmarshal(data, &p) != nil {
		return pause{}, false
	}
	remaining := time.Unix(p.Until, 0).Sub(now)
	return p, remaining > 0 && remaining <= maxPause
}

func pauseBanner(p pause) string {
	until := time.Unix(p.Until, 0).Local().Format("15:04 MST")
	reason := ""
	if p.Reason != "" {
		reason = fmt.Sprintf(" (%s)", p.Reason)
	}
	message := fmt.Sprintf(
		"capt-hook is paused until %s%s: every capt-hook hook, guards included, is a no-op until then. "+
			"Run `capt-hook resume` to lift it now; quiet hooks only through `capt-hook pause`, never by editing the plugin cache.",
		until, reason)
	envelope, err := wireproto.Marshal(map[string]any{
		"systemMessage": message,
		"hookSpecificOutput": map[string]any{
			"hookEventName":     "SessionStart",
			"additionalContext": message,
		},
	})
	if err != nil {
		panic(fmt.Sprintf("captain: encode pause banner: %v", err))
	}
	return string(envelope) + "\n"
}
