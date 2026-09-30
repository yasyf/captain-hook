package hookd

import (
	"context"
	"errors"
)

func superviseHost(context.Context) error {
	return errors.New("captain: launchd supervises the macOS host; supervise runs only on linux")
}
