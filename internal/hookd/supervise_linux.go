package hookd

import (
	"context"
	"errors"
	"time"

	"github.com/yasyf/daemonkit"
	"github.com/yasyf/daemonkit/supervise"
)

const supervisorAnswerInterval = 100 * time.Millisecond

// superviseHost is the workspace-owned foreground supervisor for the host. It
// converges its own label on this build once the supervisor answers,
// so starting it is the whole deployment, and it returns when a drain signal
// or ctx ends it.
func superviseHost(ctx context.Context) error {
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
		err := ensureSupervisedHost(ctx, client)
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
