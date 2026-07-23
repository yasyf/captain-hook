package hookd

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"

	dkdaemon "github.com/yasyf/daemonkit/daemon"
	"github.com/yasyf/daemonkit/daemonrole"
	"github.com/yasyf/daemonkit/drain"
	"github.com/yasyf/daemonkit/proc"
	"github.com/yasyf/daemonkit/supervise"
	"github.com/yasyf/daemonkit/wire"
)

// Server is the captain-hook product host. Daemonkit owns its listener,
// lifecycle, transport, admission, child identities, and reaping.
type Server struct {
	paths paths
	role  daemonrole.Classifier
}

// NewServer builds the one stable captain-hook host role.
func NewServer(role daemonrole.Classifier) (*Server, error) {
	if err := role.Validate(); err != nil {
		return nil, err
	}
	resolved, err := resolvePaths()
	if err != nil {
		return nil, err
	}
	return &Server{paths: resolved, role: role}, nil
}

// Run serves the exact v1 host until daemonkit completes ordered shutdown.
func (s *Server) Run(ctx context.Context) error {
	_, runtime, err := s.runtime()
	if err != nil {
		return err
	}
	err = runtime.Run(ctx)
	if ctx.Err() != nil && errors.Is(err, ctx.Err()) {
		return nil
	}
	return err
}

func (s *Server) runtime() (*wire.Server, *dkdaemon.Runtime, error) {
	if err := s.paths.ensure(); err != nil {
		return nil, nil, err
	}
	logFile, err := os.OpenFile(s.paths.log, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return nil, nil, fmt.Errorf("captain: open host log: %w", err)
	}
	generation, err := proc.ProcessGeneration()
	if err != nil {
		_ = logFile.Close()
		return nil, nil, fmt.Errorf("captain: generate host generation: %w", err)
	}
	reaper := &proc.Reaper{
		Store: &proc.FileStore{Path: s.paths.processes}, Generation: generation,
	}
	pool, err := supervise.NewPool(64, reaper)
	if err != nil {
		_ = logFile.Close()
		return nil, nil, err
	}
	manager := newWorkerManager(pool, reaper, logFile)
	wireServer := &wire.Server{
		WireBuild: WireBuild, Trust: s.trust, Workers: 64, Backlog: 192,
		InboundQueue: 256, MaxFrame: maxHostFrame, MaxSessions: 64,
	}
	s.registerHandlers(wireServer, manager)
	var runtime *dkdaemon.Runtime
	runtimeHealth := wire.ObservationRoute{
		Op: wire.Op(opRuntimeHealth), MaxResponseBytes: 4 << 10, AvailableBeforeReady: true,
		Handler: func(ctx context.Context, request wire.ObservationRequest) (wire.ObservationResponse, error) {
			if request.Tenant != "" || len(request.Payload) != 0 {
				return wire.ObservationResponse{}, errors.New("captain: runtime health request must be empty")
			}
			if runtime == nil {
				return wire.ObservationResponse{}, errors.New("captain: runtime health is unavailable")
			}
			health, err := runtime.Health(ctx)
			if err != nil {
				return wire.ObservationResponse{}, err
			}
			payload, err := json.Marshal(runtimeHealthResponse{
				Schema: Schema, RuntimeBuild: health.RuntimeBuild, RuntimeProtocol: health.RuntimeProtocol,
				ProcessGeneration: health.ProcessGeneration, PID: health.PID, State: string(health.State),
				Draining: health.Draining, Busy: health.Busy, Ready: health.Ready,
			})
			if err != nil {
				return wire.ObservationResponse{}, err
			}
			return wire.ObservationResponse{Payload: payload}, nil
		},
	}
	runtime, err = wire.NewRuntime(wire.RuntimeConfig{
		Socket: s.paths.socket, RuntimeBuild: Build, RuntimeProtocol: Schema,
		Wire: wireServer, Classifier: s.role, ReservedProtectedSessions: 1,
		StopVerifier: wire.StopVerifier{
			Classifier: s.role, Role: stopControlRoleID,
			Store: &proc.FileStore{Path: s.paths.stopProcesses},
		},
		Observations: []wire.ObservationRoute{runtimeHealth}, Admission: &drain.Intake{}, Workers: manager,
		State: runtimeState{}, Resources: logFile,
		Activate: func(activation dkdaemon.Activation) error {
			return manager.recover(activation.Startup)
		},
		Busy: func() bool { return len(manager.status()) != 0 },
	})
	if err != nil {
		manager.Close()
		manager.Cancel()
		_ = manager.Wait(context.Background())
		_ = logFile.Close()
		return nil, nil, err
	}
	return wireServer, runtime, nil
}

func (s *Server) registerHandlers(server *wire.Server, manager *workerManager) {
	server.RegisterConcurrent(wire.Op(opEvent), func(ctx context.Context, request wire.Request) (any, error) {
		if request.Tenant != "" {
			return nil, errors.New("captain: event request must not carry a tenant")
		}
		var event EventRequest
		if err := decodeStrict(request.Payload, &event); err != nil {
			return nil, fmt.Errorf("captain: decode event request: %w", err)
		}
		if event.Build != Build {
			return nil, fmt.Errorf("captain: Python build %q does not match signed host build %q", event.Build, Build)
		}
		if event.ClientPID != request.Peer.PID {
			return nil, errors.New("captain: event client pid does not match authenticated peer")
		}
		return manager.dispatch(ctx, event)
	})
	server.RegisterControl(wire.Op(opStatus), func(_ context.Context, request wire.Request) (any, error) {
		if request.Tenant != "" || len(request.Payload) != 0 {
			return nil, errors.New("captain: status request must be empty")
		}
		return statusResponse{Schema: Schema, Build: Build, PID: os.Getpid(), Workers: manager.status()}, nil
	})
	server.RegisterControl(wire.Op(opRestartWorkers), func(ctx context.Context, request wire.Request) (any, error) {
		if request.Tenant != "" {
			return nil, errors.New("captain: restart-workers request must not carry a tenant")
		}
		var restart restartWorkersRequest
		if err := decodeStrict(request.Payload, &restart); err != nil {
			return nil, fmt.Errorf("captain: decode restart-workers request: %w", err)
		}
		if restart.Schema != Schema || restart.Build != Build {
			return nil, errors.New("captain: restart-workers requires the exact runtime build")
		}
		if err := manager.restart(ctx); err != nil {
			return nil, err
		}
		return struct {
			Schema int `json:"schema"`
		}{Schema: Schema}, nil
	})
}

type runtimeState struct{}

func (runtimeState) Close() error { return nil }

func (s *Server) trust(ctx context.Context, peer wire.Peer) error {
	accepted, err := s.role.Classify(ctx, peer)
	if err != nil {
		return err
	}
	if !accepted {
		return daemonrole.ErrUntrustedPeer
	}
	return nil
}

// DaemonRole resolves the exact stable executable role used for launch and trust.
func DaemonRole() (daemonrole.Classifier, error) {
	executable, err := os.Executable()
	if err != nil {
		return daemonrole.Classifier{}, fmt.Errorf("captain: resolve host executable: %w", err)
	}
	executable, err = filepath.EvalSymlinks(executable)
	if err != nil {
		return daemonrole.Classifier{}, fmt.Errorf("captain: resolve host role: %w", err)
	}
	role := daemonrole.Classifier{RoleID: daemonRoleID, RolePath: executable}
	if err := role.Validate(); err != nil {
		return daemonrole.Classifier{}, err
	}
	return role, nil
}
