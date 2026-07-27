package hookd

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"sync"
	"syscall"
	"time"

	dkdaemon "github.com/yasyf/daemonkit/daemon"
	"github.com/yasyf/daemonkit/trust"
	"github.com/yasyf/daemonkit/wire"
)

// ErrDaemonUnavailable means the exact signed host cannot be reached.
var ErrDaemonUnavailable = errors.New("captain: daemon unavailable")

// Client owns one exact persistent product session.
type Client struct {
	paths paths
	role  trust.PeerRole

	mu       sync.Mutex
	business *wire.Client
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
	return &Client{paths: resolved, role: businessRoleID}
}

// Close settles the product session.
func (c *Client) Close() error {
	c.mu.Lock()
	business := c.business
	c.business = nil
	c.mu.Unlock()
	if business != nil {
		return business.Close()
	}
	return nil
}

// EnsureCurrent requires the exact deployment-owned host build.
func (c *Client) EnsureCurrent(ctx context.Context, timeout time.Duration) error {
	if timeout <= 0 {
		return errors.New("captain: ensure timeout must be positive")
	}
	health, err := c.RuntimeHealth(ctx)
	if err != nil {
		return probeFailure(err)
	}
	if health.RuntimeProtocol != Schema {
		return fmt.Errorf("captain: runtime protocol %d is not exact v%d", health.RuntimeProtocol, Schema)
	}
	if health.RuntimeBuild != Build {
		return fmt.Errorf("captain: runtime build %q is not exact build %q", health.RuntimeBuild, Build)
	}
	if !health.current() {
		return errors.New("captain: exact signed host is not ready; run `capt-hook helper install`")
	}
	return nil
}

func probeFailure(err error) error {
	if probeRanOutOfTime(err) {
		return fmt.Errorf(
			"captain: signed host did not answer the readiness probe in time; it is running but slow, "+
				"most likely under machine load, and hooks retry on the next event: %w", err,
		)
	}
	return fmt.Errorf("captain: signed host is not installed and ready; run `capt-hook helper install`: %w", err)
}

func probeRanOutOfTime(err error) bool {
	if errors.Is(err, os.ErrDeadlineExceeded) || errors.Is(err, context.DeadlineExceeded) ||
		errors.Is(err, syscall.ETIMEDOUT) {
		return true
	}
	var timeout net.Error
	if errors.As(err, &timeout) && timeout.Timeout() {
		return true
	}
	// *wire.HandshakeRejectionError unwraps to this sentinel alone, never to
	// wire.ErrHandshake, so a capacity refusal cannot ride the pairing below.
	if errors.Is(err, wire.ErrSessionCapacity) {
		return true
	}
	return errors.Is(err, wire.ErrHandshake) &&
		(errors.Is(err, io.EOF) || errors.Is(err, wire.ErrFrameTruncated) ||
			errors.Is(err, io.ErrUnexpectedEOF) || errors.Is(err, syscall.ECONNRESET))
}

// RuntimeHealth observes the exact product runtime without mutating it.
func (c *Client) RuntimeHealth(ctx context.Context) (runtimeHealthResponse, error) {
	result, err := c.call(ctx, wire.Op(opRuntimeHealth), nil)
	if err != nil {
		return runtimeHealthResponse{}, err
	}
	var health runtimeHealthResponse
	if err := decodeStrict(result, &health); err != nil {
		return runtimeHealthResponse{}, fmt.Errorf("captain: decode runtime health: %w", err)
	}
	if health.Schema != Schema || health.RuntimeBuild == "" || health.RuntimeProtocol <= 0 ||
		health.ProcessGeneration == "" || health.PID <= 1 {
		return runtimeHealthResponse{}, errors.New("captain: runtime health identity is incomplete")
	}
	switch dkdaemon.State(health.State) {
	case dkdaemon.StateHealthy, dkdaemon.StateDegraded, dkdaemon.StateFailed:
	default:
		return runtimeHealthResponse{}, fmt.Errorf("captain: invalid runtime health state %q", health.State)
	}
	return health, nil
}

func (h runtimeHealthResponse) current() bool {
	return h.RuntimeBuild == Build && h.RuntimeProtocol == Schema && h.State == string(dkdaemon.StateHealthy) &&
		h.Ready && !h.Draining
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
	payload, err := json.Marshal(restartWorkersRequest{Schema: Schema, Build: Build})
	if err != nil {
		return err
	}
	_, err = c.call(ctx, wire.Op(opRestartWorkers), payload)
	return err
}

func (c *Client) runtimeClientConfig(role trust.PeerRole, timeout time.Duration) wire.RuntimeClientConfig {
	return wire.RuntimeClientConfig{
		Client: wire.ClientConfig{
			Dial: wire.UnixDialer(c.paths.socket), WireBuild: WireBuild, Role: role, MaxFrame: maxHostFrame,
		},
		NoProgressTimeout: timeout,
	}
}

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
		Dial: wire.UnixDialer(c.paths.socket), WireBuild: WireBuild, Role: c.role, MaxFrame: maxHostFrame,
	})
	if err != nil {
		if errors.Is(err, os.ErrNotExist) || errors.Is(err, syscall.ECONNREFUSED) {
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
