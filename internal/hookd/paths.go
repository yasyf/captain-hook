package hookd

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
)

const (
	businessRoleID              = "com.yasyf.captain-hook.business.v1"
	lifecycleRoleID             = "com.yasyf.captain-hook.lifecycle.v1"
	stopControlRoleID           = "com.yasyf.captain-hook.stop-control.v1"
	helperConsumerRoleID        = "com.yasyf.captain-hook.helper.consumer.v1"
	helperBrokerLifecycleRoleID = "com.yasyf.captain-hook.helper.broker-lifecycle.v1"
	helperBrokerHandoffRoleID   = "com.yasyf.captain-hook.helper.broker-handoff.v1"
	helperClientRoleID          = "com.yasyf.captain-hook.helper.client.v1"
	hostServiceLabel            = "com.yasyf.captain-hook.host.v1"
)

type paths struct {
	dir           string
	socket        string
	startLock     string
	processes     string
	stopState     string
	stopProcesses string
	log           string
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
		dir: dir, socket: filepath.Join(dir, "capt-hookd.sock"),
		startLock:     filepath.Join(dir, "start.lock"),
		processes:     filepath.Join(dir, "workers.json"),
		stopState:     filepath.Join(dir, "stop-controller.db"),
		stopProcesses: filepath.Join(dir, "stop-processes.db"),
		log:           filepath.Join(dir, "capt-hookd.log"),
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
