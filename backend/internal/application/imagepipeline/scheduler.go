package imagepipeline

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sort"
	"strings"
	"sync"
	"time"

	domain "github.com/chenyme/grok2api/backend/internal/domain/imagepipeline"
	"github.com/chenyme/grok2api/backend/internal/infra/security"
	"github.com/chenyme/grok2api/backend/internal/repository"
)

var (
	ErrQueueFull     = errors.New("生图流水线队列已满")
	ErrNotConfigured = errors.New("生图流水线未配置")
)

type Config struct {
	PipelineSlots       int
	QueueCapacity       int
	ExpandConcurrency   int
	SSEMin              int
	SSEInitial          int
	SSEMax              int
	SSEStagger          time.Duration
	DownloadConcurrency int
	Retention           time.Duration
}

func DefaultConfig() Config {
	return Config{
		PipelineSlots: 10, QueueCapacity: 100, ExpandConcurrency: 2,
		SSEMin: 2, SSEInitial: 3, SSEMax: 6, SSEStagger: 400 * time.Millisecond,
		DownloadConcurrency: 8, Retention: 12 * time.Hour,
	}
}

type Scheduler struct {
	repo   repository.ImagePipelineRepository
	logger *slog.Logger
	cfg    Config

	mu              sync.Mutex
	slots           []*Run
	waiters         []*slotWaiter
	expandActive    int
	sseActive       int
	sseTarget       int
	downloadActive  int
	lastSSEStart    time.Time
	expandWaiters   []*stageWaiter
	sseWaiters      []*stageWaiter
	downloadWaiters []*stageWaiter
	sseTimerPending bool
	recentOutcomes  []outcomeSample
	live            map[string]*Run
}

type slotWaiter struct {
	run        *Run
	enqueuedAt time.Time
	grant      chan int
	granted    bool
	lane       int
}

type stageWaiter struct {
	run        *Run
	stage      domain.Stage
	enqueuedAt time.Time
	grant      chan struct{}
	granted    bool
}

type outcomeSample struct {
	ok            bool
	softStop      bool
	rateLimit     bool
	transportFail bool
	totalMS       int64
	expandMS      int64
	sseMS         int64
	downloadMS    int64
	at            time.Time
}

type AdmitInput struct {
	RequestID string
	Model     string
}

type Run struct {
	scheduler       *Scheduler
	trace           domain.Trace
	mu              sync.Mutex
	seq             int
	openSegs        map[domain.Stage]uint64
	stageWait       map[domain.Stage]time.Time
	heldStages      map[domain.Stage]bool
	holdingSlot     bool
	releasedAccount bool
	waitingFor      domain.Stage
}

type Timing struct {
	QueueMS    int64
	ExpandMS   int64
	SSEMS      int64
	DownloadMS int64
}

func NewScheduler(repo repository.ImagePipelineRepository, cfg Config, logger *slog.Logger) *Scheduler {
	cfg = normalizeConfig(cfg)
	if logger == nil {
		logger = slog.Default()
	}
	s := &Scheduler{
		repo: repo, logger: logger, cfg: cfg,
		slots:     make([]*Run, cfg.PipelineSlots),
		sseTarget: cfg.SSEInitial,
		live:      make(map[string]*Run),
	}
	return s
}

func normalizeConfig(cfg Config) Config {
	def := DefaultConfig()
	if cfg.PipelineSlots < 1 {
		cfg.PipelineSlots = def.PipelineSlots
	}
	if cfg.PipelineSlots > 64 {
		cfg.PipelineSlots = 64
	}
	if cfg.QueueCapacity < 1 {
		cfg.QueueCapacity = def.QueueCapacity
	}
	if cfg.ExpandConcurrency < 1 {
		cfg.ExpandConcurrency = def.ExpandConcurrency
	}
	if cfg.SSEMin < 1 {
		cfg.SSEMin = def.SSEMin
	}
	if cfg.SSEInitial < 1 {
		cfg.SSEInitial = def.SSEInitial
	}
	if cfg.SSEMax < 1 {
		cfg.SSEMax = def.SSEMax
	}
	if cfg.SSEInitial < cfg.SSEMin {
		cfg.SSEInitial = cfg.SSEMin
	}
	if cfg.SSEMax < cfg.SSEInitial {
		cfg.SSEMax = cfg.SSEInitial
	}
	if cfg.SSEStagger <= 0 {
		cfg.SSEStagger = def.SSEStagger
	}
	if cfg.DownloadConcurrency < 1 {
		cfg.DownloadConcurrency = def.DownloadConcurrency
	}
	if cfg.Retention <= 0 {
		cfg.Retention = def.Retention
	}
	return cfg
}

func (s *Scheduler) Admit(ctx context.Context, input AdmitInput) (*Run, error) {
	if s == nil {
		return nil, ErrNotConfigured
	}
	traceID, err := newTraceID()
	if err != nil {
		return nil, err
	}
	now := time.Now().UTC()
	run := &Run{
		scheduler: s,
		trace: domain.Trace{
			ID: traceID, RequestID: input.RequestID, Status: domain.StatusQueued,
			Model: input.Model, StartedAt: now, Lane: -1,
		},
		openSegs:   make(map[domain.Stage]uint64),
		stageWait:  make(map[domain.Stage]time.Time),
		heldStages: make(map[domain.Stage]bool),
	}
	s.mu.Lock()
	s.live[traceID] = run
	s.mu.Unlock()

	// 先占槽再落库，避免 lane=-1 在旧库 CHECK 下写失败；槽位到手后立刻 Create。
	queueSegID := run.beginSegment(domain.StageQueue, now)

	lane, err := s.acquireSlot(ctx, run, now)
	ended := time.Now().UTC()
	run.endSegment(queueSegID, domain.StageQueue, ended, outcomeFromErr(err))
	if err != nil {
		run.mu.Lock()
		run.trace.QueueMS = ended.Sub(run.trace.StartedAt).Milliseconds()
		run.mu.Unlock()
		// 未分到槽时不落库（旧库 CHECK 拒 lane=-1）；内存 live 仍可被 timeline 合并。
		run.Finish(domain.StatusCanceled, outcomeFromErr(err), false)
		return nil, err
	}

	run.mu.Lock()
	run.trace.Lane = lane
	run.trace.Status = domain.StatusRunning
	run.trace.QueueMS = ended.Sub(run.trace.StartedAt).Milliseconds()
	run.holdingSlot = true
	run.mu.Unlock()
	if persistErr := s.persistCreate(run.snapshotTrace()); persistErr != nil {
		s.logger.Warn("image_pipeline_trace_create_failed", "error", persistErr)
	}
	return run, nil
}

func (s *Scheduler) acquireSlot(ctx context.Context, run *Run, enqueuedAt time.Time) (int, error) {
	s.mu.Lock()
	if len(s.waiters) == 0 {
		if lane, ok := s.findFreeSlotLocked(); ok {
			s.slots[lane] = run
			s.mu.Unlock()
			return lane, nil
		}
	}
	if len(s.waiters) >= s.cfg.QueueCapacity {
		s.mu.Unlock()
		return -1, ErrQueueFull
	}
	waiter := &slotWaiter{run: run, enqueuedAt: enqueuedAt, grant: make(chan int, 1), lane: -1}
	s.waiters = append(s.waiters, waiter)
	s.mu.Unlock()

	select {
	case lane := <-waiter.grant:
		return lane, nil
	case <-ctx.Done():
		s.mu.Lock()
		if waiter.granted {
			if waiter.lane >= 0 && waiter.lane < len(s.slots) && s.slots[waiter.lane] == run {
				s.slots[waiter.lane] = nil
				s.grantWaitersLocked()
			}
		} else {
			for i, queued := range s.waiters {
				if queued == waiter {
					s.waiters = append(s.waiters[:i], s.waiters[i+1:]...)
					break
				}
			}
		}
		s.mu.Unlock()
		return -1, ctx.Err()
	}
}

func (s *Scheduler) findFreeSlotLocked() (int, bool) {
	for i, owner := range s.slots {
		if owner == nil {
			return i, true
		}
	}
	return -1, false
}

func (s *Scheduler) releaseSlot(lane int, owner *Run) {
	if lane < 0 {
		return
	}
	s.mu.Lock()
	if lane < len(s.slots) && s.slots[lane] == owner {
		s.slots[lane] = nil
		s.grantWaitersLocked()
	}
	s.mu.Unlock()
}

func (s *Scheduler) grantWaitersLocked() {
	for len(s.waiters) > 0 {
		lane, ok := s.findFreeSlotLocked()
		if !ok {
			return
		}
		waiter := s.waiters[0]
		s.waiters = s.waiters[1:]
		waiter.granted = true
		waiter.lane = lane
		s.slots[lane] = waiter.run
		waiter.grant <- lane
	}
}

func (s *Scheduler) acquireStage(ctx context.Context, run *Run, stage domain.Stage, enqueuedAt time.Time) error {
	waiter := &stageWaiter{run: run, stage: stage, enqueuedAt: enqueuedAt, grant: make(chan struct{}, 1)}
	s.mu.Lock()
	queue := s.stageWaitersLocked(stage)
	*queue = append(*queue, waiter)
	s.grantStageWaitersLocked(stage, time.Now().UTC())
	s.mu.Unlock()

	select {
	case <-waiter.grant:
		return nil
	case <-ctx.Done():
		s.mu.Lock()
		if waiter.granted {
			s.decrementStageActiveLocked(stage)
			s.grantStageWaitersLocked(stage, time.Now().UTC())
		} else {
			queue = s.stageWaitersLocked(stage)
			for index, queued := range *queue {
				if queued == waiter {
					*queue = append((*queue)[:index], (*queue)[index+1:]...)
					break
				}
			}
		}
		s.mu.Unlock()
		return ctx.Err()
	}
}

func (s *Scheduler) releaseStage(stage domain.Stage) {
	s.mu.Lock()
	s.decrementStageActiveLocked(stage)
	s.grantStageWaitersLocked(stage, time.Now().UTC())
	s.mu.Unlock()
}

func (s *Scheduler) stageWaitersLocked(stage domain.Stage) *[]*stageWaiter {
	switch stage {
	case domain.StageExpand:
		return &s.expandWaiters
	case domain.StageSSE:
		return &s.sseWaiters
	case domain.StageDownload:
		return &s.downloadWaiters
	default:
		panic("unsupported image pipeline stage: " + string(stage))
	}
}

func (s *Scheduler) stageCapacityLocked(stage domain.Stage) (active, limit int) {
	switch stage {
	case domain.StageExpand:
		return s.expandActive, s.cfg.ExpandConcurrency
	case domain.StageSSE:
		return s.sseActive, s.sseTarget
	case domain.StageDownload:
		return s.downloadActive, s.cfg.DownloadConcurrency
	default:
		return 0, 0
	}
}

func (s *Scheduler) incrementStageActiveLocked(stage domain.Stage, now time.Time) {
	switch stage {
	case domain.StageExpand:
		s.expandActive++
	case domain.StageSSE:
		s.sseActive++
		s.lastSSEStart = now
	case domain.StageDownload:
		s.downloadActive++
	}
}

func (s *Scheduler) decrementStageActiveLocked(stage domain.Stage) {
	switch stage {
	case domain.StageExpand:
		if s.expandActive > 0 {
			s.expandActive--
		}
	case domain.StageSSE:
		if s.sseActive > 0 {
			s.sseActive--
		}
	case domain.StageDownload:
		if s.downloadActive > 0 {
			s.downloadActive--
		}
	}
}

// grantStageWaitersLocked always grants from the head. Queue age therefore
// monotonically increases priority and an older request cannot be bypassed.
func (s *Scheduler) grantStageWaitersLocked(stage domain.Stage, now time.Time) {
	queue := s.stageWaitersLocked(stage)
	for len(*queue) > 0 {
		active, limit := s.stageCapacityLocked(stage)
		if active >= limit {
			return
		}
		if stage == domain.StageSSE && !s.lastSSEStart.IsZero() {
			remaining := s.cfg.SSEStagger - now.Sub(s.lastSSEStart)
			if remaining > 0 {
				s.scheduleSSEGrantLocked(remaining)
				return
			}
		}
		waiter := (*queue)[0]
		*queue = (*queue)[1:]
		waiter.granted = true
		s.incrementStageActiveLocked(stage, now)
		waiter.grant <- struct{}{}
		if stage == domain.StageSSE {
			now = time.Now().UTC()
		}
	}
}

func (s *Scheduler) scheduleSSEGrantLocked(after time.Duration) {
	if s.sseTimerPending {
		return
	}
	s.sseTimerPending = true
	time.AfterFunc(after, func() {
		s.mu.Lock()
		s.sseTimerPending = false
		s.grantStageWaitersLocked(domain.StageSSE, time.Now().UTC())
		s.mu.Unlock()
	})
}

func (r *Run) AcquireExpand(ctx context.Context) error {
	return r.acquirePool(ctx, domain.StageExpand)
}

func (r *Run) ReleaseExpand() {
	r.releasePool(domain.StageExpand)
}

func (r *Run) AcquireSSE(ctx context.Context) error {
	return r.acquirePool(ctx, domain.StageSSE)
}

func (r *Run) ReleaseSSE() {
	r.releasePool(domain.StageSSE)
}

func (r *Run) AcquireDownload(ctx context.Context) error {
	return r.acquirePool(ctx, domain.StageDownload)
}

func (r *Run) ReleaseDownload() {
	r.releasePool(domain.StageDownload)
}

func (r *Run) acquirePool(ctx context.Context, stage domain.Stage) error {
	waitStart := time.Now().UTC()
	r.mu.Lock()
	r.waitingFor = stage
	r.mu.Unlock()
	segID := r.beginSegment(domain.StageQueue, waitStart)
	err := r.scheduler.acquireStage(ctx, r, stage, waitStart)
	ended := time.Now().UTC()
	r.mu.Lock()
	r.waitingFor = ""
	r.trace.QueueMS += ended.Sub(waitStart).Milliseconds()
	r.mu.Unlock()
	if err == nil {
		r.endSegment(segID, domain.StageQueue, ended, "acquired")
		r.mu.Lock()
		r.heldStages[stage] = true
		r.mu.Unlock()
		r.beginSegment(stage, ended)
		return nil
	}
	r.endSegment(segID, domain.StageQueue, ended, "canceled")
	return err
}

func (r *Run) releasePool(stage domain.Stage) {
	now := time.Now().UTC()
	r.mu.Lock()
	segID, ok := r.openSegs[stage]
	started := r.stageWait[stage]
	held := r.heldStages[stage]
	delete(r.openSegs, stage)
	delete(r.stageWait, stage)
	delete(r.heldStages, stage)
	r.mu.Unlock()
	if ok {
		r.endSegment(segID, stage, now, "ok")
		if !started.IsZero() {
			ms := now.Sub(started).Milliseconds()
			r.mu.Lock()
			switch stage {
			case domain.StageExpand:
				r.trace.ExpandMS += ms
			case domain.StageSSE:
				r.trace.SSEMS += ms
			case domain.StageDownload:
				r.trace.DownloadMS += ms
			}
			r.mu.Unlock()
		}
	}
	if held {
		r.scheduler.releaseStage(stage)
	}
}

func (r *Run) SkipExpand() {
	r.mu.Lock()
	r.trace.ExpandMS = 0
	r.mu.Unlock()
}

func (r *Run) SetAccount(accountID uint64, accountName string) {
	r.mu.Lock()
	r.trace.AccountID = &accountID
	r.trace.AccountName = accountName
	r.mu.Unlock()
	if err := r.scheduler.persistUpdate(r.snapshotTrace()); err != nil {
		r.scheduler.logger.Warn("image_pipeline_trace_update_failed", "trace_id", r.TraceID(), "error", err)
	}
}

func (r *Run) MarkAccountReleased() {
	r.mu.Lock()
	r.releasedAccount = true
	r.mu.Unlock()
}

func (r *Run) AccountReleased() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.releasedAccount
}

func (r *Run) MarkSoftStop() {
	r.mu.Lock()
	r.trace.SoftStop = true
	r.mu.Unlock()
}

func (r *Run) Finish(status domain.Status, errorCode string, softStop bool) {
	ended := time.Now().UTC()
	heldStages := r.finishLocked(status, errorCode, ended)
	for _, stage := range heldStages {
		r.scheduler.releaseStage(stage)
	}
	r.mu.Lock()
	softStop = softStop || r.trace.SoftStop
	r.trace.SoftStop = softStop
	sample := outcomeSample{
		ok: status == domain.StatusSucceeded, softStop: softStop,
		rateLimit: errorCode == "usage_limit_reached" || strings.Contains(errorCode, "rate_limit"),
		totalMS:   r.trace.TotalMS, expandMS: r.trace.ExpandMS, sseMS: r.trace.SSEMS, downloadMS: r.trace.DownloadMS,
		at: ended,
	}
	lane := r.trace.Lane
	holding := r.holdingSlot
	r.holdingSlot = false
	id := r.trace.ID
	r.mu.Unlock()
	r.scheduler.recordOutcome(sample)
	if holding {
		r.scheduler.releaseSlot(lane, r)
	}
	r.scheduler.mu.Lock()
	delete(r.scheduler.live, id)
	r.scheduler.mu.Unlock()
	if err := r.scheduler.persistUpdate(r.snapshotTrace()); err != nil {
		r.scheduler.logger.Warn("image_pipeline_trace_update_failed", "trace_id", id, "error", err)
	}
}

func (r *Run) finishLocked(status domain.Status, errorCode string, ended time.Time) []domain.Stage {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.trace.EndedAt != nil {
		return nil
	}
	heldStages := make([]domain.Stage, 0, len(r.heldStages))
	for stage := range r.heldStages {
		heldStages = append(heldStages, stage)
		delete(r.heldStages, stage)
	}
	for stage, segID := range r.openSegs {
		r.scheduler.closeSegmentAsync(segID, ended, "aborted")
		delete(r.openSegs, stage)
		delete(r.stageWait, stage)
	}
	r.trace.Status = status
	r.trace.ErrorCode = errorCode
	r.trace.EndedAt = &ended
	r.trace.TotalMS = ended.Sub(r.trace.StartedAt).Milliseconds()
	return heldStages
}

func (r *Run) snapshotTrace() domain.Trace {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.trace
}

func (r *Run) TraceID() string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.trace.ID
}

func (r *Run) Lane() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.trace.Lane
}

func (r *Run) Timing() Timing {
	r.mu.Lock()
	defer r.mu.Unlock()
	return Timing{QueueMS: r.trace.QueueMS, ExpandMS: r.trace.ExpandMS, SSEMS: r.trace.SSEMS, DownloadMS: r.trace.DownloadMS}
}

func (r *Run) beginSegment(stage domain.Stage, at time.Time) uint64 {
	r.mu.Lock()
	r.seq++
	seq := r.seq
	traceID := r.trace.ID
	r.stageWait[stage] = at
	r.mu.Unlock()
	seg, err := r.scheduler.repo.AppendSegment(context.Background(), domain.Segment{
		TraceID: traceID, Stage: stage, Sequence: seq, StartedAt: at,
	})
	if err != nil {
		r.scheduler.logger.Warn("image_pipeline_segment_append_failed", "stage", stage, "error", err)
		return 0
	}
	r.mu.Lock()
	r.openSegs[stage] = seg.ID
	r.mu.Unlock()
	return seg.ID
}

func (r *Run) endSegment(id uint64, stage domain.Stage, at time.Time, outcome string) {
	if id == 0 {
		return
	}
	r.mu.Lock()
	if open, ok := r.openSegs[stage]; ok && open == id {
		delete(r.openSegs, stage)
	}
	r.mu.Unlock()
	r.scheduler.closeSegmentAsync(id, at, outcome)
}

func (s *Scheduler) closeSegmentAsync(id uint64, at time.Time, outcome string) {
	if id == 0 {
		return
	}
	if err := s.repo.CloseSegment(context.Background(), id, at, outcome); err != nil {
		s.logger.Warn("image_pipeline_segment_close_failed", "id", id, "error", err)
	}
}

func (s *Scheduler) persistCreate(trace domain.Trace) error {
	return s.repo.CreateTrace(context.Background(), trace)
}

func (s *Scheduler) persistUpdate(trace domain.Trace) error {
	return s.repo.UpdateTrace(context.Background(), trace)
}

func (s *Scheduler) recordOutcome(sample outcomeSample) {
	s.mu.Lock()
	defer s.mu.Unlock()
	defer func() { s.grantStageWaitersLocked(domain.StageSSE, time.Now().UTC()) }()
	s.recentOutcomes = append(s.recentOutcomes, sample)
	if len(s.recentOutcomes) > 20 {
		s.recentOutcomes = s.recentOutcomes[len(s.recentOutcomes)-20:]
	}
	if !sample.ok || sample.softStop || sample.rateLimit || sample.transportFail {
		s.sseTarget = max(s.cfg.SSEMin, s.sseTarget*7/10)
		return
	}
	if len(s.recentOutcomes) < 5 {
		return
	}
	success := 0
	var totals []int64
	for _, item := range s.recentOutcomes {
		if item.ok && !item.softStop && !item.rateLimit && !item.transportFail {
			success++
		}
		totals = append(totals, item.totalMS)
	}
	rate := float64(success) / float64(len(s.recentOutcomes))
	if rate >= 0.95 && s.sseTarget < s.cfg.SSEMax {
		sort.Slice(totals, func(i, j int) bool { return totals[i] < totals[j] })
		p95 := totals[min(len(totals)-1, (len(totals)*95)/100)]
		// 简单 AIMD：近期成功且 P95 未明显恶化时 +1
		prevP95 := int64(0)
		if len(s.recentOutcomes) >= 10 {
			older := make([]int64, 0, 10)
			for _, item := range s.recentOutcomes[:len(s.recentOutcomes)-5] {
				older = append(older, item.totalMS)
			}
			sort.Slice(older, func(i, j int) bool { return older[i] < older[j] })
			if len(older) > 0 {
				prevP95 = older[min(len(older)-1, (len(older)*95)/100)]
			}
		}
		if prevP95 == 0 || p95 <= prevP95*110/100 {
			s.sseTarget++
		}
	}
}

func (s *Scheduler) Snapshot() domain.Snapshot {
	s.mu.Lock()
	slotOwners := append([]*Run(nil), s.slots...)
	waiters := append([]*slotWaiter(nil), s.waiters...)
	recentOutcomes := append([]outcomeSample(nil), s.recentOutcomes...)
	snap := domain.Snapshot{
		PipelineSlots: s.cfg.PipelineSlots,
		QueueDepth:    len(waiters), QueueCapacity: s.cfg.QueueCapacity,
		ExpandActive: s.expandActive, ExpandLimit: s.cfg.ExpandConcurrency,
		SSEActive: s.sseActive, SSELimit: s.cfg.SSEMax, SSETarget: s.sseTarget,
		DownloadActive: s.downloadActive, DownloadLimit: s.cfg.DownloadConcurrency,
		ExpandQueued: len(s.expandWaiters), SSEQueued: len(s.sseWaiters), DownloadQueued: len(s.downloadWaiters),
		SampleCount: len(recentOutcomes), UpdatedAt: time.Now().UTC(),
	}
	s.mu.Unlock()

	now := snap.UpdatedAt
	snap.Slots = make([]domain.SlotSnapshot, len(slotOwners))
	for lane, owner := range slotOwners {
		snap.Slots[lane] = domain.SlotSnapshot{Lane: lane}
		if owner != nil {
			snap.ActiveSlots++
			snap.Slots[lane] = owner.slotSnapshot(lane, now)
		}
	}
	snap.Queue = make([]domain.QueueSnapshot, 0, len(waiters))
	for position, waiter := range waiters {
		trace := waiter.run.snapshotTrace()
		waitMS := max(int64(0), now.Sub(waiter.enqueuedAt).Milliseconds())
		if waitMS > snap.OldestQueueMS {
			snap.OldestQueueMS = waitMS
		}
		snap.Queue = append(snap.Queue, domain.QueueSnapshot{
			Position: position + 1, TraceID: trace.ID, RequestID: trace.RequestID,
			Model: trace.Model, EnqueuedAt: waiter.enqueuedAt, WaitMS: waitMS,
		})
	}
	if len(recentOutcomes) == 0 {
		return snap
	}
	success := 0
	totals := make([]int64, 0, len(recentOutcomes))
	expands := make([]int64, 0, len(recentOutcomes))
	sses := make([]int64, 0, len(recentOutcomes))
	downloads := make([]int64, 0, len(recentOutcomes))
	for _, item := range recentOutcomes {
		if item.ok {
			success++
		}
		totals = append(totals, item.totalMS)
		expands = append(expands, item.expandMS)
		sses = append(sses, item.sseMS)
		downloads = append(downloads, item.downloadMS)
	}
	snap.SuccessRate = float64(success) / float64(len(recentOutcomes))
	snap.P50TotalMS, snap.P90TotalMS, snap.P95TotalMS = percentiles(totals)
	snap.P50ExpandMS, snap.P90ExpandMS, _ = percentiles(expands)
	snap.P50SSEMS, snap.P90SSEMS, _ = percentiles(sses)
	snap.P50DownloadMS, snap.P90DownloadMS, _ = percentiles(downloads)
	return snap
}

func (r *Run) slotSnapshot(lane int, now time.Time) domain.SlotSnapshot {
	r.mu.Lock()
	defer r.mu.Unlock()
	stage := currentStage(r.openSegs)
	return domain.SlotSnapshot{
		Lane: lane, Occupied: true, TraceID: r.trace.ID, RequestID: r.trace.RequestID,
		Model: r.trace.Model, AccountName: r.trace.AccountName, Stage: stage,
		WaitingFor: r.waitingFor,
		Status:     r.trace.Status, StartedAt: r.trace.StartedAt,
		ActiveMS: max(int64(0), now.Sub(r.trace.StartedAt).Milliseconds()),
	}
}

func currentStage(open map[domain.Stage]uint64) domain.Stage {
	for _, stage := range []domain.Stage{domain.StageDownload, domain.StageSSE, domain.StageExpand, domain.StageQueue} {
		if _, ok := open[stage]; ok {
			return stage
		}
	}
	return ""
}

func (s *Scheduler) Timeline(ctx context.Context, from, to time.Time) (domain.Timeline, error) {
	if to.IsZero() {
		to = time.Now().UTC()
	}
	if from.IsZero() {
		from = to.Add(-30 * time.Minute)
	}
	traces, err := s.repo.ListTraces(ctx, from, to, 1000)
	if err != nil {
		return domain.Timeline{}, err
	}
	// 合并进行中的 live segments（以 now 作为临时终点）
	now := time.Now().UTC()
	s.mu.Lock()
	live := make([]*Run, 0, len(s.live))
	for _, run := range s.live {
		live = append(live, run)
	}
	s.mu.Unlock()
	for _, run := range live {
		trace := run.snapshotTrace()
		run.mu.Lock()
		for stage, segID := range run.openSegs {
			started := run.stageWait[stage]
			trace.Segments = append(trace.Segments, domain.Segment{
				ID: segID, TraceID: trace.ID, Stage: stage, StartedAt: started, EndedAt: &now, Outcome: "running",
			})
		}
		run.mu.Unlock()
		found := false
		for i := range traces {
			if traces[i].ID == trace.ID {
				traces[i] = trace
				found = true
				break
			}
		}
		if !found && !trace.StartedAt.After(to) && (trace.EndedAt == nil || !trace.EndedAt.Before(from)) {
			traces = append(traces, trace)
		}
	}
	sort.Slice(traces, func(i, j int) bool { return traces[i].StartedAt.Before(traces[j].StartedAt) })
	return domain.Timeline{
		From: from, To: to, Snapshot: s.Snapshot(), Lanes: s.cfg.PipelineSlots, Traces: traces,
	}, nil
}

func (s *Scheduler) Cleanup(ctx context.Context) (int64, error) {
	before := time.Now().UTC().Add(-s.cfg.Retention)
	return s.repo.DeleteOlderThan(ctx, before)
}

func percentiles(values []int64) (p50, p90, p95 int64) {
	if len(values) == 0 {
		return 0, 0, 0
	}
	sorted := append([]int64(nil), values...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i] < sorted[j] })
	idx := func(p int) int {
		i := (len(sorted) * p) / 100
		if i >= len(sorted) {
			i = len(sorted) - 1
		}
		return i
	}
	return sorted[idx(50)], sorted[idx(90)], sorted[idx(95)]
}

func outcomeFromErr(err error) string {
	if err == nil {
		return "ok"
	}
	if errors.Is(err, ErrQueueFull) {
		return "queue_full"
	}
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return "canceled"
	}
	return "error"
}

func newTraceID() (string, error) {
	value, err := security.NewOpaqueToken(18)
	if err != nil || value == "" {
		return "", fmt.Errorf("生成 trace id 失败: %w", err)
	}
	return "ipt_" + value, nil
}
