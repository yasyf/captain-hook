package hookd

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/yasyf/captain-hook/internal/wireproto"
	"github.com/yasyf/daemonkit"
)

const closeTimeout = 5 * time.Second

// Client owns one exact persistent product session.
type Client struct {
	daemon   *daemonkit.Client
	business *daemonkit.Business
}

// NewClient returns a lazy client for the only host schema and build.
func NewClient() (*Client, error) {
	return openClient(hostDaemon())
}

func openClient(d daemonkit.Daemon) (*Client, error) {
	daemon, err := daemonkit.Open(d)
	if err != nil {
		return nil, fmt.Errorf("captain: open signed host: %w", err)
	}
	return &Client{daemon: daemon, business: daemon.Business()}, nil
}

// Close settles the product session.
func (c *Client) Close() error {
	return closeLane(c.business)
}

func closeLane(lane *daemonkit.Business) error {
	ctx, cancel := context.WithTimeout(context.Background(), closeTimeout)
	defer cancel()
	return lane.Close(ctx)
}

// Event dispatches exactly once. A host that is absent, starting, or draining
// refused the event before dispatch, so it is sent once more, on a fresh
// session, after a host is ready within the same deadline.
func (c *Client) Event(ctx context.Context, request wireproto.EventRequest) (wireproto.EventResponse, error) {
	payload, err := wireproto.MarshalEventRequest(request)
	if err != nil {
		return wireproto.EventResponse{}, err
	}
	result, err := c.call(ctx, opEvent, payload)
	if betweenGenerations(err) {
		if _, waitErr := c.daemon.WaitReady(ctx); waitErr != nil {
			return wireproto.EventResponse{}, errors.Join(err, waitErr)
		}
		stale := c.business
		c.business = c.daemon.Business()
		result, err = c.call(ctx, opEvent, payload)
		_ = closeLane(stale)
	}
	if err != nil {
		return wireproto.EventResponse{}, err
	}
	var response wireproto.EventResponse
	if err := decodeStrict(result, &response); err != nil {
		return wireproto.EventResponse{}, fmt.Errorf("captain: decode event response: %w", err)
	}
	if err := response.Validate(); err != nil {
		return wireproto.EventResponse{}, err
	}
	return response, nil
}

// Status returns the live host and worker generations without spawning.
func (c *Client) Status(ctx context.Context) (statusResponse, error) {
	result, err := c.call(ctx, opStatus, nil)
	if err != nil {
		return statusResponse{}, err
	}
	var response statusResponse
	if err := decodeStrict(result, &response); err != nil {
		return statusResponse{}, fmt.Errorf("captain: decode status response: %w", err)
	}
	return response, nil
}

// RestartWorkers kills and reaps every product worker generation.
func (c *Client) RestartWorkers(ctx context.Context) error {
	payload, err := wireproto.Marshal(restartWorkersRequest{Schema: wireproto.Schema, Build: Build})
	if err != nil {
		return err
	}
	_, err = c.call(ctx, opRestartWorkers, payload)
	return err
}

func betweenGenerations(err error) bool {
	return daemonkit.Undispatched(err) && (errors.Is(err, daemonkit.ErrAbsent) ||
		errors.Is(err, daemonkit.ErrDraining) || errors.Is(err, daemonkit.ErrNotReady))
}

func (c *Client) call(ctx context.Context, op string, payload []byte) ([]byte, error) {
	reply, err := c.business.Call(ctx, op, payload)
	if err != nil {
		return nil, err
	}
	return reply.Body, nil
}
