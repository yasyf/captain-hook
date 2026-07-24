package hookd

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"time"

	dkdaemon "github.com/yasyf/daemonkit/daemon"
	"github.com/yasyf/daemonkit/proc"
	"github.com/yasyf/daemonkit/trust"
	"github.com/yasyf/daemonkit/wire"
	"github.com/yasyf/daemonkit/worker"
)

const (
	hostTeamID                    = "SXKCTF23Q2"
	hostSigningIdentifier         = "capt-hookd"
	helperSigningIdentifier       = "com.yasyf.capt-hook.helper"
	helperClientSigningIdentifier = "com.yasyf.capt-hook.helper.bridge"
	hostShutdownTimeout           = 30 * time.Second
)

// Server is the captain-hook product host. Daemonkit owns its listener,
// lifecycle, transport, admission, child identities, and reaping.
type Server struct {
	paths paths
	trust trust.TrustPolicy
}

// NewServer builds the one stable captain-hook host.
func NewServer() (*Server, error) {
	resolved, err := resolvePaths()
	if err != nil {
		return nil, err
	}
	policy, err := hostTrustPolicy()
	if err != nil {
		return nil, err
	}
	return &Server{paths: resolved, trust: policy}, nil
}

// Run serves the exact v1 host until daemonkit completes ordered shutdown.
func (s *Server) Run(ctx context.Context) error {
	_, runtime, err := s.runtime()
	if err != nil {
		return err
	}
	return runtime.run(ctx)
}

type hostRuntime struct {
	daemon  *dkdaemon.Runtime
	slot    *dkdaemon.PublicationSlot[*hostProduct]
	product *hostProduct
	log     *os.File
}

type hostProduct struct {
	manager *workerManager
	hub     *notificationHub
}

func (r *hostRuntime) run(ctx context.Context) error {
	activation, err := r.daemon.Begin(ctx)
	if err != nil {
		return errors.Join(err, r.closeProduct())
	}
	publication, err := r.slot.Stage(activation, r.product)
	if err != nil {
		_ = activation.Fail(err)
		return errors.Join(err, r.daemon.Wait(context.Background()), r.closeProduct())
	}
	settlement, err := activation.ClaimProductSettlement()
	if err != nil {
		_ = activation.Fail(err)
		return errors.Join(err, r.daemon.Wait(context.Background()), r.closeProduct())
	}
	settled := make(chan error, 1)
	go func() {
		<-activation.Context().Done()
		settleCtx, cancel := context.WithTimeout(context.Background(), hostShutdownTimeout)
		defer cancel()
		joined, managerErr := r.product.manager.Close(settleCtx)
		logErr := r.log.Close()
		if !joined {
			settled <- errors.Join(managerErr, logErr)
			return
		}
		settled <- errors.Join(managerErr, logErr, settlement.Complete())
	}()
	if err := activation.CommitReady(publication); err != nil {
		_ = activation.Fail(err)
		return errors.Join(err, r.daemon.Wait(context.Background()), <-settled)
	}
	stopContext := context.AfterFunc(ctx, func() {
		shutdownCtx, cancel := context.WithTimeout(context.Background(), hostShutdownTimeout)
		defer cancel()
		_ = r.daemon.Shutdown(shutdownCtx)
	})
	defer stopContext()
	return errors.Join(r.daemon.Wait(context.Background()), <-settled)
}

func (r *hostRuntime) closeProduct() error {
	closeCtx, cancel := context.WithTimeout(context.Background(), hostShutdownTimeout)
	defer cancel()
	_, managerErr := r.product.manager.Close(closeCtx)
	return errors.Join(managerErr, r.log.Close())
}

func (s *Server) runtime() (*wire.Server, *hostRuntime, error) {
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
		Store:      &proc.FileStore{Path: s.paths.processes, UnsupportedSchema: proc.ArchiveUnsupportedSchema},
		Generation: generation,
	}
	children, err := proc.NewManager(64, reaper)
	if err != nil {
		_ = logFile.Close()
		return nil, nil, err
	}
	disposable, err := worker.NewPool(worker.Config{
		Capacity: 1, QueueCapacity: 0, MaxTotalRun: 30 * time.Second,
		MaxStdinBytes: 1, MaxStdoutBytes: 1, MaxStderrBytes: 1,
	}, reaper)
	if err != nil {
		_ = logFile.Close()
		return nil, nil, err
	}
	manager := newWorkerManager(children, logFile)
	product := &hostProduct{manager: manager, hub: newNotificationHub()}
	wireServer := &wire.Server{
		WireBuild: WireBuild, Workers: 64, Backlog: 192,
		InboundQueue: 256, MaxFrame: maxHostFrame, MaxSessions: 64,
	}
	var runtime *dkdaemon.Runtime
	runtimeHealth := wire.ObservationRoute{
		Op: wire.Op(opRuntimeHealth), MaxResponseBytes: 4 << 10,
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
				ProcessGeneration: health.ProcessGeneration.String(), PID: health.PID, State: string(health.State),
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
		Wire: wireServer, TrustPolicy: s.trust,
		StopControlStore: &proc.FileStore{Path: s.paths.stopProcesses, UnsupportedSchema: proc.ArchiveUnsupportedSchema},
		Observations:     []wire.ObservationRoute{runtimeHealth},
		Workers:          disposable, Children: children, ShutdownTimeout: hostShutdownTimeout,
	})
	if err != nil {
		_ = logFile.Close()
		return nil, nil, err
	}
	slot := dkdaemon.NewPublicationSlot[*hostProduct](runtime)
	s.registerHandlers(wireServer, slot)
	return wireServer, &hostRuntime{daemon: runtime, slot: slot, product: product, log: logFile}, nil
}

func (s *Server) registerHandlers(server *wire.Server, slot *dkdaemon.PublicationSlot[*hostProduct]) {
	server.Register(wire.HandlerSpec{Op: wire.Op(opEvent), Concurrent: true, Handler: func(ctx context.Context, request wire.Request) (any, error) {
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
		product, err := slot.Value(request.Publication)
		if err != nil {
			return nil, err
		}
		return product.manager.dispatch(ctx, event)
	}})
	server.Register(wire.HandlerSpec{Op: wire.Op(opStatus), Handler: func(_ context.Context, request wire.Request) (any, error) {
		if request.Tenant != "" || len(request.Payload) != 0 {
			return nil, errors.New("captain: status request must be empty")
		}
		product, err := slot.Value(request.Publication)
		if err != nil {
			return nil, err
		}
		return statusResponse{Schema: Schema, Build: Build, PID: os.Getpid(), Workers: product.manager.status()}, nil
	}})
	server.Register(wire.HandlerSpec{Op: wire.Op(opRestartWorkers), Handler: func(ctx context.Context, request wire.Request) (any, error) {
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
		product, err := slot.Value(request.Publication)
		if err != nil {
			return nil, err
		}
		if err := product.manager.restart(ctx); err != nil {
			return nil, err
		}
		return struct {
			Schema int `json:"schema"`
		}{Schema: Schema}, nil
	}})
	server.Register(wire.HandlerSpec{Op: wire.Op(opHelperPing), Handler: func(_ context.Context, request wire.Request) (any, error) {
		if request.Tenant != "" || len(request.Payload) != 0 {
			return nil, errors.New("captain: helper ping request must be empty")
		}
		version := Build
		return helperReply{OK: true, Version: &version}, nil
	}})
	server.Register(wire.HandlerSpec{Op: wire.Op(opHelperNotify), Handler: func(ctx context.Context, request wire.Request) (any, error) {
		if request.Tenant != "" {
			return nil, errors.New("captain: helper notify request must not carry a tenant")
		}
		_, payload, err := decodeHelperNotification(request.Payload)
		if err != nil {
			return nil, err
		}
		product, err := slot.Value(request.Publication)
		if err != nil {
			return nil, err
		}
		if err := product.hub.publish(ctx, payload); err != nil {
			return nil, err
		}
		return helperReply{OK: true}, nil
	}})
	server.Register(wire.HandlerSpec{Op: wire.Op(opHelperNext), Concurrent: true, Handler: func(ctx context.Context, request wire.Request) (any, error) {
		if request.Tenant != "" || len(request.Payload) != 0 {
			return nil, errors.New("captain: helper next request must be empty")
		}
		product, err := slot.Value(request.Publication)
		if err != nil {
			return nil, err
		}
		return product.hub.next(ctx)
	}})
}

func hostTrustPolicy() (trust.TrustPolicy, error) {
	hostRequirement := trust.Requirement{TeamID: hostTeamID, SigningIdentifier: hostSigningIdentifier}
	helperRequirement := trust.Requirement{TeamID: hostTeamID, SigningIdentifier: helperSigningIdentifier}
	helperClientRequirement := trust.Requirement{TeamID: hostTeamID, SigningIdentifier: helperClientSigningIdentifier}
	return trust.NewTrustPolicy(trust.TrustPolicyConfig{
		ExpectedUID: os.Geteuid(),
		Roles: map[trust.PeerRole]trust.Requirement{
			businessRoleID: hostRequirement, lifecycleRoleID: hostRequirement, stopControlRoleID: hostRequirement,
			helperConsumerRoleID:        helperRequirement,
			helperBrokerLifecycleRoleID: helperRequirement,
			helperBrokerHandoffRoleID:   helperRequirement,
			helperClientRoleID:          helperClientRequirement,
		},
		StopRoles:      []trust.PeerRole{stopControlRoleID},
		ReceiptRoles:   []trust.PeerRole{lifecycleRoleID, helperConsumerRoleID, helperBrokerLifecycleRoleID},
		ReadinessRoles: []trust.PeerRole{lifecycleRoleID, helperConsumerRoleID, helperBrokerLifecycleRoleID},
		HandoffRoles:   []trust.PeerRole{helperBrokerHandoffRoleID},
	})
}
