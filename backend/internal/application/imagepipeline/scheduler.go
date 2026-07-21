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

	mu             sync.Mutex
	slots          []bool
	queueWaiting   int
	expandActive   int
	sseActive      int
	sseTarget      int
	downloadActive int
	lastSSEStart   time.Time
	recentOutcomes []outcomeSample
	live           map[string]*Run
	expandGate     chan struct{}
	sseGate        chan struct{}
	downloadGate   chan struct{}
	slotWake       chan struct{}
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
	holdingSlot     bool
	releasedAccount bool
}

func NewScheduler(repo repository.ImagePipelineRepository, cfg Config, logger *slog.Logger) *Scheduler {
	cfg = normalizeConfig(cfg)
	if logger == nil {
		logger = slog.Default()
	}
	s := &Scheduler{
		repo: repo, logger: logger, cfg: cfg,
		slots:        make([]bool, cfg.PipelineSlots),
		sseTarget:    cfg.SSEInitial,
		live:         make(map[string]*Run),
		expandGate:   make(chan struct{}, cfg.ExpandConcurrency),
		sseGate:      make(chan struct{}, cfg.SSEMax),
		downloadGate: make(chan struct{}, cfg.DownloadConcurrency),
		slotWake:     make(chan struct{}, 1),
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
		openSegs:  make(map[domain.Stage]uint64),
		stageWait: make(map[domain.Stage]time.Time),
	}
	s.mu.Lock()
	s.live[traceID] = run
	s.mu.Unlock()

	// 先占槽再落库，避免 lane=-1 在旧库 CHECK 下写失败；槽位到手后立刻 Create。
	queueSegID := run.beginSegment(domain.StageQueue, now)

	lane, err := s.acquireSlot(ctx)
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

func (s *Scheduler) acquireSlot(ctx context.Context) (int, error) {
	s.mu.Lock()
	if lane, ok := s.findFreeSlotLocked(); ok {
		s.slots[lane] = true
		s.mu.Unlock()
		return lane, nil
	}
	if s.queueWaiting >= s.cfg.QueueCapacity {
		s.mu.Unlock()
		return -1, ErrQueueFull
	}
	s.queueWaiting++
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		s.queueWaiting--
		s.mu.Unlock()
	}()

	for {
		select {
		case <-ctx.Done():
			return -1, ctx.Err()
		case <-s.slotWake:
		case <-time.After(50 * time.Millisecond):
		}
		s.mu.Lock()
		if lane, ok := s.findFreeSlotLocked(); ok {
			s.slots[lane] = true
			s.mu.Unlock()
			return lane, nil
		}
		s.mu.Unlock()
	}
}

func (s *Scheduler) findFreeSlotLocked() (int, bool) {
	for i, used := range s.slots {
		if !used {
			return i, true
		}
	}
	return -1, false
}

func (s *Scheduler) releaseSlot(lane int) {
	if lane < 0 {
		return
	}
	s.mu.Lock()
	if lane < len(s.slots) {
		s.slots[lane] = false
	}
	s.mu.Unlock()
	select {
	case s.slotWake <- struct{}{}:
	default:
	}
}

func (r *Run) AcquireExpand(ctx context.Context) error {
	return r.acquirePool(ctx, domain.StageExpand, r.scheduler.expandGate, func() {
		r.scheduler.mu.Lock()
		r.scheduler.expandActive++
		r.scheduler.mu.Unlock()
	})
}

func (r *Run) ReleaseExpand() {
	r.releasePool(domain.StageExpand, r.scheduler.expandGate, func() {
		r.scheduler.mu.Lock()
		if r.scheduler.expandActive > 0 {
			r.scheduler.expandActive--
		}
		r.scheduler.mu.Unlock()
	})
}

func (r *Run) AcquireSSE(ctx context.Context) error {
	if err := r.waitSSEBudget(ctx); err != nil {
		return err
	}
	return r.acquirePool(ctx, domain.StageSSE, r.scheduler.sseGate, func() {
		r.scheduler.mu.Lock()
		r.scheduler.sseActive++
		r.scheduler.lastSSEStart = time.Now().UTC()
		r.scheduler.mu.Unlock()
	})
}

func (r *Run) ReleaseSSE() {
	r.releasePool(domain.StageSSE, r.scheduler.sseGate, func() {
		r.scheduler.mu.Lock()
		if r.scheduler.sseActive > 0 {
			r.scheduler.sseActive--
		}
		r.scheduler.mu.Unlock()
	})
}

func (r *Run) AcquireDownload(ctx context.Context) error {
	return r.acquirePool(ctx, domain.StageDownload, r.scheduler.downloadGate, func() {
		r.scheduler.mu.Lock()
		r.scheduler.downloadActive++
		r.scheduler.mu.Unlock()
	})
}

func (r *Run) ReleaseDownload() {
	r.releasePool(domain.StageDownload, r.scheduler.downloadGate, func() {
		r.scheduler.mu.Lock()
		if r.scheduler.downloadActive > 0 {
			r.scheduler.downloadActive--
		}
		r.scheduler.mu.Unlock()
	})
}

func (r *Run) waitSSEBudget(ctx context.Context) error {
	s := r.scheduler
	for {
		s.mu.Lock()
		target := s.sseTarget
		active := s.sseActive
		staggerOK := s.lastSSEStart.IsZero() || time.Since(s.lastSSEStart) >= s.cfg.SSEStagger
		s.mu.Unlock()
		if active < target && staggerOK {
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(50 * time.Millisecond):
		}
	}
}

func (r *Run) acquirePool(ctx context.Context, stage domain.Stage, gate chan struct{}, onAcquire func()) error {
	waitStart := time.Now().UTC()
	segID := r.beginSegment(domain.StageQueue, waitStart)
	select {
	case gate <- struct{}{}:
		ended := time.Now().UTC()
		r.endSegment(segID, domain.StageQueue, ended, "acquired")
		r.mu.Lock()
		r.trace.QueueMS += ended.Sub(waitStart).Milliseconds()
		r.mu.Unlock()
		onAcquire()
		r.beginSegment(stage, ended)
		return nil
	case <-ctx.Done():
		ended := time.Now().UTC()
		r.endSegment(segID, domain.StageQueue, ended, "canceled")
		return ctx.Err()
	}
}

func (r *Run) releasePool(stage domain.Stage, gate chan struct{}, onRelease func()) {
	now := time.Now().UTC()
	r.mu.Lock()
	segID, ok := r.openSegs[stage]
	started := r.stageWait[stage]
	delete(r.openSegs, stage)
	delete(r.stageWait, stage)
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
	onRelease()
	select {
	case <-gate:
	default:
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
	_ = r.scheduler.persistUpdate(r.snapshotTrace())
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
	r.finishLocked(status, errorCode, ended)
	r.mu.Lock()
	softStop = softStop || r.trace.SoftStop
	r.trace.SoftStop = softStop
	sample := outcomeSample{
		ok: status == domain.StatusSucceeded, softStop: softStop,
		rateLimit: errorCode == "usage_limit_reached" || strings.Contains(errorCode, "rate_limit"),
		totalMS: r.trace.TotalMS, expandMS: r.trace.ExpandMS, sseMS: r.trace.SSEMS, downloadMS: r.trace.DownloadMS,
		at: ended,
	}
	lane := r.trace.Lane
	holding := r.holdingSlot
	r.holdingSlot = false
	id := r.trace.ID
	r.mu.Unlock()
	r.scheduler.recordOutcome(sample)
	if holding {
		r.scheduler.releaseSlot(lane)
	}
	r.scheduler.mu.Lock()
	delete(r.scheduler.live, id)
	r.scheduler.mu.Unlock()
	_ = r.scheduler.persistUpdate(r.snapshotTrace())
}

func (r *Run) finishLocked(status domain.Status, errorCode string, ended time.Time) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.trace.EndedAt != nil {
		return
	}
	for stage, segID := range r.openSegs {
		r.scheduler.closeSegmentAsync(segID, ended, "aborted")
		delete(r.openSegs, stage)
	}
	r.trace.Status = status
	r.trace.ErrorCode = errorCode
	r.trace.EndedAt = &ended
	r.trace.TotalMS = ended.Sub(r.trace.StartedAt).Milliseconds()
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
	defer s.mu.Unlock()
	activeSlots := 0
	for _, used := range s.slots {
		if used {
			activeSlots++
		}
	}
	snap := domain.Snapshot{
		PipelineSlots: s.cfg.PipelineSlots, ActiveSlots: activeSlots,
		QueueDepth: s.queueWaiting, QueueCapacity: s.cfg.QueueCapacity,
		ExpandActive: s.expandActive, ExpandLimit: s.cfg.ExpandConcurrency,
		SSEActive: s.sseActive, SSELimit: s.cfg.SSEMax, SSETarget: s.sseTarget,
		DownloadActive: s.downloadActive, DownloadLimit: s.cfg.DownloadConcurrency,
		SampleCount: len(s.recentOutcomes), UpdatedAt: time.Now().UTC(),
	}
	if len(s.recentOutcomes) == 0 {
		return snap
	}
	success := 0
	totals := make([]int64, 0, len(s.recentOutcomes))
	expands := make([]int64, 0, len(s.recentOutcomes))
	sses := make([]int64, 0, len(s.recentOutcomes))
	downloads := make([]int64, 0, len(s.recentOutcomes))
	for _, item := range s.recentOutcomes {
		if item.ok {
			success++
		}
		totals = append(totals, item.totalMS)
		expands = append(expands, item.expandMS)
		sses = append(sses, item.sseMS)
		downloads = append(downloads, item.downloadMS)
	}
	snap.SuccessRate = float64(success) / float64(len(s.recentOutcomes))
	snap.P50TotalMS, snap.P90TotalMS, snap.P95TotalMS = percentiles(totals)
	snap.P50ExpandMS, snap.P90ExpandMS, _ = percentiles(expands)
	snap.P50SSEMS, snap.P90SSEMS, _ = percentiles(sses)
	snap.P50DownloadMS, snap.P90DownloadMS, _ = percentiles(downloads)
	return snap
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
	for _, run := range s.live {
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
	s.mu.Unlock()
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
