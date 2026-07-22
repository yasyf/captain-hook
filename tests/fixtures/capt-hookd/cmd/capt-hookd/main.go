package main

import (
	"encoding/json"
	"os"

	"github.com/yasyf/captain-hook/internal/hookd"
)

func main() {
	if len(os.Args) != 2 || os.Args[1] != "version" {
		os.Exit(2)
	}
	if err := json.NewEncoder(os.Stdout).Encode(map[string]any{"schema": 1, "build": hookd.Build}); err != nil {
		os.Exit(1)
	}
}
