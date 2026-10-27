package hookd

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/yasyf/captain-hook/internal/snapshots"
	"github.com/yasyf/captain-hook/internal/wireproto"
)

var warmUsageCounters = []string{"source_opens", "source_bytes_read", "bytes_decoded", "events_parsed", "cold_parses", "append_parses", "activity_lifts", "cache_hits", "inflight_joins", "generations_published", "generations_invalidated", "requests_cancelled", "requests_failed", "output_bytes", "transport_bytes", "nonincremental_lowering_calls", "nonincremental_lowering_source_bytes", "discovery_entries_examined", "prepared_cache_reads", "prepared_cache_bytes_read"}

func warmLimits() map[string]any {
	return map[string]any{"max_read_bytes": 8388608, "max_source_read_bytes": 4194304, "max_events": 1000000, "max_items": 65536, "max_output_bytes": 16777216, "max_discovery_entries": 50000, "max_sources": 4096}
}

func registeredDemand() map[string]any {
	return map[string]any{"schema": "captain.transcript/1", "tool_registry": []any{}, "request": map[string]any{
		"schema": "cc-transcript.snapshot/1", "operation": "warm_registered", "classifier": map[string]any{"id": "native", "version": "1"},
		"thread_ids": []any{"thread"}, "roots": []any{"/tmp"}, "direct_paths": []any{}, "active_since_unix_ms": nil, "limits": warmLimits(),
	}}
}

func rootDemand() map[string]any {
	return map[string]any{"schema": "captain.transcript/1", "tool_registry": []any{}, "request": map[string]any{
		"schema": "cc-transcript.snapshot/1", "operation": "warm_root", "path": "/tmp/root.jsonl", "classifier": map[string]any{"id": "native", "version": "1"},
		"tail_bytes": nil, "limits": warmLimits(),
	}}
}

func encodeDemand(t *testing.T, demand map[string]any) json.RawMessage {
	t.Helper()
	body, err := wireproto.Marshal(demand)
	if err != nil {
		t.Fatal(err)
	}
	return body
}

func warmCaller(namespace int) snapshots.CallContext {
	callContext := userSnapshotContext(fmt.Sprintf("worker:%d", namespace), "hook", 501)
	callContext.WorkClass = "background"
	return callContext
}

func registryProgress(next int, complete bool, epoch, revision string, offset int) map[string]any {
	return map[string]any{"kind": "warmed_registry", "owner_epoch": epoch, "membership_revision": revision, "next_index": next, "complete": complete,
		"source_offset": offset, "source_size": 1 << 20, "fact_cache_bytes": 0, "fact_cache_write_bytes": 0, "fact_cache_writes": 0}
}

func rootProgress(complete, facts bool, epoch, revision string, offset int) map[string]any {
	return map[string]any{"kind": "warmed_root", "owner_epoch": epoch, "source_revision": revision, "source_offset": offset, "source_size": 1 << 20, "complete": complete, "facts_complete": facts}
}

type warmResult struct {
	status string
	data   map[string]any
	usage  map[string]int64
	err    error
}

func warmReplyBody(t *testing.T, id string, result warmResult) json.RawMessage {
	t.Helper()
	usage := make(map[string]int64, len(warmUsageCounters))
	for _, name := range warmUsageCounters {
		usage[name] = result.usage[name]
	}
	response := map[string]any{"schema": "cc-transcript.snapshot/1", "id": id, "status": result.status, "complete": result.status == "ok", "data": result.data, "cursor": nil, "reason": "fixture", "usage": usage}
	if result.status == "ok" {
		response["reason"] = nil
	}
	body, err := wireproto.Marshal(map[string]any{"schema": "captain.transcript/1", "response": response})
	if err != nil {
		t.Fatal(err)
	}
	if err := snapshots.Validate("host-response", body); err != nil {
		t.Fatalf("fixture reply %s: %v", body, err)
	}
	return body
}

type warmCall struct {
	id          string
	fields      map[string]json.RawMessage
	callContext snapshots.CallContext
}

func (c warmCall) cursor() string {
	return fmt.Sprintf("%s %s", c.fields["start_index"], c.fields["membership_revision"])
}

func decodeWarmStep(t *testing.T, body json.RawMessage, callContext snapshots.CallContext) warmCall {
	t.Helper()
	metadata, err := snapshots.Metadata(body)
	if err != nil {
		t.Fatalf("host step %s: %v", body, err)
	}
	var envelope struct {
		Request map[string]json.RawMessage `json:"request"`
	}
	if err := json.Unmarshal(body, &envelope); err != nil {
		t.Fatal(err)
	}
	return warmCall{id: metadata.ID, fields: envelope.Request, callContext: callContext}
}

func answerWarmFrame(t *testing.T, owner net.Conn, frame wireproto.Frame, result warmResult) warmCall {
	t.Helper()
	if frame.Op != wireproto.OpSnapshotRequest {
		t.Fatalf("owner frame=%+v, want a snapshot request", frame)
	}
	var callContext snapshots.CallContext
	if err := json.Unmarshal(frame.SnapshotContext, &callContext); err != nil {
		t.Fatal(err)
	}
	call := decodeWarmStep(t, frame.Snapshot, callContext)
	reply := wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpSnapshotResult, ID: frame.ID, Snapshot: warmReplyBody(t, call.id, result)}
	if err := wireproto.EncodeFrameLimit(owner, reply, snapshots.MaxFrameBytes); err != nil {
		t.Fatal(err)
	}
	return call
}

func awaitWarmIdle(t *testing.T, coordinator *warmCoordinator) {
	t.Helper()
	idle := make(chan struct{})
	go func() { coordinator.wg.Wait(); close(idle) }()
	select {
	case <-idle:
	case <-time.After(time.Second):
		t.Fatal("warm driver did not go idle")
	}
}

type warmStepRequest struct {
	body        json.RawMessage
	callContext snapshots.CallContext
}

type warmHarness struct {
	t           *testing.T
	clock       *fakeClock
	coordinator *warmCoordinator
	requests    chan warmStepRequest
	results     chan snapshotResult
	log         bytes.Buffer
	mu          sync.Mutex
	pauses      []time.Duration
}

func newWarmHarness(t *testing.T) *warmHarness {
	t.Helper()
	h := &warmHarness{t: t, clock: &fakeClock{now: time.Unix(1_700_000_000, 0)}, requests: make(chan warmStepRequest, 8), results: make(chan snapshotResult, 1)}
	h.coordinator = newWarmCoordinator(h.step, h.clock.Now, &h.log)
	h.coordinator.pause = h.pause
	t.Cleanup(h.coordinator.close)
	return h
}

func (h *warmHarness) step(ctx context.Context, request json.RawMessage, callContext snapshots.CallContext) (json.RawMessage, error) {
	h.requests <- warmStepRequest{body: request, callContext: callContext}
	select {
	case result := <-h.results:
		return result.body, result.err
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func (h *warmHarness) pause(_ context.Context, wait time.Duration) error {
	h.mu.Lock()
	h.pauses = append(h.pauses, wait)
	h.mu.Unlock()
	h.clock.Advance(wait)
	return nil
}

func (h *warmHarness) paused() []time.Duration {
	h.mu.Lock()
	defer h.mu.Unlock()
	return slices.Clone(h.pauses)
}

func (h *warmHarness) demand(demand map[string]any, caller int) string {
	h.t.Helper()
	body := encodeDemand(h.t, demand)
	if err := h.coordinator.demand(body, warmCaller(caller)); err != nil {
		h.t.Fatal(err)
	}
	key, err := warmKey(body, warmCaller(caller))
	if err != nil {
		h.t.Fatal(err)
	}
	return key
}

func (h *warmHarness) receive() warmCall {
	h.t.Helper()
	select {
	case request := <-h.requests:
		return decodeWarmStep(h.t, request.body, request.callContext)
	case <-time.After(time.Second):
		h.t.Fatal("warm step timed out")
		return warmCall{}
	}
}

func (h *warmHarness) reply(call warmCall, result warmResult) {
	h.t.Helper()
	if result.err != nil {
		h.results <- snapshotResult{err: result.err}
		return
	}
	h.results <- snapshotResult{body: warmReplyBody(h.t, call.id, result)}
}

func (h *warmHarness) answer(result warmResult) warmCall {
	h.t.Helper()
	call := h.receive()
	h.reply(call, result)
	return call
}

func (h *warmHarness) settled() {
	h.t.Helper()
	awaitWarmIdle(h.t, h.coordinator)
	select {
	case extra := <-h.requests:
		h.t.Fatalf("idle warm driver issued another step: %s", extra.body)
	default:
	}
}

func (h *warmHarness) job(key string) *warmJob {
	h.coordinator.mu.Lock()
	defer h.coordinator.mu.Unlock()
	job, ok := h.coordinator.jobs[key]
	if !ok {
		return nil
	}
	held := *job
	return &held
}

func (h *warmHarness) jobs() int {
	h.coordinator.mu.Lock()
	defer h.coordinator.mu.Unlock()
	return len(h.coordinator.jobs)
}

func (h *warmHarness) logged() string {
	h.coordinator.mu.Lock()
	defer h.coordinator.mu.Unlock()
	return h.log.String()
}

func TestWarmDemandsForOneScopeShareOneJobAndCursor(t *testing.T) {
	h := newWarmHarness(t)
	var key string
	for caller := range 8 {
		key = h.demand(registeredDemand(), caller)
	}
	if jobs := h.jobs(); jobs != 1 {
		t.Fatalf("jobs=%d, want eight callers joined on one scope", jobs)
	}
	deadline := strconv.FormatInt(h.clock.Now().Add(warmStepTimeout).UnixMilli(), 10)
	ids := map[string]bool{}
	for index, step := range []struct {
		cursor string
		result warmResult
	}{
		{"0 null", warmResult{status: "ok", data: registryProgress(8, false, "epoch-1", "rev-1", 0)}},
		{`8 "rev-1"`, warmResult{status: "ok", data: registryProgress(16, false, "epoch-1", "rev-1", 0)}},
		{`16 "rev-1"`, warmResult{status: "ok", data: registryProgress(20, true, "epoch-1", "rev-1", 0)}},
	} {
		call := h.answer(step.result)
		if got := call.cursor(); got != step.cursor {
			t.Fatalf("step %d cursor=%s, want %s", index, got, step.cursor)
		}
		if call.callContext.Claimant != "warm:"+key[:32] || call.callContext.Admission != "hook" || call.callContext.WorkClass != "background" || call.callContext.Authority.EffectiveUID != "501" {
			t.Fatalf("step %d context=%+v", index, call.callContext)
		}
		if got := string(call.fields["deadline_unix_ms"]); got != deadline {
			t.Fatalf("step %d deadline=%s, want %s", index, got, deadline)
		}
		var limits map[string]int64
		if err := json.Unmarshal(call.fields["limits"], &limits); err != nil {
			t.Fatal(err)
		}
		if limits["max_source_read_bytes"] != warmStepBytes || limits["max_read_bytes"] != 8388608 || len(limits) != 7 {
			t.Fatalf("step %d limits=%v, want the demand's limits with the host step size", index, limits)
		}
		ids[call.id] = true
	}
	h.settled()
	if len(ids) != 3 {
		t.Fatalf("step ids=%v, want three distinct ids", ids)
	}
	if jobs := h.jobs(); jobs != 0 {
		t.Fatalf("jobs=%d, want the complete job removed", jobs)
	}
	if want := fmt.Sprintf("captain: snapshot warm warm_registered %.16s complete after 3 steps source_bytes_read=0 prepared_cache_bytes_read=0 events_parsed=0\n", key); h.logged() != want {
		t.Fatalf("log=%q, want %q", h.logged(), want)
	}
	if pauses := h.paused(); len(pauses) != 0 {
		t.Fatalf("pauses=%v, want none for steps that read nothing", pauses)
	}
}

func TestWarmScopeKeyCoversEveryInvalidationInput(t *testing.T) {
	for _, tc := range []struct {
		name   string
		demand func() map[string]any
		mutate func(demand, request map[string]any, callContext *snapshots.CallContext)
	}{
		{"thread ids", registeredDemand, func(_, request map[string]any, _ *snapshots.CallContext) { request["thread_ids"] = []any{"other"} }},
		{"roots", registeredDemand, func(_, request map[string]any, _ *snapshots.CallContext) { request["roots"] = []any{"/var"} }},
		{"direct paths", registeredDemand, func(_, request map[string]any, _ *snapshots.CallContext) {
			request["direct_paths"] = []any{"/tmp/direct.jsonl"}
		}},
		{"active window", registeredDemand, func(_, request map[string]any, _ *snapshots.CallContext) {
			request["active_since_unix_ms"] = int64(1_700_000_000_000)
		}},
		{"tool registry", registeredDemand, func(demand, _ map[string]any, _ *snapshots.CallContext) {
			demand["tool_registry"] = []any{map[string]any{"name": "Shell", "behaves_like": "Bash", "span_edit": nil}}
		}},
		{"authority", registeredDemand, func(_, _ map[string]any, callContext *snapshots.CallContext) {
			callContext.Authority.EffectiveUID = "502"
		}},
		{"admission", registeredDemand, func(_, _ map[string]any, callContext *snapshots.CallContext) { callContext.Admission = "review" }},
		{"classifier", registeredDemand, func(_, request map[string]any, _ *snapshots.CallContext) {
			request["classifier"] = map[string]any{"id": "native", "version": "2"}
		}},
		{"limits", registeredDemand, func(_, request map[string]any, _ *snapshots.CallContext) {
			request["limits"].(map[string]any)["max_events"] = 10
		}},
		{"root path", rootDemand, func(_, request map[string]any, _ *snapshots.CallContext) { request["path"] = "/tmp/other.jsonl" }},
		{"root tail window", rootDemand, func(_, request map[string]any, _ *snapshots.CallContext) { request["tail_bytes"] = 4096 }},
		{"root classifier", rootDemand, func(_, request map[string]any, _ *snapshots.CallContext) {
			request["classifier"] = map[string]any{"id": "native", "version": "2"}
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := newWarmHarness(t)
			key := h.demand(tc.demand(), 1)
			if again := h.demand(tc.demand(), 2); again != key || h.jobs() != 1 {
				t.Fatalf("the same demand from another claimant keyed %s with %d jobs, want %s on one job", again, h.jobs(), key)
			}
			demand, callContext := tc.demand(), warmCaller(1)
			tc.mutate(demand, demand["request"].(map[string]any), &callContext)
			body := encodeDemand(t, demand)
			changed, err := warmKey(body, callContext)
			if err != nil {
				t.Fatal(err)
			}
			if err := h.coordinator.demand(body, callContext); err != nil {
				t.Fatal(err)
			}
			if changed == key || h.jobs() != 2 {
				t.Fatalf("a changed %s keyed %s with %d jobs, want a second scope beside %s", tc.name, changed, h.jobs(), key)
			}
		})
	}
}

func TestCompletedWarmDemandIsDebouncedAcrossWorkersFromItsAcceptance(t *testing.T) {
	for _, tc := range []struct {
		name     string
		demand   func() map[string]any
		progress map[string]any
	}{
		{"registered", registeredDemand, registryProgress(8, true, "epoch-1", "rev-1", 0)},
		{"root", rootDemand, rootProgress(true, true, "epoch-1", "src-1", 0)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := newWarmHarness(t)
			key := h.demand(tc.demand(), 1)
			call := h.receive()
			h.clock.Advance(4 * time.Second)
			h.reply(call, warmResult{status: "ok", data: tc.progress})
			h.settled()
			for caller := 2; caller < 10; caller++ {
				if again := h.demand(tc.demand(), caller); again != key || h.jobs() != 0 {
					t.Fatalf("caller %d key=%s jobs=%d, want completed scope %s suppressed", caller, again, h.jobs(), key)
				}
			}
			h.clock.Advance(5 * time.Second)
			h.demand(tc.demand(), 10)
			h.settled()
			h.clock.Advance(time.Second)
			h.demand(tc.demand(), 11)
			h.answer(warmResult{status: "ok", data: tc.progress})
			h.settled()
			if got := strings.Count(h.logged(), "complete after 1 steps"); got != 2 {
				t.Fatalf("completed runs=%d, want one before and one at the debounce boundary", got)
			}
		})
	}
}

func TestCompletedWarmDemandDoesNotSuppressAChangedBinding(t *testing.T) {
	for _, tc := range []struct {
		name   string
		mutate func(map[string]any, *snapshots.CallContext)
	}{
		{"registry", func(demand map[string]any, _ *snapshots.CallContext) {
			demand["tool_registry"] = []any{map[string]any{"name": "Shell", "behaves_like": "Bash", "span_edit": nil}}
		}},
		{"path", func(demand map[string]any, _ *snapshots.CallContext) {
			demand["request"].(map[string]any)["path"] = "/tmp/other.jsonl"
		}},
		{"authority", func(_ map[string]any, caller *snapshots.CallContext) {
			caller.Authority.EffectiveUID = "502"
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := newWarmHarness(t)
			h.demand(rootDemand(), 1)
			h.answer(warmResult{status: "ok", data: rootProgress(true, true, "epoch-1", "src-1", 0)})
			h.settled()
			demand, caller := rootDemand(), warmCaller(2)
			tc.mutate(demand, &caller)
			if err := h.coordinator.demand(encodeDemand(t, demand), caller); err != nil {
				t.Fatal(err)
			}
			h.answer(warmResult{status: "ok", data: rootProgress(true, true, "epoch-1", "src-2", 0)})
			h.settled()
			if got := strings.Count(h.logged(), "complete after 1 steps"); got != 2 {
				t.Fatalf("completed runs=%d, want both bindings executed", got)
			}
		})
	}
}

func TestFailedWarmDemandCanBeDemandedAgainImmediately(t *testing.T) {
	for _, tc := range []struct {
		name   string
		result warmResult
		steps  int
	}{
		{"refused", warmResult{status: "permission_denied"}, 1},
		{"cancelled", warmResult{status: "cancelled"}, 2},
		{"deadline", warmResult{status: "deadline"}, 2},
		{"transport", warmResult{err: errors.New("fixture")}, 2},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := newWarmHarness(t)
			key := h.demand(rootDemand(), 1)
			for range tc.steps {
				h.answer(tc.result)
			}
			h.settled()
			h.demand(rootDemand(), 2)
			if h.job(key) == nil {
				t.Fatal("the unsuccessful job suppressed another demand")
			}
			h.answer(warmResult{status: "ok", data: rootProgress(true, true, "epoch-1", "src-1", 0)})
			h.settled()
		})
	}
}

func TestCompletedWarmDemandHistoryStaysBounded(t *testing.T) {
	h := newWarmHarness(t)
	keys := make([]string, maxWarmJobs+1)
	for index := range keys {
		demand := rootDemand()
		demand["request"].(map[string]any)["path"] = fmt.Sprintf("/tmp/root-%d.jsonl", index)
		keys[index] = h.demand(demand, index)
		h.answer(warmResult{status: "ok", data: rootProgress(true, true, "epoch-1", "src-1", 0)})
		h.settled()
		h.clock.Advance(time.Millisecond)
	}
	h.coordinator.mu.Lock()
	defer h.coordinator.mu.Unlock()
	if len(h.coordinator.completed) != maxWarmJobs {
		t.Fatalf("completed history=%d, want %d", len(h.coordinator.completed), maxWarmJobs)
	}
	if _, kept := h.coordinator.completed[keys[0]]; kept {
		t.Fatal("the oldest completed demand was retained at capacity")
	}
	for _, key := range keys[1:] {
		if _, kept := h.coordinator.completed[key]; !kept {
			t.Fatalf("newer completed demand %s was evicted", key)
		}
	}
}

func TestWarmCursorResetsWhenTheScopeChangesUnderIt(t *testing.T) {
	for _, tc := range []struct {
		name   string
		second warmResult
		cursor string
		final  map[string]any
	}{
		{"changed status", warmResult{status: "changed"}, "0 null", registryProgress(4, true, "epoch-1", "rev-1", 0)},
		{"membership revision", warmResult{status: "ok", data: registryProgress(16, true, "epoch-1", "rev-2", 8192)}, `0 "rev-2"`, registryProgress(4, true, "epoch-1", "rev-2", 0)},
		{"owner epoch", warmResult{status: "ok", data: registryProgress(16, true, "epoch-2", "rev-1", 8192)}, `0 "rev-1"`, registryProgress(4, true, "epoch-2", "rev-1", 0)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := newWarmHarness(t)
			key := h.demand(registeredDemand(), 0)
			h.answer(warmResult{status: "ok", data: registryProgress(8, false, "epoch-1", "rev-1", 4096)})
			if got := h.answer(tc.second).cursor(); got != `8 "rev-1"` {
				t.Fatalf("cursor before the change=%s, want 8 \"rev-1\"", got)
			}
			call := h.receive()
			if got := call.cursor(); got != tc.cursor {
				t.Fatalf("cursor after the change=%s, want %s", got, tc.cursor)
			}
			if job := h.job(key); job == nil || job.startIndex != 0 || job.sourceOffset != 0 || job.stalled != 0 {
				t.Fatalf("job after the change=%+v, want a reset cursor on the same job", job)
			}
			h.reply(call, warmResult{status: "ok", data: tc.final})
			h.settled()
			if jobs := h.jobs(); jobs != 0 {
				t.Fatalf("jobs=%d, want the rewalked job complete", jobs)
			}
		})
	}
}

func TestWarmJobKeepsItsCursorWhileDemandLapses(t *testing.T) {
	h := newWarmHarness(t)
	key := h.demand(registeredDemand(), 0)
	call := h.receive()
	h.clock.Advance(warmDemandWindow)
	h.reply(call, warmResult{status: "ok", data: registryProgress(8, false, "epoch-1", "rev-1", 0)})
	call = h.receive()
	if got := call.cursor(); got != `8 "rev-1"` {
		t.Fatalf("cursor one full window after the demand=%s, want 8 \"rev-1\"", got)
	}
	h.clock.Advance(time.Nanosecond)
	h.reply(call, warmResult{status: "ok", data: registryProgress(16, false, "epoch-1", "rev-1", 0)})
	h.settled()
	if job := h.job(key); job == nil || job.startIndex != 16 || job.membershipRevision != "rev-1" || job.steps != 2 {
		t.Fatalf("dormant job=%+v, want its cursor retained after two steps", job)
	}
	h.demand(registeredDemand(), 1)
	if got := h.answer(warmResult{status: "ok", data: registryProgress(20, true, "epoch-1", "rev-1", 0)}).cursor(); got != `16 "rev-1"` {
		t.Fatalf("resumed cursor=%s, want 16 \"rev-1\"", got)
	}
	h.settled()
	if jobs := h.jobs(); jobs != 0 {
		t.Fatalf("jobs=%d, want the resumed job complete", jobs)
	}
}

func TestWarmAllowanceChargesSourceAndPreparedCacheBytes(t *testing.T) {
	read := func(usage map[string]int64) warmResult {
		return warmResult{status: "ok", data: registryProgress(1, false, "epoch-1", "rev-1", 0), usage: usage}
	}
	for _, tc := range []struct {
		name       string
		direct     map[string]int64
		unanswered int
		first      warmResult
		pauses     []time.Duration
	}{
		{name: "source and prepared cache bytes", first: read(map[string]int64{"source_bytes_read": 2 << 20, "prepared_cache_bytes_read": 6 << 20}), pauses: []time.Duration{2 * time.Second}},
		{name: "no bytes", first: read(nil)},
		{name: "prepared cache bytes alone", first: read(map[string]int64{"prepared_cache_bytes_read": 4 << 20}), pauses: []time.Duration{time.Second}},
		{name: "unanswered step", first: warmResult{err: errors.New("fixture")}, pauses: []time.Duration{2 * time.Second}},
		{name: "direct step", direct: map[string]int64{"source_bytes_read": 1 << 20, "prepared_cache_bytes_read": 1 << 20}, first: read(nil), pauses: []time.Duration{time.Second / 2}},
		{name: "unanswered direct steps add no debt", unanswered: 2, first: read(nil), pauses: []time.Duration{2 * time.Second}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := newWarmHarness(t)
			if tc.direct != nil {
				h.coordinator.charge(decodeWarmReply(warmReplyBody(t, "direct", warmResult{status: "deadline", usage: tc.direct}), nil))
			}
			for range tc.unanswered {
				h.coordinator.charge(decodeWarmReply(nil, errors.New("fixture")))
			}
			h.demand(registeredDemand(), 0)
			h.answer(tc.first)
			h.answer(warmResult{status: "ok", data: registryProgress(2, true, "epoch-1", "rev-1", 0)})
			h.settled()
			if got := h.paused(); !slices.Equal(got, tc.pauses) {
				t.Fatalf("pauses=%v, want %v", got, tc.pauses)
			}
		})
	}
}

func TestWarmJobStopsAfterTwoStepsWithoutProgress(t *testing.T) {
	atEight := warmResult{status: "ok", data: registryProgress(8, false, "epoch-1", "rev-1", 0)}
	unanswered := warmResult{err: errors.New("fixture")}
	for _, tc := range []struct {
		name    string
		results []warmResult
		stopped bool
	}{
		{"step errors", []warmResult{unanswered, unanswered}, true},
		{"stall statuses", []warmResult{{status: "deadline"}, {status: "retained_limit"}}, true},
		{"steps that move nothing", []warmResult{atEight, atEight}, true},
		{"a step error then a step that moves nothing", []warmResult{unanswered, atEight}, true},
		{"progress between stalls", []warmResult{unanswered, {status: "ok", data: registryProgress(16, false, "epoch-1", "rev-1", 0)}, {status: "lease_limit"}}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := newWarmHarness(t)
			key := h.demand(registeredDemand(), 0)
			h.answer(atEight)
			for _, result := range tc.results {
				h.answer(result)
			}
			complete := warmResult{status: "ok", data: registryProgress(20, true, "epoch-1", "rev-1", 0)}
			if !tc.stopped {
				if got := h.answer(complete).cursor(); got != `16 "rev-1"` {
					t.Fatalf("cursor after one stall=%s, want 16 \"rev-1\"", got)
				}
				h.settled()
				return
			}
			h.settled()
			if job := h.job(key); job != nil {
				t.Fatalf("stalled job=%+v, want it removed", job)
			}
			if want := fmt.Sprintf("captain: snapshot warm warm_registered %.16s stopped after 3 steps: no progress source_bytes_read=0 prepared_cache_bytes_read=0 events_parsed=0\n", key); h.logged() != want {
				t.Fatalf("log=%q, want %q", h.logged(), want)
			}
			h.demand(registeredDemand(), 1)
			if got := h.answer(complete).cursor(); got != "0 null" {
				t.Fatalf("cursor after the stalled job was dropped=%s, want 0 null", got)
			}
			h.settled()
		})
	}
}

func TestWarmStallStatusesKeepTheCursorUntilTheLimit(t *testing.T) {
	for _, status := range []string{"incomplete", "deadline", "retained_limit", "lease_limit", "cancelled"} {
		t.Run(status, func(t *testing.T) {
			job := &warmJob{operation: "warm_registered", startIndex: 8, membershipRevision: "rev-1"}
			for attempt, want := range []string{"", "no progress"} {
				complete, stopped := job.advance(&warmReply{Status: status})
				if complete || stopped != want || job.stalled != attempt+1 || job.startIndex != 8 || job.membershipRevision != "rev-1" {
					t.Fatalf("attempt %d: complete=%t stopped=%q job=%+v", attempt, complete, stopped, job)
				}
			}
		})
	}
}

func TestWarmJobStopsOnAHardFailure(t *testing.T) {
	for _, tc := range []struct {
		name   string
		demand func() map[string]any
		result warmResult
		why    string
	}{
		{"registered permission denied", registeredDemand, warmResult{status: "permission_denied"}, "permission_denied: fixture"},
		{"root changed", rootDemand, warmResult{status: "changed"}, "changed: fixture"},
		{"registered answered as a root", registeredDemand, warmResult{status: "ok", data: rootProgress(true, true, "epoch-1", "src-1", 0)}, "invalid progress"},
		{"root answered as a registry", rootDemand, warmResult{status: "ok", data: registryProgress(1, true, "epoch-1", "rev-1", 0)}, "invalid progress"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := newWarmHarness(t)
			demand := tc.demand()
			key := h.demand(demand, 0)
			h.answer(tc.result)
			h.settled()
			if job := h.job(key); job != nil {
				t.Fatalf("failed job=%+v, want it removed", job)
			}
			if want := fmt.Sprintf("captain: snapshot warm %s %.16s stopped after 1 steps: %s source_bytes_read=0 prepared_cache_bytes_read=0 events_parsed=0\n", demand["request"].(map[string]any)["operation"], key, tc.why); h.logged() != want {
				t.Fatalf("log=%q, want %q", h.logged(), want)
			}
		})
	}
}

func TestWarmJobLogsItsCumulativeUsage(t *testing.T) {
	usage := func(source, prepared, events int64) map[string]int64 {
		return map[string]int64{"source_bytes_read": source, "prepared_cache_bytes_read": prepared, "events_parsed": events}
	}
	for _, tc := range []struct {
		name    string
		results []warmResult
		ending  string
	}{
		{"complete across a changed restart", []warmResult{
			{status: "ok", data: registryProgress(8, false, "epoch-1", "rev-1", 0), usage: usage(1000, 20, 3)},
			{status: "changed", usage: usage(400, 0, 50)},
			{status: "ok", data: registryProgress(20, true, "epoch-1", "rev-1", 0), usage: usage(0, 6000, 700)},
		}, "complete after 3 steps source_bytes_read=1400 prepared_cache_bytes_read=6020 events_parsed=753"},
		{"stopped after an unanswered step", []warmResult{
			{status: "ok", data: registryProgress(8, false, "epoch-1", "rev-1", 0), usage: usage(2000, 30, 4)},
			{err: errors.New("fixture")},
			{status: "permission_denied", usage: usage(500, 70, 0)},
		}, "stopped after 3 steps: permission_denied: fixture source_bytes_read=2500 prepared_cache_bytes_read=100 events_parsed=4"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := newWarmHarness(t)
			key := h.demand(registeredDemand(), 0)
			for _, result := range tc.results {
				h.answer(result)
			}
			h.settled()
			if jobs := h.jobs(); jobs != 0 {
				t.Fatalf("jobs=%d, want the job ended", jobs)
			}
			if want := fmt.Sprintf("captain: snapshot warm warm_registered %.16s %s\n", key, tc.ending); h.logged() != want {
				t.Fatalf("log=%q, want %q", h.logged(), want)
			}
		})
	}
}

func TestWarmRootJobCompletesOnlyWithItsFacts(t *testing.T) {
	h := newWarmHarness(t)
	key := h.demand(rootDemand(), 0)
	for index, progress := range []map[string]any{
		rootProgress(true, false, "epoch-1", "src-1", 4096),
		rootProgress(false, true, "epoch-1", "src-1", 4096),
		rootProgress(true, true, "epoch-1", "src-1", 4096),
	} {
		call := h.answer(warmResult{status: "ok", data: progress})
		_, indexed := call.fields["start_index"]
		_, revised := call.fields["membership_revision"]
		if indexed || revised || string(call.fields["operation"]) != `"warm_root"` || string(call.fields["path"]) != `"/tmp/root.jsonl"` || call.callContext.Claimant != "warm:"+key[:32] {
			t.Fatalf("root step %d=%s context=%+v, want a cursorless warm_root step", index, call.fields, call.callContext)
		}
	}
	h.settled()
	if jobs := h.jobs(); jobs != 0 {
		t.Fatalf("jobs=%d, want the root job complete", jobs)
	}
	if want := fmt.Sprintf("captain: snapshot warm warm_root %.16s complete after 3 steps source_bytes_read=0 prepared_cache_bytes_read=0 events_parsed=0\n", key); h.logged() != want {
		t.Fatalf("log=%q, want %q", h.logged(), want)
	}
}

func TestWarmCapacityEvictsTheLeastRecentlyDemandedJob(t *testing.T) {
	scope := func(index int) map[string]any {
		demand := registeredDemand()
		demand["request"].(map[string]any)["thread_ids"] = []any{fmt.Sprintf("thread-%d", index)}
		return demand
	}
	for _, tc := range []struct {
		name     string
		lapse    bool
		victim   int
		survivor int
		steps    int
	}{
		{"a lapsed job before the renewed oldest one", true, 1, 0, 0},
		{"the oldest demand when none lapsed", false, 0, 1, 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := newWarmHarness(t)
			keys := make([]string, maxWarmJobs)
			for index := range maxWarmJobs {
				keys[index] = h.demand(scope(index), 0)
				h.clock.Advance(time.Second)
			}
			h.receive()
			if tc.lapse {
				h.clock.Advance(warmDemandWindow - (maxWarmJobs-2)*time.Second)
				h.demand(scope(0), 0)
			}
			added := h.demand(scope(maxWarmJobs), 0)
			if jobs := h.jobs(); jobs != maxWarmJobs {
				t.Fatalf("jobs=%d, want the table held at %d", jobs, maxWarmJobs)
			}
			if h.job(keys[tc.victim]) != nil || h.job(keys[tc.survivor]) == nil || h.job(added) == nil {
				t.Fatalf("victim kept=%t survivor kept=%t added kept=%t", h.job(keys[tc.victim]) != nil, h.job(keys[tc.survivor]) != nil, h.job(added) != nil)
			}
			if want := fmt.Sprintf("captain: snapshot warm warm_registered %.16s stopped after %d steps: evicted source_bytes_read=0 prepared_cache_bytes_read=0 events_parsed=0\n", keys[tc.victim], tc.steps); h.logged() != want {
				t.Fatalf("log=%q, want %q", h.logged(), want)
			}
		})
	}
}

func TestWarmDemandValidation(t *testing.T) {
	mutated := func(mutate func(demand, request map[string]any)) func(*testing.T) json.RawMessage {
		return func(t *testing.T) json.RawMessage {
			demand := registeredDemand()
			mutate(demand, demand["request"].(map[string]any))
			return encodeDemand(t, demand)
		}
	}
	for _, tc := range []struct {
		name   string
		body   func(*testing.T) json.RawMessage
		reason string
		target error
	}{
		{name: "request id", body: mutated(func(_, request map[string]any) { request["id"] = "mine" }), reason: "host-owned id"},
		{name: "request deadline", body: mutated(func(_, request map[string]any) { request["deadline_unix_ms"] = int64(9_000_000_000_000) }), reason: "host-owned deadline_unix_ms"},
		{name: "request start index", body: mutated(func(_, request map[string]any) { request["start_index"] = 0 }), reason: "host-owned start_index"},
		{name: "request membership revision", body: mutated(func(_, request map[string]any) { request["membership_revision"] = nil }), reason: "host-owned membership_revision"},
		{name: "another operation", body: mutated(func(_, request map[string]any) { request["operation"] = "stats" }), reason: "names no warm operation"},
		{name: "unknown envelope field", body: mutated(func(demand, _ map[string]any) { demand["priority"] = "high" }), reason: "unknown field"},
		{name: "unknown request field", body: mutated(func(_, request map[string]any) { request["priority"] = "high" })},
		{name: "missing limits", body: mutated(func(_, request map[string]any) { delete(request, "limits") }), reason: "has no limits"},
		{name: "null limits", body: mutated(func(_, request map[string]any) { request["limits"] = nil }), reason: "has no limits"},
		{name: "trailing JSON", body: func(t *testing.T) json.RawMessage { return append(encodeDemand(t, registeredDemand()), "{}"...) }, reason: "trailing JSON"},
		{name: "empty body", body: func(*testing.T) json.RawMessage { return nil }, target: wireproto.ErrPayloadTooLarge},
		{name: "body past the step headroom", body: func(t *testing.T) json.RawMessage {
			body := encodeDemand(t, registeredDemand())
			return append(body, bytes.Repeat([]byte(" "), snapshots.MaxFrameBytes-warmDemandHeadroom+1-len(body))...)
		}, target: wireproto.ErrPayloadTooLarge},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := newWarmHarness(t)
			err := h.coordinator.demand(tc.body(t), warmCaller(0))
			if err == nil || !strings.Contains(err.Error(), tc.reason) || (tc.target != nil && !errors.Is(err, tc.target)) {
				t.Fatalf("demand error=%v, want one naming %q matching %v", err, tc.reason, tc.target)
			}
			h.settled()
			if jobs := h.jobs(); jobs != 0 {
				t.Fatalf("jobs=%d, want a rejected demand to start no job", jobs)
			}
		})
	}
}

func TestWarmDemandAtItsSizeBoundStaysSteppable(t *testing.T) {
	h := newWarmHarness(t)
	demand := registeredDemand()
	request := demand["request"].(map[string]any)
	paths := make([]any, 255)
	for i := range paths {
		paths[i] = "/" + strings.Repeat("p", 4095)
	}
	request["direct_paths"] = append(paths, "/")
	request["direct_paths"] = append(paths, "/"+strings.Repeat("p", snapshots.MaxFrameBytes-warmDemandHeadroom-len(encodeDemand(t, demand))))
	if size := len(encodeDemand(t, demand)); size != snapshots.MaxFrameBytes-warmDemandHeadroom {
		t.Fatalf("demand=%d bytes, want exactly the bound %d", size, snapshots.MaxFrameBytes-warmDemandHeadroom)
	}
	h.demand(demand, 0)
	revision := strings.Repeat("f", 64)
	if got := h.answer(warmResult{status: "ok", data: registryProgress(1<<53-1, false, "epoch-1", revision, 0)}).cursor(); got != "0 null" {
		t.Fatalf("first cursor=%s, want 0 null", got)
	}
	if got, want := h.answer(warmResult{status: "ok", data: registryProgress(1<<53-1, true, "epoch-1", revision, 0)}).cursor(), fmt.Sprintf("%d %q", 1<<53-1, revision); got != want {
		t.Fatalf("cursor at the size bound=%s, want %s inside one frame", got, want)
	}
	h.settled()
	if jobs := h.jobs(); jobs != 0 {
		t.Fatalf("jobs=%d, want the bound-sized scope complete", jobs)
	}
}

func TestWarmCloseCancelsTheStepInFlightWithoutSettlingIt(t *testing.T) {
	h := newWarmHarness(t)
	key := h.demand(registeredDemand(), 0)
	h.receive()
	h.coordinator.close()
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := h.coordinator.wait(ctx); err != nil {
		t.Fatalf("wait=%v, want the driver to leave its step in flight", err)
	}
	if job := h.job(key); job == nil || job.stalled != 0 || job.steps != 1 {
		t.Fatalf("job after close=%+v, want the cancelled step left unsettled", job)
	}
	h.coordinator.mu.Lock()
	ready := h.coordinator.ready
	h.coordinator.mu.Unlock()
	if !ready.IsZero() {
		t.Fatalf("allowance ready=%v, want the cancelled step uncharged", ready)
	}
	if err := h.coordinator.demand(json.RawMessage(`{`), warmCaller(0)); err != nil {
		t.Fatalf("demand after close=%v, want a no-op", err)
	}
	h.demand(rootDemand(), 0)
	if jobs := h.jobs(); jobs != 1 {
		t.Fatalf("jobs=%d, want no job admitted after close", jobs)
	}
	h.settled()
}

func TestSnapshotWarmFrameCarriesOnlyADemand(t *testing.T) {
	service, err := newSnapshotService(&workerManager{lifetime: context.Background(), logWriter: io.Discard, now: time.Now, readiness: workerReadinessTimeout})
	if err != nil {
		t.Fatal(err)
	}
	demand := encodeDemand(t, registeredDemand())
	for name, frame := range map[string]wireproto.Frame{
		"id":       {Op: wireproto.OpSnapshotWarm, Snapshot: demand, ID: 1},
		"parent":   {Op: wireproto.OpSnapshotWarm, Snapshot: demand, ParentID: 1},
		"build":    {Op: wireproto.OpSnapshotWarm, Snapshot: demand, Build: Build},
		"request":  {Op: wireproto.OpSnapshotWarm, Snapshot: demand, Request: &wireproto.EventRequest{}},
		"response": {Op: wireproto.OpSnapshotWarm, Snapshot: demand, Response: &wireproto.EventResponse{}},
		"adopt":    {Op: wireproto.OpSnapshotWarm, Snapshot: demand, Adopt: &wireproto.AdoptRequest{}},
		"error":    {Op: wireproto.OpSnapshotWarm, Snapshot: demand, Error: "fixture"},
		"context":  {Op: wireproto.OpSnapshotWarm, Snapshot: demand, SnapshotContext: json.RawMessage(`{}`)},
		"config":   {Op: wireproto.OpSnapshotWarm, Snapshot: demand, SnapshotConfig: json.RawMessage(`{}`)},
	} {
		t.Run(name, func(t *testing.T) {
			if err := (&workerClient{snapshots: service}).snapshotWarmFrame(frame); err == nil || err.Error() != "captain: invalid snapshot warm frame" {
				t.Fatalf("frame=%+v err=%v, want the frame refused", frame, err)
			}
		})
	}
	if err := (&workerClient{}).snapshotWarmFrame(wireproto.Frame{Op: wireproto.OpSnapshotWarm, Snapshot: demand}); err == nil || err.Error() != "captain: snapshot service unavailable" {
		t.Fatalf("demand without a snapshot service=%v", err)
	}
	service.warm.mu.Lock()
	jobs := len(service.warm.jobs)
	service.warm.mu.Unlock()
	if jobs != 0 {
		t.Fatalf("jobs=%d, want a refused frame to start no job", jobs)
	}
}

func TestWarmJobOutlivesTheWorkerThatDemandedIt(t *testing.T) {
	frames := make(chan wireproto.Frame, 8)
	owner, ownerServer := fakeSnapshotOwner(t, frames)
	service, err := newSnapshotService(&workerManager{lifetime: context.Background(), logWriter: io.Discard, now: time.Now, readiness: workerReadinessTimeout})
	if err != nil {
		t.Fatal(err)
	}
	service.owner = owner
	t.Cleanup(service.warm.close)
	client, server := net.Pipe()
	defer server.Close()
	go func() {
		hello, _ := wireproto.DecodeFrame(server)
		_ = wireproto.EncodeFrame(server, wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpHello, Build: hello.Build})
	}()
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	worker, err := handshakeWorker(ctx, client, Build)
	if err != nil {
		t.Fatal(err)
	}
	worker.setSnapshots(service)
	if err := wireproto.EncodeFrame(server, wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpSnapshotWarm, Snapshot: encodeDemand(t, registeredDemand())}); err != nil {
		t.Fatal(err)
	}
	first := receiveSnapshot(t, frames)
	worker.fail(net.ErrClosed)
	select {
	case frame := <-frames:
		t.Fatalf("the demanding worker's exit reached the owner: %+v", frame)
	case <-time.After(50 * time.Millisecond):
	}
	if got := answerWarmFrame(t, ownerServer, first, warmResult{status: "ok", data: registryProgress(8, false, "epoch-1", "rev-1", 0)}).cursor(); got != "0 null" {
		t.Fatalf("first cursor=%s, want 0 null", got)
	}
	second := answerWarmFrame(t, ownerServer, receiveSnapshot(t, frames), warmResult{status: "ok", data: registryProgress(12, true, "epoch-1", "rev-1", 0)})
	if got := second.cursor(); got != `8 "rev-1"` || !strings.HasPrefix(second.callContext.Claimant, "warm:") {
		t.Fatalf("cursor after the worker left=%s context=%+v, want the shared job advanced to 8 \"rev-1\"", got, second.callContext)
	}
	awaitWarmIdle(t, service.warm)
	service.warm.mu.Lock()
	jobs := len(service.warm.jobs)
	service.warm.mu.Unlock()
	if jobs != 0 || len(service.warmSlots) != 0 || len(service.warmQueue) != 0 {
		t.Fatalf("jobs=%d warm slots=%d warm queue=%d, want the job complete and admission released", jobs, len(service.warmSlots), len(service.warmQueue))
	}
	if !worker.broken() {
		t.Fatal("the demanding worker survived its own failure")
	}
}
