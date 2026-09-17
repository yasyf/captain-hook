package hookd

import (
	"bytes"
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/yasyf/captain-hook/internal/wireproto"
	"github.com/yasyf/daemonkit"
)

type stoppedProcess struct {
	stopped chan struct{}
	err     error
}

func (p *stoppedProcess) Stop(context.Context) (daemonkit.Reap, error) {
	close(p.stopped)
	return daemonkit.ReapUndetermined, p.err
}

type lockedBuffer struct {
	mu     sync.Mutex
	buffer bytes.Buffer
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buffer.Write(p)
}

func (b *lockedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buffer.String()
}

func adoptingManager(t *testing.T, process detachedProcess, adoptErr error) (*workerManager, chan int, *lockedBuffer) {
	t.Helper()
	log := &lockedBuffer{}
	manager := newWorkerManager(daemonkit.Ctx{}, log)
	adopted := make(chan int, 1)
	manager.adoptProcess = func(_ context.Context, pid int) (detachedProcess, error) {
		adopted <- pid
		return process, adoptErr
	}
	return manager, adopted, log
}

func closeManager(t *testing.T, manager *workerManager) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if drained, err := manager.Close(ctx); !drained || err != nil {
		t.Fatalf("Close = %v, %v; want a drained manager", drained, err)
	}
}

func TestAnAdoptFrameReachesTheOwnerBetweenReplies(t *testing.T) {
	t.Parallel()
	worker, serverConn := silentWorker(t)
	adopted := make(chan wireproto.AdoptRequest, 1)
	worker.setOnAdopt(func(request wireproto.AdoptRequest) { adopted <- request })
	go func() {
		event, err := wireproto.DecodeFrame(serverConn)
		if err != nil {
			return
		}
		_ = wireproto.EncodeFrame(serverConn, wireproto.Frame{
			Protocol: wireproto.Schema, Op: wireproto.OpAdopt, Adopt: &wireproto.AdoptRequest{PID: 4242, LifetimeMS: 7_500_000},
		})
		_ = wireproto.EncodeFrame(serverConn, wireproto.Frame{
			Protocol: wireproto.Schema, Op: wireproto.OpResult, ID: event.ID,
			Response: &wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok", Stdout: "answered"},
		})
	}()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	response, err := worker.call(ctx, testEventRequest("SessionEnd"))
	if err != nil {
		t.Fatalf("call across an adopt frame = %v, want success", err)
	}
	if response.Stdout != "answered" {
		t.Fatalf("Stdout = %q, want %q", response.Stdout, "answered")
	}
	if got := <-adopted; got != (wireproto.AdoptRequest{PID: 4242, LifetimeMS: 7_500_000}) {
		t.Fatalf("adopted = %+v, want pid 4242 bounded at 7500000ms", got)
	}
	if worker.broken() {
		t.Fatal("an adopt frame broke the worker every other session shares")
	}
}

func TestAMalformedAdoptFrameMarksTheWorkerBroken(t *testing.T) {
	t.Parallel()
	for name, frame := range map[string]wireproto.Frame{
		"no body":       {Protocol: wireproto.Schema, Op: wireproto.OpAdopt},
		"carries an id": {Protocol: wireproto.Schema, Op: wireproto.OpAdopt, ID: 1, Adopt: &wireproto.AdoptRequest{PID: 4242, LifetimeMS: 1}},
		"init's pid":    {Protocol: wireproto.Schema, Op: wireproto.OpAdopt, Adopt: &wireproto.AdoptRequest{PID: 1, LifetimeMS: 1}},
		"unbounded":     {Protocol: wireproto.Schema, Op: wireproto.OpAdopt, Adopt: &wireproto.AdoptRequest{PID: 4242}},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			worker, serverConn := silentWorker(t)
			worker.setOnAdopt(func(wireproto.AdoptRequest) { t.Error("a malformed adopt frame reached the owner") })
			go func() { _ = wireproto.EncodeFrame(serverConn, frame) }()
			deadline := time.Now().Add(5 * time.Second)
			for !worker.broken() {
				if time.Now().After(deadline) {
					t.Fatal("a malformed adopt frame left the worker serving")
				}
				time.Sleep(time.Millisecond)
			}
		})
	}
}

func TestAnAdoptedProcessIsStoppedAtItsLifetimeBound(t *testing.T) {
	t.Parallel()
	process := &stoppedProcess{stopped: make(chan struct{})}
	manager, adopted, _ := adoptingManager(t, process, nil)
	defer closeManager(t, manager)

	manager.adopt(wireproto.AdoptRequest{PID: 4242, LifetimeMS: 20})

	if pid := <-adopted; pid != 4242 {
		t.Fatalf("adopted pid = %d, want 4242", pid)
	}
	select {
	case <-process.stopped:
	case <-time.After(5 * time.Second):
		t.Fatal("the process outlived its lifetime bound")
	}
}

func TestCloseLeavesAnAdoptedProcessToTheScopeSettlement(t *testing.T) {
	t.Parallel()
	process := &stoppedProcess{stopped: make(chan struct{})}
	manager, adopted, _ := adoptingManager(t, process, nil)

	manager.adopt(wireproto.AdoptRequest{PID: 4242, LifetimeMS: time.Hour.Milliseconds()})
	<-adopted
	closeManager(t, manager)

	select {
	case <-process.stopped:
		t.Fatal("Close stopped the process itself; the ownership scope settles it")
	default:
	}
}

func TestAnAdoptionAnnouncedDuringCloseIsStillRecorded(t *testing.T) {
	t.Parallel()
	process := &stoppedProcess{stopped: make(chan struct{})}
	manager, adopted, _ := adoptingManager(t, process, nil)
	closeManager(t, manager)

	manager.adopt(wireproto.AdoptRequest{PID: 4242, LifetimeMS: 20})

	select {
	case pid := <-adopted:
		if pid != 4242 {
			t.Fatalf("adopted pid = %d, want 4242", pid)
		}
	default:
		t.Fatal("an adoption announced after Close began was dropped; nothing would ever settle the process")
	}
}

func TestAnAdoptionRacingCloseIsRecordedOnItsOwnBudget(t *testing.T) {
	t.Parallel()
	manager, _, _ := adoptingManager(t, &stoppedProcess{stopped: make(chan struct{})}, nil)
	entered := make(chan struct{})
	release := make(chan struct{})
	budget := make(chan error, 1)
	manager.adoptProcess = func(ctx context.Context, _ int) (detachedProcess, error) {
		close(entered)
		<-release
		budget <- ctx.Err()
		return &stoppedProcess{stopped: make(chan struct{})}, nil
	}

	manager.adopt(wireproto.AdoptRequest{PID: 4242, LifetimeMS: time.Hour.Milliseconds()})
	<-entered
	closed := make(chan error, 1)
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_, err := manager.Close(ctx)
		closed <- err
	}()
	manager.mu.Lock()
	for !manager.closed {
		manager.mu.Unlock()
		time.Sleep(time.Millisecond)
		manager.mu.Lock()
	}
	manager.mu.Unlock()
	close(release)

	if err := <-budget; err != nil {
		t.Fatalf("adoption context after Close began = %v; Close cancelled an adoption the host had accepted", err)
	}
	if err := <-closed; err != nil {
		t.Fatalf("Close = %v, want it to join the adoption and drain", err)
	}
}

func TestARefusedAdoptionIsLoggedAndHoldsNoBound(t *testing.T) {
	t.Parallel()
	manager, adopted, log := adoptingManager(t, nil, errors.New("no such process"))

	manager.adopt(wireproto.AdoptRequest{PID: 4242, LifetimeMS: time.Hour.Milliseconds()})
	<-adopted
	closeManager(t, manager)

	if got := log.String(); !strings.Contains(got, "adopt detached pid 4242: no such process") {
		t.Fatalf("host log = %q, want the refused adoption named", got)
	}
}
