package hookd

import (
	"context"
	"errors"
	"fmt"
	"os"

	"github.com/yasyf/daemonkit"
)

// Server is the captain-hook product host. Daemonkit owns its listener,
// lifecycle, transport, admission, child identities, and reaping.
type Server struct {
	paths  paths
	daemon daemonkit.Daemon
}

// NewServer builds the one stable captain-hook host.
func NewServer() (*Server, error) {
	resolved, err := resolvePaths()
	if err != nil {
		return nil, err
	}
	return &Server{paths: resolved, daemon: hostDaemon()}, nil
}

// Run serves the exact v1 host until daemonkit completes ordered shutdown.
func (s *Server) Run(ctx context.Context) error {
	if err := s.paths.ensure(); err != nil {
		return err
	}
	// The log outlives this daemon generation deliberately. Daemonkit's per-child
	// stderr copiers are detached goroutines with no completion boundary a
	// consumer can join, so closing the sink during shutdown turns a worker's
	// last diagnostics into a write on a closed file. Process exit closes it.
	logFile, err := os.OpenFile(s.paths.log, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return fmt.Errorf("captain: open host log: %w", err)
	}
	_, err = daemonkit.Serve(ctx, s.daemon, func(hostCtx daemonkit.Ctx) (daemonkit.Product, error) {
		return &hostProduct{
			manager: newWorkerManager(hostCtx, logFile),
			hub:     newNotificationHub(),
		}, nil
	})
	return err
}

type hostProduct struct {
	manager *workerManager
	hub     *notificationHub
}

// Handle owns dispatch for every captain-hook op. Admission to the business
// lane is the only authority boundary: any peer daemonkit admitted may invoke
// any op here.
func (p *hostProduct) Handle(ctx context.Context, req daemonkit.Request) (daemonkit.Reply, error) {
	switch req.Op {
	case opEvent:
		var event EventRequest
		if err := decodeStrict(req.Body, &event); err != nil {
			return daemonkit.Reply{}, fmt.Errorf("captain: decode event request: %w", err)
		}
		if event.Build != Build {
			return daemonkit.Reply{}, fmt.Errorf(
				"captain: Python build %q does not match signed host build %q", event.Build, Build,
			)
		}
		if event.ClientPID != req.Caller.PID {
			return daemonkit.Reply{}, errors.New("captain: event client pid does not match authenticated peer")
		}
		response, err := p.manager.dispatch(ctx, event)
		if err != nil {
			return daemonkit.Reply{}, err
		}
		return encodeReply(response)
	case opStatus:
		if len(req.Body) != 0 {
			return daemonkit.Reply{}, errors.New("captain: status request must be empty")
		}
		return encodeReply(statusResponse{
			Schema: Schema, Build: Build, PID: os.Getpid(), Workers: p.manager.status(),
		})
	case opRuntimeHealth:
		if len(req.Body) != 0 {
			return daemonkit.Reply{}, errors.New("captain: runtime health request must be empty")
		}
		return encodeReply(runtimeHealthResponse{
			Schema: Schema, RuntimeBuild: Build, RuntimeProtocol: Schema, PID: os.Getpid(),
		})
	case opRestartWorkers:
		var restart restartWorkersRequest
		if err := decodeStrict(req.Body, &restart); err != nil {
			return daemonkit.Reply{}, fmt.Errorf("captain: decode restart-workers request: %w", err)
		}
		if restart.Schema != Schema || restart.Build != Build {
			return daemonkit.Reply{}, errors.New("captain: restart-workers requires the exact runtime build")
		}
		if err := p.manager.restart(ctx); err != nil {
			return daemonkit.Reply{}, err
		}
		return encodeReply(struct {
			Schema int `json:"schema"`
		}{Schema: Schema})
	case opHelperPing:
		if len(req.Body) != 0 {
			return daemonkit.Reply{}, errors.New("captain: helper ping request must be empty")
		}
		version := Build
		return encodeReply(helperReply{OK: true, Version: &version})
	case opHelperNotify:
		_, payload, err := decodeHelperNotification(req.Body)
		if err != nil {
			return daemonkit.Reply{}, err
		}
		if err := p.hub.publish(ctx, payload); err != nil {
			return daemonkit.Reply{}, err
		}
		return encodeReply(helperReply{OK: true})
	case opHelperNext:
		if len(req.Body) != 0 {
			return daemonkit.Reply{}, errors.New("captain: helper next request must be empty")
		}
		payload, err := p.hub.next(ctx)
		if err != nil {
			return daemonkit.Reply{}, err
		}
		return daemonkit.Reply{Body: payload}, nil
	default:
		return daemonkit.Reply{}, fmt.Errorf("captain: unknown operation %q", req.Op)
	}
}

// Drain has nothing of its own to settle: daemonkit joins admitted dispatch
// before this stage and terminates the worker children after it.
func (p *hostProduct) Drain(daemonkit.Budget) error { return nil }

func (p *hostProduct) Close(budget daemonkit.Budget) error {
	ctx, cancel := budget.Context(context.Background())
	defer cancel()
	_, err := p.manager.Close(ctx)
	return err
}

func encodeReply(value any) (daemonkit.Reply, error) {
	payload, err := marshalHostJSON(value)
	if err != nil {
		return daemonkit.Reply{}, fmt.Errorf("captain: encode reply: %w", err)
	}
	return daemonkit.Reply{Body: payload}, nil
}
