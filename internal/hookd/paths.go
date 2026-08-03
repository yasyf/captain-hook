package hookd

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
)

const hostServiceLabel = "com.yasyf.captain-hook.host.v1"

type paths struct {
	dir               string
	log               string
	deploymentRecords string
}

func resolvePaths() (paths, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return paths{}, fmt.Errorf("captain: resolve user home: %w", err)
	}
	cache := filepath.Join(home, ".cache")
	if runtime.GOOS == "darwin" {
		cache = filepath.Join(home, "Library", "Caches")
	}
	dir := filepath.Join(cache, "captain-hook", "host-v1")
	return paths{
		dir:               dir,
		log:               filepath.Join(dir, "capt-hookd.log"),
		deploymentRecords: filepath.Join(dir, "deployment.records"),
	}, nil
}

func (p paths) ensure() error {
	if info, err := os.Lstat(p.dir); err == nil {
		if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
			return errors.New("captain: host state path is not a real directory")
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("captain: inspect host state: %w", err)
	} else if err := os.MkdirAll(p.dir, 0o700); err != nil {
		return fmt.Errorf("captain: create host state: %w", err)
	}
	if err := os.Chmod(p.dir, 0o700); err != nil {
		return fmt.Errorf("captain: secure host state: %w", err)
	}
	return nil
}
