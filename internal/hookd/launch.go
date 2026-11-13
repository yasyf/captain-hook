package hookd

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
)

const binrunPluginRootEnv = "BINRUN_PLUGIN_ROOT"

func recordLaunch(dir string) error {
	root := os.Getenv(binrunPluginRootEnv)
	if root == "" {
		return nil
	}
	executable, err := os.Executable()
	if err != nil {
		return fmt.Errorf("captain: resolve executable: %w", err)
	}
	descriptor, err := os.Stat(filepath.Join(root, "bin", "hook.binrun"))
	if err != nil {
		return fmt.Errorf("captain: stat hook descriptor: %w", err)
	}
	record := filepath.Join(dir, "launch", Build)
	content := []byte(executable + "\n")
	// bin/hook trusts the record only while bash -nt, which compares whole seconds, finds it newer than the descriptor.
	if info, err := os.Stat(record); err == nil && info.ModTime().Unix() > descriptor.ModTime().Unix() {
		if current, err := os.ReadFile(record); err == nil && bytes.Equal(current, content) {
			return nil
		}
	}
	if err := os.MkdirAll(filepath.Dir(record), 0o700); err != nil {
		return fmt.Errorf("captain: create launch records: %w", err)
	}
	stage, err := os.CreateTemp(filepath.Dir(record), ".launch-")
	if err != nil {
		return fmt.Errorf("captain: stage launch record: %w", err)
	}
	defer os.Remove(stage.Name())
	if _, err := stage.Write(content); err != nil {
		stage.Close()
		return fmt.Errorf("captain: write launch record: %w", err)
	}
	if err := stage.Close(); err != nil {
		return fmt.Errorf("captain: close launch record: %w", err)
	}
	if err := os.Rename(stage.Name(), record); err != nil {
		return fmt.Errorf("captain: publish launch record: %w", err)
	}
	return nil
}
