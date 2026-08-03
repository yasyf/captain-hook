package hookd

import (
	"context"
	"errors"
	"fmt"
	"os"
	"time"

	"github.com/yasyf/daemonkit"
)

// ErrDaemonUnavailable means the exact signed host cannot be reached.
var ErrDaemonUnavailable = errors.New("captain: daemon unavailable")

const closeTimeout = 5 * time.Second

// Client owns one exact persistent product session.
type Client struct {
	daemon   *daemonkit.Client
	business *daemonkit.Business
}

// NewClient returns a lazy client for the only host schema and build.
func NewClient() (*Client, error) {
	daemon, err := daemonkit.Open(hostDaemon())
	if err != nil {
		return nil, fmt.Errorf("captain: open signed host: %w", err)
	}
	return &Client{daemon: daemon, business: daemon.Business()}, nil
}

// Close settles the product session.
func (c *Client) Close() error {
	ctx, cancel := context.WithTimeout(context.Background(), closeTimeout)
	defer cancel()
	return c.business.Close(ctx)
}

// EnsureCurrent requires the exact deployment-owned host build.
func (c *Client) EnsureCurrent(ctx context.Context) error {
	health, err := c.RuntimeHealth(ctx)
	if err != nil {
		return probeFailure(err)
	}
	return health.exact()
}

// probeFailure names what the probe actually met, because the five outcomes
// have five different next steps. ErrNotReady, ErrDraining, and ErrPeerGone are
// one runtime mid-transition and answer again on the next event; a deadline is
// a host that is there and slow; ErrUntrusted is a live peer that failed the
// signed-host requirement, which reinstalling fixes and waiting does not; and
// ErrNoVerifier is a machine that cannot answer the question at all. Only what
// is left is a host that is not installed.
func probeFailure(err error) error {
	switch {
	case errors.Is(err, daemonkit.ErrNotReady), errors.Is(err, daemonkit.ErrDraining),
		errors.Is(err, daemonkit.ErrPeerGone):
		return fmt.Errorf(
			"captain: signed host is between generations — starting, draining, or restarting — "+
				"and hooks retry on the next event: %w", err,
		)
	case errors.Is(err, os.ErrDeadlineExceeded), errors.Is(err, context.DeadlineExceeded):
		return fmt.Errorf(
			"captain: signed host did not answer the readiness probe in time; it is running but slow, "+
				"most likely under machine load, and hooks retry on the next event: %w", err,
		)
	case errors.Is(err, daemonkit.ErrUntrusted):
		return fmt.Errorf(
			"captain: the process serving the host socket is not the signed capt-hookd; "+
				"reinstall the helper with `capt-hook helper install`: %w", err,
		)
	case errors.Is(err, daemonkit.ErrNoVerifier):
		return fmt.Errorf(
			"captain: this machine offers no code-signing verifier, so the signed host cannot be "+
				"trusted and hooks stay unserved: %w", err,
		)
	default:
		return fmt.Errorf("captain: signed host is not installed and ready; run `capt-hook helper install`: %w", err)
	}
}

// RuntimeHealth observes the exact product runtime without mutating it.
func (c *Client) RuntimeHealth(ctx context.Context) (runtimeHealthResponse, error) {
	result, err := c.call(ctx, opRuntimeHealth, nil)
	if err != nil {
		return runtimeHealthResponse{}, err
	}
	var health runtimeHealthResponse
	if err := decodeStrict(result, &health); err != nil {
		return runtimeHealthResponse{}, fmt.Errorf("captain: decode runtime health: %w", err)
	}
	if health.Schema != Schema || health.RuntimeBuild == "" || health.RuntimeProtocol <= 0 || health.PID <= 1 {
		return runtimeHealthResponse{}, errors.New("captain: runtime health identity is incomplete")
	}
	return health, nil
}

func (h runtimeHealthResponse) exact() error {
	if h.RuntimeProtocol != Schema {
		return fmt.Errorf("captain: runtime protocol %d is not exact v%d", h.RuntimeProtocol, Schema)
	}
	if h.RuntimeBuild != Build {
		return fmt.Errorf("captain: runtime build %q is not exact build %q", h.RuntimeBuild, Build)
	}
	return nil
}

// Event dispatches exactly once. No transport outcome is replayed.
func (c *Client) Event(ctx context.Context, request EventRequest) (EventResponse, error) {
	payload, err := marshalEventRequest(request)
	if err != nil {
		return EventResponse{}, err
	}
	result, err := c.call(ctx, opEvent, payload)
	if err != nil {
		return EventResponse{}, err
	}
	var response EventResponse
	if err := decodeStrict(result, &response); err != nil {
		return EventResponse{}, fmt.Errorf("captain: decode event response: %w", err)
	}
	if err := validateEventResponse(response); err != nil {
		return EventResponse{}, err
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
	payload, err := marshalHostJSON(restartWorkersRequest{Schema: Schema, Build: Build})
	if err != nil {
		return err
	}
	_, err = c.call(ctx, opRestartWorkers, payload)
	return err
}

func (c *Client) call(ctx context.Context, op string, payload []byte) ([]byte, error) {
	reply, err := c.business.Call(ctx, op, payload)
	if errors.Is(err, daemonkit.ErrAbsent) {
		return nil, errors.Join(ErrDaemonUnavailable, err)
	}
	if err != nil {
		return nil, err
	}
	return reply.Body, nil
}
