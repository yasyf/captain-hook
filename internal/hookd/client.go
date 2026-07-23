package hookd

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	dkdaemon "github.com/yasyf/daemonkit/daemon"
	"github.com/yasyf/daemonkit/proc"
	"github.com/yasyf/daemonkit/service"
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

// EnsureCurrent starts or upgrades the one exact host build.
func (c *Client) EnsureCurrent(ctx context.Context, timeout time.Duration) error {
	if timeout <= 0 {
		return errors.New("captain: ensure timeout must be positive")
	}
	if health, err := c.RuntimeHealth(ctx); err == nil && health.current() {
		return nil
	}
	if err := c.paths.ensure(); err != nil {
		return err
	}
	lock, err := (proc.FileLockSpec{
		Path: c.paths.startLock, Mode: proc.FileLockExclusive, Deadline: timeout,
	}).Acquire(ctx)
	if err != nil {
		return fmt.Errorf("captain: acquire host start lock: %w", err)
	}
	defer lock.Close()

	health, healthErr := c.RuntimeHealth(ctx)
	if healthErr == nil && health.current() {
		return nil
	}
	executable, err := hostExecutable()
	if err != nil {
		return err
	}
	if healthErr == nil {
		if health.RuntimeProtocol != Schema {
			return fmt.Errorf("captain: runtime protocol %d is not exact v%d", health.RuntimeProtocol, Schema)
		}
		if err := c.stopRuntime(ctx, health); err != nil {
			return err
		}
		c.resetBusiness(ErrDaemonUnavailable)
	} else if !errors.Is(healthErr, ErrDaemonUnavailable) {
		present, endpointErr := c.endpointPresent(ctx)
		if endpointErr != nil {
			return errors.Join(healthErr, endpointErr)
		}
		if present {
			return fmt.Errorf("captain: incompatible host must be stopped before the hard cut: %w", healthErr)
		}
		c.resetBusiness(ErrDaemonUnavailable)
	}
	controller, err := c.serviceController(ctx)
	if err != nil {
		return err
	}
	defer func() {
		closeCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
		defer cancel()
		_ = controller.Close(closeCtx)
	}()
	if err := controller.Converge(ctx, nil); err != nil {
		return fmt.Errorf("captain: settle host service: %w", err)
	}
	if err := controller.Converge(ctx, []service.Agent{c.hostAgent(executable)}); err != nil {
		return fmt.Errorf("captain: start host service: %w", err)
	}
	_, err = wire.AcquireReadyRuntime(ctx, c.runtimeClientConfig(lifecycleRoleID, timeout), Build)
	return err
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

// Shutdown requests daemonkit's ordered host shutdown.
func (c *Client) Shutdown(ctx context.Context) (returnErr error) {
	if err := c.paths.ensure(); err != nil {
		return err
	}
	lock, err := (proc.FileLockSpec{
		Path: c.paths.startLock, Mode: proc.FileLockExclusive, Deadline: 30 * time.Second,
	}).Acquire(ctx)
	if err != nil {
		return fmt.Errorf("captain: acquire host start lock: %w", err)
	}
	defer lock.Close()
	health, err := c.RuntimeHealth(ctx)
	if err != nil {
		return err
	}
	if health.RuntimeProtocol != Schema {
		return fmt.Errorf("captain: runtime protocol %d is not exact v%d", health.RuntimeProtocol, Schema)
	}
	if err := c.stopRuntime(ctx, health); err != nil {
		return err
	}
	controller, err := c.serviceController(ctx)
	if err != nil {
		return err
	}
	defer func() {
		closeCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
		defer cancel()
		returnErr = errors.Join(returnErr, controller.Close(closeCtx))
	}()
	if err := controller.Converge(ctx, nil); err != nil {
		return err
	}
	c.resetBusiness(ErrDaemonUnavailable)
	return nil
}

func (c *Client) stopRuntime(ctx context.Context, health runtimeHealthResponse) (returnErr error) {
	controller, err := c.serviceController(ctx)
	if err != nil {
		return err
	}
	defer func() {
		closeCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
		defer cancel()
		returnErr = errors.Join(returnErr, controller.Close(closeCtx))
	}()
	_, err = controller.StopRuntime(ctx, service.StopRuntimeRequest{
		OperationID:          "captain-hook.stop.v1:" + health.ProcessGeneration,
		RuntimeClientConfig:  c.runtimeClientConfig(stopControlRoleID, 30*time.Second),
		ExpectedRuntimeBuild: health.RuntimeBuild,
		ControlRole:          stopControlRoleID,
	})
	return err
}

func (c *Client) serviceController(ctx context.Context) (*service.Controller, error) {
	return service.NewController(ctx, service.ControllerConfig{
		StatePath: c.paths.stopState, ProcessPath: c.paths.stopProcesses, WorkerLimit: 1,
	})
}

func (c *Client) hostAgent(executable string) service.Agent {
	return service.Agent{
		Label: hostServiceLabel, Program: executable, Args: []string{"serve"}, LogPath: c.paths.log,
		AssociatedBundleIdentifiers: []string{"com.yasyf.capt-hook.helper"},
		RestartPolicy:               service.RestartOnFailure,
	}
}

func (c *Client) runtimeClientConfig(role trust.PeerRole, timeout time.Duration) wire.RuntimeClientConfig {
	return wire.RuntimeClientConfig{
		Client: wire.ClientConfig{
			Dial: wire.UnixDialer(c.paths.socket), WireBuild: WireBuild, Role: role, MaxFrame: maxHostFrame,
		},
		NoProgressTimeout: timeout,
	}
}

func hostExecutable() (string, error) {
	executable, err := os.Executable()
	if err != nil {
		return "", fmt.Errorf("captain: resolve host executable: %w", err)
	}
	executable, err = filepath.EvalSymlinks(executable)
	if err != nil {
		return "", fmt.Errorf("captain: resolve host executable identity: %w", err)
	}
	if !filepath.IsAbs(executable) || filepath.Clean(executable) != executable {
		return "", errors.New("captain: host executable identity is not exact")
	}
	return executable, nil
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

func (c *Client) resetBusiness(cause error) {
	c.mu.Lock()
	session := c.business
	c.business = nil
	c.mu.Unlock()
	if session != nil {
		_ = session.Abort(cause)
	}
}

func (c *Client) endpointPresent(ctx context.Context) (bool, error) {
	conn, err := wire.UnixDialer(c.paths.socket)(ctx)
	if err == nil {
		_ = conn.Close()
		return true, nil
	}
	if errors.Is(err, os.ErrNotExist) || errors.Is(err, syscall.ENOENT) || errors.Is(err, syscall.ECONNREFUSED) {
		return false, nil
	}
	return false, fmt.Errorf("captain: probe host endpoint: %w", err)
}
