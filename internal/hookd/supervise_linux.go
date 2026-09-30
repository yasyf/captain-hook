package hookd

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/yasyf/daemonkit"
	"github.com/yasyf/daemonkit/supervise"
)

const (
	supervisorAnswerInterval = 100 * time.Millisecond
	supervisorClaimTimeout   = time.Second
)

// superviseHost is the workspace-owned foreground supervisor for the host. It
// converges its own label on this build once the supervisor answers,
// so starting it is the whole deployment, and it returns when a drain signal
// or ctx ends it. The supervise lock is held for its whole life, so a second
// invocation exits before it can converge the first one's daemon.
func superviseHost(ctx context.Context) error {
	installed, err := resolveInstalledPaths()
	if err != nil {
		return err
	}
	claimCtx, cancelClaim := context.WithTimeout(ctx, supervisorClaimTimeout)
	claim, err := installed.lock(claimCtx, superviseLockName)
	cancelClaim()
	if err != nil {
		return fmt.Errorf("captain: another capt-hookd supervise owns the host: %w", err)
	}
	defer claim.Close()
	client, err := openSupervisedHost()
	if err != nil {
		return err
	}
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	supervised := make(chan error, 1)
	go func() {
		supervised <- daemonkit.Supervise(ctx, hostServiceLabel)
		cancel()
	}()
	for {
		err := ensureInstalledHost(ctx, installed, client)
		switch {
		case err == nil, ctx.Err() != nil:
			return <-supervised
		case !errors.Is(err, supervise.ErrNoSupervisor):
			cancel()
			return errors.Join(err, <-supervised)
		}
		select {
		case <-ctx.Done():
		case <-time.After(supervisorAnswerInterval):
		}
	}
}

// ensureInstalledHost converges under the install lock, so a package-install
// in flight never has its build replaced by this supervisor's.
func ensureInstalledHost(ctx context.Context, installed installedPaths, client *daemonkit.Client) error {
	lockCtx, cancel := context.WithTimeout(ctx, packageLifecycleTimeout)
	defer cancel()
	lock, err := installed.lock(lockCtx, installLockName)
	if err != nil {
		return err
	}
	defer lock.Close()
	return ensureSupervisedHost(ctx, client)
}
