package hookd

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"sync"
	"time"

	dkdaemon "github.com/yasyf/daemonkit/daemon"
	"github.com/yasyf/daemonkit/proc"
	"github.com/yasyf/daemonkit/wire"
)

// ErrDaemonUnavailable means the exact signed host cannot be reached.
var ErrDaemonUnavailable = errors.New("captain: daemon unavailable")

// Client owns exact persistent business and lifecycle sessions.
type Client struct {
	paths paths

	mu        sync.Mutex
	business  *wire.Client
	lifecycle *wire.LifecyclePeer
}

// NewClient returns a lazy client for the only host schema and build.
func NewClient() (*Client, error) {
	resolved, err := resolvePaths()
	if err != nil {
		return nil, err
	}
	return newClientWithPaths(resolved), nil
}

func newClientWithPaths(resolved paths) *Client {
	client := &Client{paths: resolved}
	client.lifecycle = &wire.LifecyclePeer{Config: wire.ClientConfig{
		Dial: wire.UnixDialer(resolved.socket), Build: Build, LifecycleBuild: Build, MaxFrame: maxHostFrame,
	}}
	return client
}

// Close settles the business and lifecycle sessions.
func (c *Client) Close() error {
	c.mu.Lock()
	business := c.business
	c.business = nil
	c.mu.Unlock()
	var businessErr error
	if business != nil {
		businessErr = business.Close()
	}
	return errors.Join(businessErr, c.lifecycle.Close())
}

// EnsureCurrent starts or upgrades the one exact host build.
func (c *Client) EnsureCurrent(ctx context.Context, timeout time.Duration) error {
	if health, err := c.lifecycle.Health(ctx); err == nil && health.Build == Build &&
		health.Protocol == int(wire.ProtocolVersion) {
		return nil
	}
	if err := c.paths.ensure(); err != nil {
		return err
	}
	role, err := DaemonRole()
	if err != nil {
		return err
	}
	spawn := proc.Spawn{
		Socket: c.paths.socket, LogPath: c.paths.log, ExecPath: role.RolePath,
		Args: []string{"serve"}, Timeout: timeout,
		Available: func() bool {
			probeCtx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
			defer cancel()
			health, err := c.lifecycle.Health(probeCtx)
			return err == nil && health.Build == Build && health.Protocol == int(wire.ProtocolVersion)
		},
		CanHost: func() error { return nil },
	}
	return dkdaemon.EnsureCurrent(ctx, dkdaemon.EnsureConfig{
		Peer: c.lifecycle, Protocol: int(wire.ProtocolVersion), LockPath: c.paths.startLock,
		Ensure: spawn.EnsureRunning, Timeout: timeout,
	}, Build)
}

// Event dispatches exactly once. No transport outcome is replayed.
func (c *Client) Event(ctx context.Context, request EventRequest) (EventResponse, error) {
	payload, err := json.Marshal(request)
	if err != nil {
		return EventResponse{}, fmt.Errorf("captain: encode event request: %w", err)
	}
	result, err := c.call(ctx, wire.Op(opEvent), payload)
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
	result, err := c.call(ctx, wire.Op(opStatus), nil)
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
	_, err := c.call(ctx, wire.Op(opRestartWorkers), nil)
	return err
}

// Shutdown requests daemonkit's ordered host shutdown.
func (c *Client) Shutdown(ctx context.Context) error { return c.lifecycle.Shutdown(ctx) }

func (c *Client) call(ctx context.Context, op wire.Op, payload []byte) ([]byte, error) {
	session, err := c.businessSession(ctx)
	if err != nil {
		return nil, err
	}
	result, err := session.Call(ctx, op, "", payload)
	if err != nil {
		c.retireBusiness(session, err)
		return nil, err
	}
	if result.Outcome != wire.Delivered {
		reason := result.Response.Reason
		if reason == "" {
			reason = result.Outcome.String()
		}
		return nil, fmt.Errorf("captain: request rejected before dispatch: %s", reason)
	}
	if result.Response.Err != "" {
		return nil, errors.New(result.Response.Err)
	}
	return result.Response.Payload, nil
}

func (c *Client) businessSession(ctx context.Context) (*wire.Client, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.business != nil {
		return c.business, nil
	}
	session, err := wire.NewClient(ctx, wire.ClientConfig{
		Dial: wire.UnixDialer(c.paths.socket), Build: Build, MaxFrame: maxHostFrame,
	})
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, ErrDaemonUnavailable
		}
		return nil, err
	}
	c.business = session
	return session, nil
}

func (c *Client) retireBusiness(session *wire.Client, cause error) {
	c.mu.Lock()
	if c.business == session {
		c.business = nil
	}
	c.mu.Unlock()
	_ = session.Abort(cause)
}
