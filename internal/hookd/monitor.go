package hookd

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"os"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/yasyf/captain-hook/internal/wireproto"
)

const (
	resourceEvent      = "ResourcePressure"
	resourceProceedAck = `{"decision":"proceed"}`

	resourceWarnDeadline     = 10 * time.Second
	resourceJudgeDeadline    = 35 * time.Second
	resourceEscalateDeadline = 10 * time.Second

	resourceAnchorHops        = 20
	resourceAncestryHops      = 64
	resourceSessionsPerAnchor = 8
	resourceTickBudget        = 4096
)

type childState int

const (
	childObserving childState = iota
	childWarnPending
	childGrace
	childJudgePending
	childJudged
	childEscalatePending
	childDone
	childExcluded
)

type resourceStage string

const (
	stageWarn     resourceStage = "warn"
	stageJudge    resourceStage = "judge"
	stageEscalate resourceStage = "escalate"
)

type resourceMonitor struct {
	manager   *workerManager
	source    procSource
	settings  wireproto.ResourceSettings
	now       func() time.Time
	dispatch  func(context.Context, wireproto.EventRequest) (wireproto.EventResponse, error)
	logWriter io.Writer

	mu       sync.Mutex
	sessions map[string]*monitoredSession
}

type monitoredSession struct {
	id             string
	anchor         procIdentity
	registeredAt   time.Time
	lastSeen       time.Time
	request        wireproto.EventRequest
	transcriptPath string
	settings       wireproto.ResourceSettings
	baseline       map[procIdentity]struct{}

	lifetime context.Context
	end      context.CancelFunc
	inflight bool

	foreignLogged bool
	exhausted     bool

	argvs   map[procIdentity]cachedArgv
	tracked map[int]*trackedChild
}

type cachedArgv struct {
	argv  []string
	known bool
}

type trackedChild struct {
	row      procRow
	cpu      metricTrack
	disk     metricTrack
	state    childState
	warned   bool
	gracedAt time.Time
	judgedAt time.Time
}

func (c *trackedChild) clone() *trackedChild {
	copied := *c
	copied.cpu.rates = slices.Clone(c.cpu.rates)
	copied.disk.rates = slices.Clone(c.disk.rates)
	return &copied
}

type metricTrack struct {
	last   float64
	lastAt time.Time
	known  bool
	rates  []float64
}

func (t *metricTrack) observe(value float64, now time.Time, window int) {
	if t.known {
		t.rates = append(t.rates, (value-t.last)/now.Sub(t.lastAt).Seconds())
		if len(t.rates) > window {
			t.rates = t.rates[len(t.rates)-window:]
		}
	}
	t.last, t.lastAt, t.known = value, now, true
}

func (t *metricTrack) reset() {
	*t = metricTrack{}
}

func (t *metricTrack) sustained(threshold float64, window int) bool {
	if len(t.rates) < window {
		return false
	}
	for _, rate := range t.rates {
		if rate < threshold {
			return false
		}
	}
	return true
}

func (t *metricTrack) latest() (float64, bool) {
	if len(t.rates) == 0 {
		return 0, false
	}
	return t.rates[len(t.rates)-1], true
}

func newResourceMonitor(manager *workerManager, source procSource, settings wireproto.ResourceSettings) *resourceMonitor {
	return &resourceMonitor{
		manager: manager, source: source, settings: settings,
		now: manager.now, dispatch: manager.dispatch, logWriter: manager.logWriter,
		sessions: make(map[string]*monitoredSession),
	}
}

func (m *resourceMonitor) start() {
	m.manager.wg.Add(1)
	go func() {
		defer m.manager.wg.Done()
		ticker := time.NewTicker(m.settings.SampleInterval)
		defer ticker.Stop()
		for {
			select {
			case <-m.manager.lifetime.Done():
				return
			case <-ticker.C:
				m.tick(m.now())
			}
		}
	}()
}

type sessionFields struct {
	SessionID      string `json:"session_id"`
	TranscriptPath string `json:"transcript_path"`
}

func (m *resourceMonitor) observe(request wireproto.EventRequest, peer int) {
	var fields sessionFields
	if json.Unmarshal([]byte(request.PayloadRaw), &fields) != nil || fields.SessionID == "" {
		return
	}
	m.mu.Lock()
	session, known := m.sessions[fields.SessionID]
	full := len(m.sessions) >= m.settings.RegistryCap
	m.mu.Unlock()
	if known {
		m.observeKnown(request, peer, session)
		return
	}
	if request.Event == "SessionEnd" || full {
		return
	}
	settings, err := wireproto.ParseResourceSettings(request.Env)
	if err != nil {
		fmt.Fprintf(m.logWriter, "captain: resource session %s: %v\n", fields.SessionID, err)
		return
	}
	if !settings.Enabled {
		return
	}
	anchor, ok := m.resolveAnchor(peer, request.ClientPPID)
	if !ok {
		return
	}
	table, err := m.source.snapshot()
	if err != nil {
		fmt.Fprintf(m.logWriter, "captain: resource session %s: %v\n", fields.SessionID, err)
		return
	}
	baseline := make(map[procIdentity]struct{})
	for _, pid := range descendants(table, childIndex(table), anchor.PID) {
		baseline[table[pid].identity()] = struct{}{}
	}
	now := m.now()
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, known := m.sessions[fields.SessionID]; known || len(m.sessions) >= m.settings.RegistryCap ||
		m.anchoredLocked(anchor.identity()) >= resourceSessionsPerAnchor {
		return
	}
	lifetime, end := context.WithCancel(m.manager.lifetime)
	m.sessions[fields.SessionID] = &monitoredSession{
		id: fields.SessionID, anchor: anchor.identity(), registeredAt: now, lastSeen: now,
		request:        wireproto.EventRequest{Root: request.Root, CWD: request.CWD, Env: request.Env},
		transcriptPath: fields.TranscriptPath, settings: settings, baseline: baseline,
		lifetime: lifetime, end: end,
		argvs: make(map[procIdentity]cachedArgv), tracked: make(map[int]*trackedChild),
	}
	fmt.Fprintf(m.logWriter, "captain: resource session %s registered under claude pid %d\n", fields.SessionID, anchor.PID)
}

func (m *resourceMonitor) observeKnown(request wireproto.EventRequest, peer int, session *monitoredSession) {
	anchor, ok := m.resolveAnchor(peer, request.ClientPPID)
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.sessions[session.id] != session {
		return
	}
	if !ok || anchor.identity() != session.anchor {
		if !session.foreignLogged {
			session.foreignLogged = true
			fmt.Fprintf(m.logWriter, "captain: resource session %s: ignored %s from peer %d outside its claude anchor\n", session.id, request.Event, peer)
		}
		return
	}
	if request.Event == "SessionEnd" {
		m.unregisterLocked(session.id, "session ended")
		return
	}
	session.lastSeen = m.now()
}

func (m *resourceMonitor) anchoredLocked(anchor procIdentity) int {
	count := 0
	for _, session := range m.sessions {
		if session.anchor == anchor {
			count++
		}
	}
	return count
}

func (m *resourceMonitor) resolveAnchor(peer, claimedParent int) (procRow, bool) {
	pid := peer
	onWalk := false
	for range resourceAnchorHops {
		if pid <= 1 {
			return procRow{}, false
		}
		row, ok := m.source.probe(pid)
		if !ok {
			return procRow{}, false
		}
		if argv, ok := m.source.argv(pid); ok && isClaudeArgv(argv) {
			if !onWalk {
				return procRow{}, false
			}
			return row, true
		}
		pid = row.PPID
		onWalk = onWalk || pid == claimedParent
	}
	return procRow{}, false
}

func (m *resourceMonitor) unregisterLocked(id, reason string) {
	session, known := m.sessions[id]
	if !known {
		return
	}
	session.end()
	delete(m.sessions, id)
	fmt.Fprintf(m.logWriter, "captain: resource session %s unregistered: %s\n", id, reason)
}

type sessionSnapshot struct {
	session      *monitoredSession
	id           string
	anchor       procIdentity
	registeredAt time.Time
	root         string
	settings     wireproto.ResourceSettings
	baseline     map[procIdentity]struct{}
	argvs        map[procIdentity]cachedArgv
	inflight     bool
	tracked      map[int]*trackedChild
}

type stageDecision struct {
	pid      int
	stage    resourceStage
	deadline time.Duration
	request  wireproto.EventRequest
	err      error
}

type childTransition struct {
	pid  int
	text string
}

type tickSample struct {
	argvs       map[procIdentity]cachedArgv
	tracked     map[int]*trackedChild
	transitions []childTransition
	decision    *stageDecision
}

func (m *resourceMonitor) tick(now time.Time) {
	table, err := m.source.snapshot()
	if err != nil {
		fmt.Fprintf(m.logWriter, "captain: resource snapshot: %v\n", err)
		return
	}
	children := childIndex(table)
	for _, snapshot := range m.snapshotSessions() {
		m.tickSession(snapshot, table, children, now)
	}
}

func (m *resourceMonitor) snapshotSessions() []sessionSnapshot {
	m.mu.Lock()
	defer m.mu.Unlock()
	snapshots := make([]sessionSnapshot, 0, len(m.sessions))
	for _, session := range m.sessions {
		tracked := make(map[int]*trackedChild, len(session.tracked))
		for pid, child := range session.tracked {
			tracked[pid] = child.clone()
		}
		snapshots = append(snapshots, sessionSnapshot{
			session: session, id: session.id, anchor: session.anchor, registeredAt: session.registeredAt,
			root: session.request.Root, settings: session.settings, baseline: session.baseline,
			argvs: session.argvs, inflight: session.inflight, tracked: tracked,
		})
	}
	return snapshots
}

func (m *resourceMonitor) tickSession(snapshot sessionSnapshot, table map[int]procRow, children map[int][]int, now time.Time) {
	if reason := sessionStale(snapshot, table); reason != "" {
		m.mu.Lock()
		defer m.mu.Unlock()
		if m.sessions[snapshot.id] == snapshot.session {
			m.unregisterLocked(snapshot.id, reason)
		}
		return
	}
	live, argvs, ok := m.walk(snapshot, table, children)
	if !ok {
		m.markExhausted(snapshot)
		return
	}
	sample := tickSample{argvs: argvs, tracked: m.selectTracked(snapshot, live, argvs)}
	window := max(1, int(snapshot.settings.Sustain/m.settings.SampleInterval))
	for pid, child := range sample.tracked {
		usage, _ := m.source.usage(pid)
		if usage.CPUKnown {
			child.cpu.observe(usage.CPUSeconds, now, window)
		} else {
			child.cpu.reset()
		}
		if usage.DiskKnown {
			child.disk.observe(float64(usage.DiskBytes), now, window)
		} else {
			child.disk.reset()
		}
	}
	if !snapshot.inflight {
		for _, pid := range sortedTracked(sample.tracked) {
			child := sample.tracked[pid]
			decision, transition := advance(child, snapshot.settings, window, now)
			if transition != "" {
				sample.transitions = append(sample.transitions, childTransition{pid: pid, text: transition})
			}
			if decision != nil {
				decision.request, decision.err = m.stageRequest(snapshot, child, decision.stage, table, now)
				sample.decision = decision
				break
			}
		}
	}
	m.applyTick(snapshot, sample)
}

func sessionStale(snapshot sessionSnapshot, table map[int]procRow) string {
	anchor, alive := table[snapshot.anchor.PID]
	switch {
	case !alive:
		return "claude process exited"
	case anchor.identity() != snapshot.anchor:
		return "claude pid reused"
	case !rootExists(snapshot.root):
		return "root removed"
	}
	return ""
}

func (m *resourceMonitor) markExhausted(snapshot sessionSnapshot) {
	m.mu.Lock()
	defer m.mu.Unlock()
	session := snapshot.session
	if m.sessions[snapshot.id] != session || session.exhausted {
		return
	}
	session.exhausted = true
	fmt.Fprintf(m.logWriter, "captain: resource session %s: descendant walk exceeded %d rows; sampling suspended until it fits\n", session.id, resourceTickBudget)
}

func (m *resourceMonitor) walk(snapshot sessionSnapshot, table map[int]procRow, children map[int][]int) (map[int]procRow, map[procIdentity]cachedArgv, bool) {
	live := make(map[int]procRow)
	argvs := make(map[procIdentity]cachedArgv)
	budget := resourceTickBudget
	queue := []int{snapshot.anchor.PID}
	for len(queue) != 0 {
		parent := queue[0]
		queue = queue[1:]
		for _, pid := range children[parent] {
			if budget == 0 {
				return nil, nil, false
			}
			budget--
			row := table[pid]
			if len(children[pid]) != 0 {
				if m.resolveArgv(snapshot.argvs, argvs, row).agent() {
					continue
				}
				queue = append(queue, pid)
			} else if cached, ok := snapshot.argvs[row.identity()]; ok {
				argvs[row.identity()] = cached
				if cached.agent() {
					continue
				}
			}
			live[pid] = row
		}
	}
	return live, argvs, true
}

func (c cachedArgv) agent() bool {
	return c.known && isAgentArgv(c.argv)
}

func (m *resourceMonitor) resolveArgv(cache, into map[procIdentity]cachedArgv, row procRow) cachedArgv {
	identity := row.identity()
	if cached, ok := into[identity]; ok {
		return cached
	}
	cached, ok := cache[identity]
	if !ok {
		cached.argv, cached.known = m.source.argv(row.PID)
	}
	into[identity] = cached
	return cached
}

func (m *resourceMonitor) selectTracked(snapshot sessionSnapshot, live map[int]procRow, argvs map[procIdentity]cachedArgv) map[int]*trackedChild {
	tracked := make(map[int]*trackedChild, len(snapshot.tracked))
	for pid, child := range snapshot.tracked {
		if row, ok := live[pid]; ok && row.identity() == child.row.identity() {
			current := child.clone()
			current.row = row
			tracked[pid] = current
		}
	}
	for _, pid := range sortedPIDs(live) {
		if _, ok := tracked[pid]; ok {
			continue
		}
		if len(tracked) >= snapshot.settings.MaxTrackedPerSession {
			break
		}
		row := live[pid]
		if _, baseline := snapshot.baseline[row.identity()]; baseline || row.StartUnix < snapshot.registeredAt.Unix() {
			continue
		}
		if m.resolveArgv(snapshot.argvs, argvs, row).agent() {
			continue
		}
		tracked[pid] = &trackedChild{row: row}
	}
	return tracked
}

func descendants(table map[int]procRow, children map[int][]int, root int) []int {
	var found []int
	queue := []int{root}
	for len(queue) != 0 {
		parent := queue[0]
		queue = queue[1:]
		for _, pid := range children[parent] {
			if _, ok := table[pid]; ok {
				found = append(found, pid)
				queue = append(queue, pid)
			}
		}
	}
	return found
}

func sortedPIDs(rows map[int]procRow) []int {
	pids := make([]int, 0, len(rows))
	for pid := range rows {
		pids = append(pids, pid)
	}
	slices.Sort(pids)
	return pids
}

func sortedTracked(tracked map[int]*trackedChild) []int {
	pids := make([]int, 0, len(tracked))
	for pid := range tracked {
		pids = append(pids, pid)
	}
	slices.Sort(pids)
	return pids
}

func advance(child *trackedChild, settings wireproto.ResourceSettings, window int, now time.Time) (*stageDecision, string) {
	above := child.cpu.sustained(settings.CPUFraction, window) ||
		child.disk.sustained(float64(settings.DiskBytesPerSecond), window)
	runtime := now.Sub(time.Unix(child.row.StartUnix, int64(child.row.StartUsec)*1000))
	switch child.state {
	case childObserving:
		if !above || runtime < settings.MinRuntime {
			return nil, ""
		}
		if child.warned {
			child.state = childGrace
			return nil, ""
		}
		child.state = childWarnPending
		return &stageDecision{pid: child.row.PID, stage: stageWarn, deadline: resourceWarnDeadline}, ""
	case childGrace:
		if !above {
			child.state = childObserving
			return nil, "calmed; observing"
		}
		if now.Sub(child.gracedAt) < settings.Grace {
			return nil, ""
		}
		child.state = childJudgePending
		return &stageDecision{pid: child.row.PID, stage: stageJudge, deadline: resourceJudgeDeadline}, ""
	case childJudged:
		if now.Sub(child.judgedAt) < settings.EscalateAfter {
			return nil, ""
		}
		if !above {
			child.state = childObserving
			return nil, "no sustained pressure at escalation; observing"
		}
		child.state = childEscalatePending
		return &stageDecision{pid: child.row.PID, stage: stageEscalate, deadline: resourceEscalateDeadline}, ""
	}
	return nil, ""
}

func (m *resourceMonitor) stageRequest(
	snapshot sessionSnapshot, child *trackedChild, stage resourceStage, table map[int]procRow, now time.Time,
) (wireproto.EventRequest, error) {
	payload, err := json.Marshal(m.payload(snapshot, child, stage, table, now))
	if err != nil {
		return wireproto.EventRequest{}, fmt.Errorf("captain: encode resource payload: %w", err)
	}
	request := snapshot.session.request
	return wireproto.EventRequest{
		Schema: wireproto.Schema, Event: resourceEvent,
		Root: request.Root, CWD: request.CWD, Env: request.Env,
		PayloadRaw: string(payload), ClientPID: os.Getpid(), ClientPPID: snapshot.anchor.PID,
	}, nil
}

func (m *resourceMonitor) applyTick(snapshot sessionSnapshot, sample tickSample) {
	m.mu.Lock()
	defer m.mu.Unlock()
	session := snapshot.session
	if m.sessions[snapshot.id] != session {
		return
	}
	session.exhausted = false
	session.argvs = sample.argvs
	consistent := session.inflight == snapshot.inflight
	for pid, child := range snapshot.tracked {
		current, ok := session.tracked[pid]
		consistent = consistent && ok && current.row.identity() == child.row.identity() && current.state == child.state
	}
	for pid := range session.tracked {
		if _, ok := sample.tracked[pid]; !ok {
			delete(session.tracked, pid)
		}
	}
	for pid, computed := range sample.tracked {
		current, ok := session.tracked[pid]
		if !ok || current.row.identity() != computed.row.identity() {
			session.tracked[pid] = computed
			continue
		}
		current.row, current.cpu, current.disk = computed.row, computed.cpu, computed.disk
		if consistent {
			current.state = computed.state
		}
	}
	if !consistent {
		return
	}
	for _, transition := range sample.transitions {
		m.log(session, session.tracked[transition.pid], transition.text)
	}
	if decision := sample.decision; decision != nil {
		m.startStage(session, session.tracked[decision.pid], *decision)
	}
}

func (m *resourceMonitor) startStage(session *monitoredSession, child *trackedChild, decision stageDecision) {
	if decision.err != nil {
		m.applyLocked(session, child, decision.stage, wireproto.EventResponse{}, decision.err)
		return
	}
	m.manager.mu.Lock()
	closed := m.manager.closed
	if !closed {
		m.manager.wg.Add(1)
	}
	m.manager.mu.Unlock()
	if closed {
		m.applyLocked(session, child, decision.stage, wireproto.EventResponse{}, errWorkerManagerClosed)
		return
	}
	session.inflight = true
	go m.runStage(session, child, decision)
}

func (m *resourceMonitor) runStage(session *monitoredSession, child *trackedChild, decision stageDecision) {
	defer m.manager.wg.Done()
	ctx, cancel := context.WithTimeout(withHostDispatch(session.lifetime), decision.deadline)
	defer cancel()
	response, err := m.dispatch(ctx, decision.request)
	m.mu.Lock()
	defer m.mu.Unlock()
	session.inflight = false
	m.applyLocked(session, child, decision.stage, response, err)
}

func (m *resourceMonitor) applyLocked(session *monitoredSession, child *trackedChild, stage resourceStage, response wireproto.EventResponse, err error) {
	if err != nil {
		fmt.Fprintf(m.logWriter, "captain: resource session %s pid %d %s dispatch: %v\n", session.id, child.row.PID, stage, err)
	}
	switch stage {
	case stageWarn:
		if err != nil || !isProceedAck(response) {
			child.state = childExcluded
			m.log(session, child, "excluded without a proceed ack")
			return
		}
		child.warned, child.state, child.gracedAt = true, childGrace, m.now()
		m.log(session, child, "warned; grace started")
	case stageJudge:
		child.judgedAt = m.now()
		child.state = childJudged
		if session.settings.EscalateAfter <= 0 {
			child.state = childDone
		}
		m.log(session, child, "judged")
	case stageEscalate:
		child.state = childDone
		m.log(session, child, "escalated")
	}
}

func isProceedAck(response wireproto.EventResponse) bool {
	if response.Status != "ok" || response.Exit != 0 {
		return false
	}
	var envelope map[string]any
	if json.Unmarshal([]byte(strings.TrimSpace(response.Stdout)), &envelope) != nil {
		return false
	}
	return len(envelope) == 1 && envelope["decision"] == "proceed"
}

func (m *resourceMonitor) log(session *monitoredSession, child *trackedChild, transition string) {
	fmt.Fprintf(m.logWriter, "captain: resource session %s pid %d (%s): %s\n", session.id, child.row.PID, child.row.Comm, transition)
}

type resourceAncestor struct {
	PID  int    `json:"pid"`
	Comm string `json:"comm"`
}

type resourceProcess struct {
	PID         int                `json:"pid"`
	PPID        int                `json:"ppid"`
	PGID        int                `json:"pgid"`
	StartUnix   int64              `json:"start_unix"`
	StartUsec   int32              `json:"start_usec"`
	Comm        string             `json:"comm"`
	Argv        []string           `json:"argv"`
	CWD         string             `json:"cwd,omitempty"`
	RuntimeS    float64            `json:"runtime_s"`
	CPUFraction *float64           `json:"cpu_fraction,omitempty"`
	DiskBPS     *float64           `json:"disk_bps,omitempty"`
	Ancestry    []resourceAncestor `json:"ancestry"`
}

type resourcePayload struct {
	HookEventName   string          `json:"hook_event_name"`
	SessionID       string          `json:"session_id"`
	TranscriptPath  string          `json:"transcript_path"`
	CWD             string          `json:"cwd"`
	Stage           resourceStage   `json:"stage"`
	ClaudePID       int             `json:"claude_pid"`
	ClaudeStartUnix int64           `json:"claude_start_unix"`
	Process         resourceProcess `json:"process"`
	Metrics         resourceMetrics `json:"metrics"`
}

type resourceMetrics struct {
	CPU  bool `json:"cpu"`
	Disk bool `json:"disk"`
}

func (m *resourceMonitor) payload(
	snapshot sessionSnapshot, child *trackedChild, stage resourceStage, table map[int]procRow, now time.Time,
) resourcePayload {
	row := child.row
	argv, known := m.source.argv(row.PID)
	if !known {
		argv = []string{}
	}
	process := resourceProcess{
		PID: row.PID, PPID: row.PPID, PGID: row.PGID, StartUnix: row.StartUnix, StartUsec: row.StartUsec, Comm: row.Comm,
		Argv:     argv,
		RuntimeS: math.Round(now.Sub(time.Unix(row.StartUnix, int64(row.StartUsec)*1000)).Seconds()*1000) / 1000,
		Ancestry: ancestry(table, row.PPID, snapshot.anchor.PID),
	}
	if cwd, ok := m.source.cwd(row.PID); ok {
		process.CWD = cwd
	}
	var metrics resourceMetrics
	if rate, ok := child.cpu.latest(); ok {
		process.CPUFraction, metrics.CPU = &rate, true
	}
	if rate, ok := child.disk.latest(); ok {
		process.DiskBPS, metrics.Disk = &rate, true
	}
	session := snapshot.session
	return resourcePayload{
		HookEventName: resourceEvent, SessionID: snapshot.id, TranscriptPath: session.transcriptPath, CWD: session.request.CWD,
		Stage: stage, ClaudePID: snapshot.anchor.PID, ClaudeStartUnix: snapshot.anchor.StartUnix,
		Process: process, Metrics: metrics,
	}
}

func ancestry(table map[int]procRow, pid, anchor int) []resourceAncestor {
	chain := make([]resourceAncestor, 0)
	for range resourceAncestryHops {
		row, ok := table[pid]
		if !ok {
			return chain
		}
		chain = append(chain, resourceAncestor{PID: row.PID, Comm: row.Comm})
		if pid == anchor || row.PPID <= 1 {
			return chain
		}
		pid = row.PPID
	}
	return chain
}
