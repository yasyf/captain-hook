package hookd

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"

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
	server, runtime, err := s.runtime()
	if err != nil {
		return err
	}
	server.RegisterLifecycle(runtime)
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
	var generation [16]byte
	if _, err := rand.Read(generation[:]); err != nil {
		_ = logFile.Close()
		return nil, nil, fmt.Errorf("captain: generate host generation: %w", err)
	}
	reaper := &proc.Reaper{
		Store: &proc.FileStore{Path: s.paths.processes}, Generation: hex.EncodeToString(generation[:]),
	}
	pool, err := supervise.NewPool(64, reaper)
	if err != nil {
		_ = logFile.Close()
		return nil, nil, err
	}
	manager := newWorkerManager(pool, reaper, logFile)
	wireServer := &wire.Server{
		Build: Build, LifecycleBuild: Build, Workers: 64, Backlog: 192,
		InboundQueue: 256, MaxFrame: maxHostFrame, ReservedProtectedSessions: 1,
		ProtectedSessionClassifier: s.role,
	}
	s.registerHandlers(wireServer, manager)
	peer := &wire.LifecyclePeer{Config: wire.ClientConfig{
		Dial: wire.UnixDialer(s.paths.socket), Build: Build, LifecycleBuild: Build, MaxFrame: maxHostFrame,
	}}
	runtime, err := dkdaemon.NewRuntime(dkdaemon.RuntimeConfig{
		Socket: s.paths.socket, Build: Build, Protocol: int(wire.ProtocolVersion),
		Peer: peer, Contract: dkdaemon.RequestDaemon, WaitMode: dkdaemon.PIDExit,
		Admission: &drain.Intake{}, Server: wireServer, Workers: manager,
		State: runtimeState{}, Resources: &runtimeResources{peer: peer, log: logFile},
		Activate: func(activation dkdaemon.Activation) error {
			return manager.recover(activation.Startup)
		},
		Busy: func() bool { return len(manager.status()) != 0 },
	})
	if err != nil {
		manager.Close()
		manager.Cancel()
		_ = manager.Wait(context.Background())
		_ = peer.Close()
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
		if request.Tenant != "" || len(request.Payload) != 0 {
			return nil, errors.New("captain: restart-workers request must be empty")
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

type runtimeResources struct {
	peer *wire.LifecyclePeer
	log  *os.File
	once sync.Once
	err  error
}

func (r *runtimeResources) Close() error {
	r.once.Do(func() { r.err = errors.Join(r.peer.Close(), r.log.Close()) })
	return r.err
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
