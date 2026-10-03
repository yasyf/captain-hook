package hookd

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"maps"
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

	mu            sync.Mutex
	sessions      map[string]*monitoredSession
	lastCensus    *procCensus
	pendingCensus *procCensus
	ticks         int
}

type procCensus struct {
	at    time.Time
	done  chan struct{}
	table map[int]procRow
	err   error
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
	walk    *descendantWalk
}

type descendantWalk struct {
	pending []procRow
	visited []procRow
	argvs   map[procIdentity]cachedArgv
	ticks   int
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
	table, err := m.census(m.settings.SampleInterval)
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

func (m *resourceMonitor) census(maxAge time.Duration) (map[int]procRow, error) {
	census, leads := m.joinCensus(maxAge)
	if leads {
		census.table, census.err = m.source.snapshot()
		m.settleCensus(census)
	}
	<-census.done
	return census.table, census.err
}

func (m *resourceMonitor) joinCensus(maxAge time.Duration) (*procCensus, bool) {
	now := m.now()
	m.mu.Lock()
	defer m.mu.Unlock()
	switch {
	case m.lastCensus != nil && now.Sub(m.lastCensus.at) < maxAge:
		return m.lastCensus, false
	case m.pendingCensus != nil:
		return m.pendingCensus, false
	}
	m.pendingCensus = &procCensus{at: now, done: make(chan struct{})}
	return m.pendingCensus, true
}

func (m *resourceMonitor) settleCensus(census *procCensus) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.pendingCensus = nil
	if census.err == nil {
		m.lastCensus = census
	}
	close(census.done)
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
	walk         *descendantWalk
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
	fits        bool
	argvs       map[procIdentity]cachedArgv
	tracked     map[int]*trackedChild
	transitions []childTransition
	decision    *stageDecision
}

func (m *resourceMonitor) tick(now time.Time) {
	table, err := m.census(0)
	if err != nil {
		fmt.Fprintf(m.logWriter, "captain: resource snapshot: %v\n", err)
		return
	}
	children := childIndex(table)
	snapshots := m.snapshotSessions()
	budget := resourceTickBudget
	for i, snapshot := range snapshots {
		budget -= m.tickSession(snapshot, table, children, budget/(len(snapshots)-i), now)
	}
}

func (m *resourceMonitor) snapshotSessions() []sessionSnapshot {
	m.mu.Lock()
	defer m.mu.Unlock()
	ids := slices.Sorted(maps.Keys(m.sessions))
	if len(ids) != 0 {
		ids = slices.Concat(ids[m.ticks%len(ids):], ids[:m.ticks%len(ids)])
	}
	m.ticks++
	snapshots := make([]sessionSnapshot, 0, len(ids))
	for _, id := range ids {
		session := m.sessions[id]
		tracked := make(map[int]*trackedChild, len(session.tracked))
		for pid, child := range session.tracked {
			tracked[pid] = child.clone()
		}
		snapshots = append(snapshots, sessionSnapshot{
			session: session, id: session.id, anchor: session.anchor, registeredAt: session.registeredAt,
			root: session.request.Root, settings: session.settings, baseline: session.baseline,
			argvs: session.argvs, inflight: session.inflight, tracked: tracked, walk: session.walk,
		})
	}
	return snapshots
}

func (m *resourceMonitor) tickSession(
	snapshot sessionSnapshot, table map[int]procRow, children map[int][]int, budget int, now time.Time,
) int {
	if reason := sessionStale(snapshot, table); reason != "" {
		m.mu.Lock()
		defer m.mu.Unlock()
		if m.sessions[snapshot.id] == snapshot.session {
			m.unregisterLocked(snapshot.id, reason)
		}
		return 0
	}
	walk, spent := m.walk(snapshot, table, children, budget)
	if len(walk.pending) != 0 {
		m.deferSample(snapshot, walk, budget)
		return spent
	}
	sample := tickSample{
		fits: walk.ticks == 1, argvs: walk.argvs,
		tracked: m.selectTracked(snapshot, walk.live(table, snapshot.anchor.PID), walk.argvs),
	}
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
	return spent
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

func (m *resourceMonitor) deferSample(snapshot sessionSnapshot, walk *descendantWalk, budget int) {
	m.mu.Lock()
	defer m.mu.Unlock()
	session := snapshot.session
	if m.sessions[snapshot.id] != session {
		return
	}
	session.walk = walk
	if session.exhausted {
		return
	}
	session.exhausted = true
	fmt.Fprintf(m.logWriter, "captain: resource session %s: descendant walk exceeded its %d-row tick share; sampling waits for each walk to finish across ticks\n", session.id, budget)
}

func (m *resourceMonitor) walk(
	snapshot sessionSnapshot, table map[int]procRow, children map[int][]int, budget int,
) (*descendantWalk, int) {
	walk := snapshot.walk
	if walk == nil {
		walk = &descendantWalk{pending: childRows(table, children, snapshot.anchor.PID), argvs: make(map[procIdentity]cachedArgv)}
	}
	walk.ticks++
	spent := 0
	for ; len(walk.pending) != 0 && spent < budget; spent++ {
		walked := walk.pending[0]
		walk.pending = walk.pending[1:]
		row, ok := table[walked.PID]
		if !ok || !sameLink(row, walked) {
			continue
		}
		if len(children[row.PID]) != 0 {
			if m.resolveArgv(snapshot.argvs, walk.argvs, row).agent() {
				continue
			}
			walk.pending = append(walk.pending, childRows(table, children, row.PID)...)
		} else if _, cached := snapshot.argvs[row.identity()]; cached || snapshot.candidate(row) {
			if m.resolveArgv(snapshot.argvs, walk.argvs, row).agent() {
				continue
			}
		}
		walk.visited = append(walk.visited, row)
	}
	return walk, spent
}

func (w *descendantWalk) live(table map[int]procRow, anchor int) map[int]procRow {
	live := make(map[int]procRow, len(w.visited))
	for _, walked := range w.visited {
		_, parentLive := live[walked.PPID]
		if row, ok := table[walked.PID]; ok && sameLink(row, walked) && (walked.PPID == anchor || parentLive) {
			live[row.PID] = row
		}
	}
	return live
}

func sameLink(row, walked procRow) bool {
	return row.identity() == walked.identity() && row.PPID == walked.PPID
}

func childRows(table map[int]procRow, children map[int][]int, parent int) []procRow {
	rows := make([]procRow, 0, len(children[parent]))
	for _, pid := range children[parent] {
		rows = append(rows, table[pid])
	}
	return rows
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
		if !snapshot.candidate(row) || m.resolveArgv(snapshot.argvs, argvs, row).agent() {
			continue
		}
		tracked[pid] = &trackedChild{row: row}
	}
	return tracked
}

func (s sessionSnapshot) candidate(row procRow) bool {
	_, baseline := s.baseline[row.identity()]
	return !baseline && !startedAt(row).Before(s.registeredAt)
}

func startedAt(row procRow) time.Time {
	return time.Unix(row.StartUnix, int64(row.StartUsec)*1000)
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
	runtime := now.Sub(startedAt(child.row))
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
	session.walk = nil
	session.exhausted = session.exhausted && !sample.fits
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
		RuntimeS: math.Round(now.Sub(startedAt(row)).Seconds()*1000) / 1000,
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
