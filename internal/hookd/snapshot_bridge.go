package hookd

import (
	"context"
	"errors"
	"fmt"
	"io"
	"sync"

	"github.com/yasyf/captain-hook/internal/snapshots"
	"github.com/yasyf/captain-hook/internal/wireproto"
	"github.com/yasyf/daemonkit"
)

func (p *hostProduct) transcript(ctx context.Context, req daemonkit.Request) (daemonkit.Reply, error) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	go func() {
		select {
		case <-req.Session.Disconnected():
			cancel()
		case <-ctx.Done():
		}
	}()
	callContext := userSnapshotContext(fmt.Sprintf("review:%d", req.Session.ID()), "review", req.Caller.UID)
	body, err := p.snapshots.call(ctx, req.Body, callContext)
	return daemonkit.Reply{Body: body}, err
}

func transcriptClientCommand(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	if len(args) != 0 {
		fmt.Fprintln(stderr, "capt-hookd transcript-client: no arguments accepted")
		return 2
	}
	client, err := NewClient()
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 1
	}
	defer client.Close()
	if err := runTranscriptBridge(stdin, stdout, client.call); err != nil {
		fmt.Fprintln(stderr, err)
		return 1
	}
	return 0
}

type snapshotBusinessCall func(context.Context, string, []byte) ([]byte, error)

func runTranscriptBridge(stdin io.Reader, stdout io.Writer, call snapshotBusinessCall) (resultErr error) {
	hello, err := wireproto.DecodeFrameLimit(stdin, snapshots.MaxFrameBytes)
	if err != nil {
		return err
	}
	if hello.Protocol != wireproto.Schema || hello.Op != wireproto.OpHello || hello.Build != Build || hello.ID != 0 || hello.ParentID != 0 || hello.Request != nil || hello.Response != nil || hello.Adopt != nil || hello.Error != "" || len(hello.Snapshot)+len(hello.SnapshotConfig)+len(hello.SnapshotContext) != 0 {
		return errors.New("captain: transcript client requires exact build handshake")
	}
	if err := wireproto.EncodeFrameLimit(stdout, wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpHello, Build: Build}, snapshots.MaxFrameBytes); err != nil {
		return err
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var mu, writeMu sync.Mutex
	var wg sync.WaitGroup
	pending := make(map[uint64]context.CancelFunc)
	var outputErr error
	stopClosing := context.AfterFunc(ctx, func() {
		if input, ok := stdin.(io.Closer); ok {
			_ = input.Close()
		}
		if output, ok := stdout.(io.Closer); ok {
			_ = output.Close()
		}
	})
	defer func() {
		cancel()
		wg.Wait()
		stopClosing()
		resultErr = errors.Join(resultErr, outputErr)
	}()
	for {
		frame, err := wireproto.DecodeFrameLimit(stdin, snapshots.MaxFrameBytes)
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return err
		}
		if frame.ID == 0 || frame.ParentID != 0 || frame.Build != "" || frame.Request != nil || frame.Response != nil || frame.Adopt != nil || frame.Error != "" || len(frame.SnapshotContext)+len(frame.SnapshotConfig) != 0 {
			return errors.New("captain: invalid transcript client frame")
		}
		if frame.Op == wireproto.OpSnapshotCancel {
			if len(frame.Snapshot) != 0 {
				return errors.New("captain: snapshot cancellation has payload")
			}
			mu.Lock()
			stop := pending[frame.ID]
			mu.Unlock()
			if stop != nil {
				stop()
			}
			continue
		}
		if frame.Op != wireproto.OpSnapshotRequest {
			return errors.New("captain: invalid transcript client operation")
		}
		deadline, err := snapshotRequestDeadline(frame.Snapshot)
		if err != nil {
			return err
		}
		requestCtx, stop := context.WithDeadline(ctx, deadline)
		mu.Lock()
		if _, duplicate := pending[frame.ID]; duplicate {
			mu.Unlock()
			stop()
			return errors.New("captain: duplicate transcript client id")
		}
		if len(pending) >= 16 {
			mu.Unlock()
			stop()
			return errors.New("captain: transcript client capacity exhausted")
		}
		pending[frame.ID] = stop
		mu.Unlock()
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer stop()
			body, err := call(requestCtx, opTranscript, frame.Snapshot)
			response := wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpSnapshotResult, ID: frame.ID, Snapshot: body}
			if err == nil {
				err = snapshots.Validate("host-response", body)
			}
			if err != nil {
				response.Op = wireproto.OpError
				response.Error = err.Error()
				response.Snapshot = nil
			}
			writeMu.Lock()
			if ctx.Err() == nil {
				if err := wireproto.EncodeFrameLimit(stdout, response, snapshots.MaxFrameBytes); err != nil {
					outputErr = err
					cancel()
				}
			}
			writeMu.Unlock()
			mu.Lock()
			delete(pending, frame.ID)
			mu.Unlock()
		}()
	}
}
