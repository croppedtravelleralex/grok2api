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

	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
	domain "github.com/chenyme/grok2api/backend/internal/domain/imagepipeline"
	"github.com/chenyme/grok2api/backend/internal/infra/security"
	"github.com/chenyme/grok2api/backend/internal/repository"
)

var (
	ErrQueueFull     = errors.New("生图流水线队列已满")
	ErrNotConfigured = errors.New("生图流水线未配置")
)

const (
	MultiImageModeFast    = "fast"
	MultiImageModeDiverse = "diverse"
)

type Config struct {
	PromptSlots         int
	SSESlots            int
	UploadConcurrency   int
	QueueCapacity       int
	DownloadConcurrency int
	Retention           time.Duration

	// Deprecated: mapped into PromptSlots during normalize.
	PipelineSlots int
	// Deprecated: ignored by v2 orchestrator.
	ExpandConcurrency int
	SSEMin            int
	SSEInitial        int
	SSEMax            int
	SSEStagger        time.Duration
}

func DefaultConfig() Config {
	return Config{
		PromptSlots: 10, SSESlots: 10, UploadConcurrency: 8,
		QueueCapacity: 100, DownloadConcurrency: 8, Retention: 12 * time.Hour,
	}
}

type Scheduler struct {
	repo   repository.ImagePipelineRepository
	logger *slog.Logger
	cfg    Config

	mu sync.Mutex

	inFlight int
	live     map[string]*Run

	psSlots   []*Run
	psWaiters []*poolWaiter
	ssSlots   []*Run
	ssWaiters []*poolWaiter

	uploadActive    int
	uploadWaiters   []*semWaiter
	downloadActive  int
	downloadWaiters []*semWaiter

	recentOutcomes []outcomeSample
}

type poolWaiter struct {
	run        *Run
	enqueuedAt time.Time
	grant      chan int
	granted    bool
	slot       int
}

type semWaiter struct {
	run        *Run
	stage      domain.Stage
	enqueuedAt time.Time
	grant      chan struct{}
	granted    bool
}

type outcomeSample struct {
	ok         bool
	softStop   bool
	rateLimit  bool
	totalMS    int64
	expandMS   int64
	sseMS      int64
	downloadMS int64
	at         time.Time
}

type AdmitInput struct {
	RequestID      string
	Model          string
	ExpandPrompt   bool
	MultiImageMode string
	NeedsUpload    bool
}

type RunArtifacts struct {
	ExpandedPrompt       string
	ImageURLs              []string
	ChromeDeviceCookie     string
	ChromeDownloadCookie   string
	ChromeUserAgent        string
	UploadAccountID        *uint64
	PSAccountID            *uint64
	SSAccountID            *uint64
	// SSCredential 保存出图阶段实际使用的完整凭据。下载图片必须用同一账号的访问
	// 令牌；只带 ID 的空壳凭据会让请求以未认证身份发出，被资产源站直接 403。
	SSCredential accountdomain.Credential
}

type Run struct {
	scheduler *Scheduler
	trace     domain.Trace
	mu        sync.Mutex

	seq        int
	openSegs   map[string]uint64
	stageWait  map[string]time.Time
	heldStages map[domain.Stage]bool

	expandPrompt   bool
	multiImageMode string
	needsUpload    bool
	phase          domain.PhaseCursor
	artifacts      RunArtifacts
	expandedOnce   bool

	psSlot int
	ssSlot int

	releasedAccount bool
	waitingFor      domain.Stage
}

type Timing struct {
	QueueMS         int64
	UploadQueueMS   int64
	PSQueueMS       int64
	SSQueueMS       int64
	DownloadQueueMS int64
	ExpandMS        int64
	SSEMS           int64
	DownloadMS      int64
}

func NewScheduler(repo repository.ImagePipelineRepository, cfg Config, logger *slog.Logger) *Scheduler {
	cfg = normalizeConfig(cfg)
	if logger == nil {
		logger = slog.Default()
	}
	return &Scheduler{
		repo: repo, logger: logger, cfg: cfg,
		psSlots:   make([]*Run, cfg.PromptSlots),
		ssSlots:   make([]*Run, cfg.SSESlots),
		live:      make(map[string]*Run),
	}
}

func normalizeConfig(cfg Config) Config {
	def := DefaultConfig()
	if cfg.PromptSlots < 1 {
		if cfg.PipelineSlots > 0 {
			cfg.PromptSlots = cfg.PipelineSlots
		} else {
			cfg.PromptSlots = def.PromptSlots
		}
	}
	if cfg.PromptSlots > 64 {
		cfg.PromptSlots = 64
	}
	if cfg.SSESlots < 1 {
		if cfg.PipelineSlots > 0 {
			cfg.SSESlots = cfg.PipelineSlots
		} else {
			cfg.SSESlots = def.SSESlots
		}
	}
	if cfg.SSESlots > 64 {
		cfg.SSESlots = 64
	}
	if cfg.UploadConcurrency < 1 {
		cfg.UploadConcurrency = def.UploadConcurrency
	}
	if cfg.QueueCapacity < 1 {
		cfg.QueueCapacity = def.QueueCapacity
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
	s.mu.Lock()
	if s.inFlight >= s.cfg.QueueCapacity {
		s.mu.Unlock()
		return nil, ErrQueueFull
	}
	s.inFlight++
	s.mu.Unlock()

	traceID, err := newTraceID()
	if err != nil {
		s.decrementInFlight()
		return nil, err
	}
	mode := strings.TrimSpace(strings.ToLower(input.MultiImageMode))
	if mode == "" {
		mode = MultiImageModeFast
	}
	now := time.Now().UTC()
	run := &Run{
		scheduler: s,
		trace: domain.Trace{
			ID: traceID, RequestID: input.RequestID, Status: domain.StatusRunning,
			Model: input.Model, StartedAt: now, Lane: -1,
		},
		openSegs:       make(map[string]uint64),
		stageWait:      make(map[string]time.Time),
		heldStages:     make(map[domain.Stage]bool),
		expandPrompt:   input.ExpandPrompt,
		multiImageMode: mode,
		needsUpload:    input.NeedsUpload,
		phase:          domain.PhaseAdmitted,
	}
	s.mu.Lock()
	s.live[traceID] = run
	s.mu.Unlock()

	if persistErr := s.persistCreate(run.snapshotTrace()); persistErr != nil {
		s.logger.Warn("image_pipeline_trace_create_failed", "error", persistErr)
	}
	select {
	case <-ctx.Done():
		run.Finish(domain.StatusCanceled, "canceled", false)
		return nil, ctx.Err()
	default:
	}
	return run, nil
}

func (s *Scheduler) decrementInFlight() {
	s.mu.Lock()
	if s.inFlight > 0 {
		s.inFlight--
	}
	s.mu.Unlock()
}

func (r *Run) NeedsPS(prompt string) bool {
	if !r.expandPrompt {
		return false
	}
	return shouldExpandImagePrompt(prompt)
}

func shouldExpandImagePrompt(prompt string) bool {
	trimmed := strings.TrimSpace(prompt)
	if trimmed == "" {
		return false
	}
	if len([]rune(trimmed)) < 48 {
		return true
	}
	return len(strings.Fields(trimmed)) < 8
}

func (r *Run) NeedsUpload() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.needsUpload
}

func (r *Run) MultiImageMode() string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.multiImageMode
}

func (r *Run) Phase() domain.PhaseCursor {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.phase
}

func (r *Run) SetPhase(phase domain.PhaseCursor) {
	r.mu.Lock()
	r.phase = phase
	r.mu.Unlock()
}

func (r *Run) Artifacts() RunArtifacts {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.artifacts
}

func (r *Run) SetExpandedPrompt(value string) {
	r.mu.Lock()
	r.artifacts.ExpandedPrompt = strings.TrimSpace(value)
	r.expandedOnce = true
	r.mu.Unlock()
}

func (r *Run) ExpandedOnce() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.expandedOnce
}

func (r *Run) AppendImageURL(value string) {
	r.mu.Lock()
	r.artifacts.ImageURLs = append(r.artifacts.ImageURLs, value)
	r.mu.Unlock()
}

func (r *Run) SetChromeDeviceCookie(value string) {
	r.mu.Lock()
	r.artifacts.ChromeDeviceCookie = strings.TrimSpace(value)
	r.mu.Unlock()
}

func (r *Run) SetChromeDownloadCookie(value string) {
	r.mu.Lock()
	r.artifacts.ChromeDownloadCookie = strings.TrimSpace(value)
	r.mu.Unlock()
}

func (r *Run) SetChromeUserAgent(value string) {
	r.mu.Lock()
	r.artifacts.ChromeUserAgent = strings.TrimSpace(value)
	r.mu.Unlock()
}

func (r *Run) PSAccountID() *uint64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.artifacts.PSAccountID
}

func (r *Run) SetPSAccount(id uint64) {
	r.mu.Lock()
	r.artifacts.PSAccountID = &id
	r.mu.Unlock()
}

func (r *Run) SetSSAccount(id uint64) {
	r.mu.Lock()
	r.artifacts.SSAccountID = &id
	r.mu.Unlock()
}

// SetSSCredential 记录出图阶段的完整凭据，供后续下载图片复用同一账号的令牌。
func (r *Run) SetSSCredential(credential accountdomain.Credential) {
	r.mu.Lock()
	id := credential.ID
	r.artifacts.SSAccountID = &id
	r.artifacts.SSCredential = credential
	r.mu.Unlock()
}

func (r *Run) SetUploadAccount(id uint64) {
	r.mu.Lock()
	r.artifacts.UploadAccountID = &id
	r.mu.Unlock()
}

func (r *Run) AcquireUpload(ctx context.Context) error {
	return r.acquireSemaphore(ctx, domain.StageUpload, domain.StageQueueUpload, func() int {
		return r.scheduler.cfg.UploadConcurrency
	}, func() *int { return &r.scheduler.uploadActive }, &r.scheduler.uploadWaiters)
}

func (r *Run) ReleaseUpload() {
	r.releaseSemaphore(domain.StageUpload)
}

func (r *Run) AcquirePS(ctx context.Context) (int, error) {
	return r.acquirePoolSlot(ctx, domain.StagePS, domain.StageQueuePS, r.scheduler.psSlots, &r.scheduler.psWaiters, func(slot int) { r.psSlot = slot })
}

func (r *Run) ReleasePS() {
	r.releasePoolSlot(domain.StagePS, r.psSlot, r.scheduler.psSlots, &r.scheduler.psWaiters)
	r.psSlot = -1
}

func (r *Run) AcquireExpand(ctx context.Context) error {
	_, err := r.AcquirePS(ctx)
	return err
}

func (r *Run) ReleaseExpand() {
	r.ReleasePS()
}

func (r *Run) AcquireSS(ctx context.Context) (int, error) {
	return r.acquirePoolSlot(ctx, domain.StageSSE, domain.StageQueueSS, r.scheduler.ssSlots, &r.scheduler.ssWaiters, func(slot int) { r.ssSlot = slot })
}

func (r *Run) ReleaseSS() {
	r.releasePoolSlot(domain.StageSSE, r.ssSlot, r.scheduler.ssSlots, &r.scheduler.ssWaiters)
	r.ssSlot = -1
}

func (r *Run) AcquireSSE(ctx context.Context) error {
	_, err := r.AcquireSS(ctx)
	return err
}

func (r *Run) ReleaseSSE() {
	r.ReleaseSS()
}

func (r *Run) AcquireDownload(ctx context.Context) error {
	return r.acquireSemaphore(ctx, domain.StageDownload, domain.StageQueueDownload, func() int {
		return r.scheduler.cfg.DownloadConcurrency
	}, func() *int { return &r.scheduler.downloadActive }, &r.scheduler.downloadWaiters)
}

func (r *Run) ReleaseDownload() {
	r.releaseSemaphore(domain.StageDownload)
}

func (r *Run) acquirePoolSlot(ctx context.Context, stage, queueStage domain.Stage, slots []*Run, waiters *[]*poolWaiter, onGrant func(int)) (int, error) {
	waitStart := time.Now().UTC()
	queueSeg := r.beginSegment(queueStage, waitStart, -1)
	waiter := &poolWaiter{run: r, enqueuedAt: waitStart, grant: make(chan int, 1), slot: -1}
	r.scheduler.mu.Lock()
	if slot, ok := firstFreeSlot(slots); ok {
		slots[slot] = r
		r.scheduler.mu.Unlock()
		ended := time.Now().UTC()
		r.addQueueMS(queueStage, ended.Sub(waitStart))
		r.endSegment(queueSeg, queueStage, ended, "acquired")
		r.beginSegment(stage, ended, slot)
		onGrant(slot)
		r.mu.Lock()
		r.heldStages[stage] = true
		r.mu.Unlock()
		return slot, nil
	}
	*waiters = append(*waiters, waiter)
	r.scheduler.mu.Unlock()

	select {
	case slot := <-waiter.grant:
		ended := time.Now().UTC()
		r.addQueueMS(queueStage, ended.Sub(waitStart))
		r.endSegment(queueSeg, queueStage, ended, "acquired")
		r.beginSegment(stage, ended, slot)
		onGrant(slot)
		r.mu.Lock()
		r.heldStages[stage] = true
		r.mu.Unlock()
		return slot, nil
	case <-ctx.Done():
		r.scheduler.mu.Lock()
		if !waiter.granted {
			removePoolWaiter(waiters, waiter)
		} else if waiter.slot >= 0 && waiter.slot < len(slots) && slots[waiter.slot] == r {
			slots[waiter.slot] = nil
			r.scheduler.grantPoolWaiters(slots, waiters)
		}
		r.scheduler.mu.Unlock()
		ended := time.Now().UTC()
		r.endSegment(queueSeg, queueStage, ended, "canceled")
		return -1, ctx.Err()
	}
}

func (r *Run) releasePoolSlot(stage domain.Stage, slot int, slots []*Run, waiters *[]*poolWaiter) {
	now := time.Now().UTC()
	r.mu.Lock()
	key := stageKey(stage, slot)
	segID, ok := r.openSegs[key]
	started := r.stageWait[key]
	held := r.heldStages[stage]
	delete(r.openSegs, key)
	delete(r.stageWait, key)
	delete(r.heldStages, stage)
	r.mu.Unlock()
	if ok {
		r.endSegment(segID, stage, now, "ok")
		if !started.IsZero() {
			ms := now.Sub(started).Milliseconds()
			r.mu.Lock()
			switch stage {
			case domain.StagePS:
				r.trace.ExpandMS += ms
			case domain.StageSSE:
				r.trace.SSEMS += ms
			}
			r.mu.Unlock()
		}
	}
	if held && slot >= 0 {
		r.scheduler.mu.Lock()
		if slot < len(slots) && slots[slot] == r {
			slots[slot] = nil
			r.scheduler.grantPoolWaiters(slots, waiters)
		}
		r.scheduler.mu.Unlock()
	}
}

func (s *Scheduler) grantPoolWaiters(slots []*Run, waiters *[]*poolWaiter) {
	for len(*waiters) > 0 {
		slot, ok := firstFreeSlot(slots)
		if !ok {
			return
		}
		waiter := (*waiters)[0]
		*waiters = (*waiters)[1:]
		waiter.granted = true
		waiter.slot = slot
		slots[slot] = waiter.run
		waiter.grant <- slot
	}
}

func firstFreeSlot(slots []*Run) (int, bool) {
	for i, owner := range slots {
		if owner == nil {
			return i, true
		}
	}
	return -1, false
}

func removePoolWaiter(waiters *[]*poolWaiter, target *poolWaiter) {
	for i, waiter := range *waiters {
		if waiter == target {
			*waiters = append((*waiters)[:i], (*waiters)[i+1:]...)
			return
		}
	}
}

func (r *Run) acquireSemaphore(ctx context.Context, stage, queueStage domain.Stage, limitFn func() int, activeFn func() *int, waiters *[]*semWaiter) error {
	waitStart := time.Now().UTC()
	queueSeg := r.beginSegment(queueStage, waitStart, -1)
	waiter := &semWaiter{run: r, stage: stage, enqueuedAt: waitStart, grant: make(chan struct{}, 1)}
	r.scheduler.mu.Lock()
	active := activeFn()
	if *active < limitFn() {
		*active++
		r.scheduler.mu.Unlock()
		ended := time.Now().UTC()
		r.addQueueMS(queueStage, ended.Sub(waitStart))
		r.endSegment(queueSeg, queueStage, ended, "acquired")
		r.beginSegment(stage, ended, -1)
		r.mu.Lock()
		r.heldStages[stage] = true
		r.mu.Unlock()
		return nil
	}
	*waiters = append(*waiters, waiter)
	r.scheduler.mu.Unlock()

	select {
	case <-waiter.grant:
		ended := time.Now().UTC()
		r.addQueueMS(queueStage, ended.Sub(waitStart))
		r.endSegment(queueSeg, queueStage, ended, "acquired")
		r.beginSegment(stage, ended, -1)
		r.mu.Lock()
		r.heldStages[stage] = true
		r.mu.Unlock()
		return nil
	case <-ctx.Done():
		r.scheduler.mu.Lock()
		if waiter.granted {
			if *activeFn() > 0 {
				*activeFn()--
			}
			r.scheduler.grantSemWaiters(limitFn, activeFn(), waiters)
		} else {
			removeSemWaiter(waiters, waiter)
		}
		r.scheduler.mu.Unlock()
		r.endSegment(queueSeg, queueStage, time.Now().UTC(), "canceled")
		return ctx.Err()
	}
}

func (s *Scheduler) grantSemWaiters(limitFn func() int, active *int, waiters *[]*semWaiter) {
	for len(*waiters) > 0 && *active < limitFn() {
		waiter := (*waiters)[0]
		*waiters = (*waiters)[1:]
		waiter.granted = true
		*active++
		waiter.grant <- struct{}{}
	}
}

func removeSemWaiter(waiters *[]*semWaiter, target *semWaiter) {
	for i, waiter := range *waiters {
		if waiter == target {
			*waiters = append((*waiters)[:i], (*waiters)[i+1:]...)
			return
		}
	}
}

func (r *Run) releaseSemaphore(stage domain.Stage) {
	now := time.Now().UTC()
	r.mu.Lock()
	key := stageKey(stage, -1)
	segID, ok := r.openSegs[key]
	started := r.stageWait[key]
	held := r.heldStages[stage]
	delete(r.openSegs, key)
	delete(r.stageWait, key)
	delete(r.heldStages, stage)
	r.mu.Unlock()
	if ok {
		r.endSegment(segID, stage, now, "ok")
		if !started.IsZero() {
			ms := now.Sub(started).Milliseconds()
			r.mu.Lock()
			switch stage {
			case domain.StageUpload:
				// upload execution tracked separately if needed
			case domain.StageDownload:
				r.trace.DownloadMS += ms
			}
			r.mu.Unlock()
		}
	}
	if !held {
		return
	}
	r.scheduler.mu.Lock()
	switch stage {
	case domain.StageUpload:
		if r.scheduler.uploadActive > 0 {
			r.scheduler.uploadActive--
		}
		r.scheduler.grantSemWaiters(func() int { return r.scheduler.cfg.UploadConcurrency }, &r.scheduler.uploadActive, &r.scheduler.uploadWaiters)
	case domain.StageDownload:
		if r.scheduler.downloadActive > 0 {
			r.scheduler.downloadActive--
		}
		r.scheduler.grantSemWaiters(func() int { return r.scheduler.cfg.DownloadConcurrency }, &r.scheduler.downloadActive, &r.scheduler.downloadWaiters)
	}
	r.scheduler.mu.Unlock()
}

func stageKey(stage domain.Stage, slot int) string {
	if slot >= 0 {
		return fmt.Sprintf("%s:%d", stage, slot)
	}
	return string(stage)
}

func (r *Run) addQueueMS(stage domain.Stage, d time.Duration) {
	ms := d.Milliseconds()
	r.mu.Lock()
	switch stage {
	case domain.StageQueueUpload:
		r.trace.UploadQueueMS += ms
	case domain.StageQueuePS:
		r.trace.PSQueueMS += ms
	case domain.StageQueueSS:
		r.trace.SSQueueMS += ms
	case domain.StageQueueDownload:
		r.trace.DownloadQueueMS += ms
	}
	r.trace.QueueMS += ms
	r.mu.Unlock()
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

// PrepareRetry clears transient failure markers before another staged upstream attempt.
func (r *Run) PrepareRetry() {
	r.mu.Lock()
	r.trace.SoftStop = false
	r.mu.Unlock()
}

func (r *Run) Finish(status domain.Status, errorCode string, softStop bool) {
	ended := time.Now().UTC()
	heldStages := r.finishLocked(status, errorCode, ended)
	for _, stage := range heldStages {
		switch stage {
		case domain.StageUpload:
			r.releaseSemaphore(stage)
		case domain.StageDownload:
			r.releaseSemaphore(stage)
		case domain.StagePS:
			r.releasePoolSlot(stage, r.psSlot, r.scheduler.psSlots, &r.scheduler.psWaiters)
		case domain.StageSSE:
			r.releasePoolSlot(stage, r.ssSlot, r.scheduler.ssSlots, &r.scheduler.ssWaiters)
		}
	}
	r.mu.Lock()
	r.heldStages = make(map[domain.Stage]bool)
	softStop = softStop || r.trace.SoftStop
	r.trace.SoftStop = softStop
	sample := outcomeSample{
		ok: status == domain.StatusSucceeded, softStop: softStop,
		rateLimit: errorCode == "usage_limit_reached" || strings.Contains(errorCode, "rate_limit"),
		totalMS:   r.trace.TotalMS, expandMS: r.trace.ExpandMS, sseMS: r.trace.SSEMS, downloadMS: r.trace.DownloadMS,
		at: ended,
	}
	id := r.trace.ID
	r.mu.Unlock()
	r.scheduler.recordOutcome(sample)
	r.scheduler.mu.Lock()
	delete(r.scheduler.live, id)
	r.scheduler.mu.Unlock()
	r.scheduler.decrementInFlight()
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
	}
	for key, segID := range r.openSegs {
		r.scheduler.closeSegmentAsync(segID, ended, "aborted")
		delete(r.openSegs, key)
		delete(r.stageWait, key)
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
	return Timing{
		QueueMS: r.trace.QueueMS, UploadQueueMS: r.trace.UploadQueueMS, PSQueueMS: r.trace.PSQueueMS,
		SSQueueMS: r.trace.SSQueueMS, DownloadQueueMS: r.trace.DownloadQueueMS,
		ExpandMS: r.trace.ExpandMS, SSEMS: r.trace.SSEMS, DownloadMS: r.trace.DownloadMS,
	}
}

func (r *Run) beginSegment(stage domain.Stage, at time.Time, slot int) uint64 {
	r.mu.Lock()
	r.seq++
	seq := r.seq
	traceID := r.trace.ID
	key := stageKey(stage, slot)
	r.stageWait[key] = at
	if slot >= 0 {
		r.trace.Lane = slot
	}
	r.mu.Unlock()
	seg, err := r.scheduler.repo.AppendSegment(context.Background(), domain.Segment{
		TraceID: traceID, Stage: stage, Slot: slot, Sequence: seq, StartedAt: at,
	})
	if err != nil {
		r.scheduler.logger.Warn("image_pipeline_segment_append_failed", "stage", stage, "error", err)
		return 0
	}
	r.mu.Lock()
	r.openSegs[key] = seg.ID
	r.mu.Unlock()
	return seg.ID
}

func (r *Run) endSegment(id uint64, stage domain.Stage, at time.Time, outcome string) {
	if id == 0 {
		return
	}
	r.mu.Lock()
	key := stageKey(stage, stageSlotFor(stage, r))
	if open, ok := r.openSegs[key]; ok && open == id {
		delete(r.openSegs, key)
	}
	r.mu.Unlock()
	r.scheduler.closeSegmentAsync(id, at, outcome)
}

func stageSlotFor(stage domain.Stage, r *Run) int {
	switch stage {
	case domain.StagePS:
		return r.psSlot
	case domain.StageSSE:
		return r.ssSlot
	default:
		return -1
	}
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
}

func (s *Scheduler) Snapshot() domain.Snapshot {
	s.mu.Lock()
	psSlots := append([]*Run(nil), s.psSlots...)
	ssSlots := append([]*Run(nil), s.ssSlots...)
	psWaiters := len(s.psWaiters)
	ssWaiters := len(s.ssWaiters)
	uploadWaiters := len(s.uploadWaiters)
	downloadWaiters := len(s.downloadWaiters)
	recentOutcomes := append([]outcomeSample(nil), s.recentOutcomes...)
	snap := domain.Snapshot{
		PromptSlots: s.cfg.PromptSlots, PromptActive: countOccupied(psSlots), PromptQueued: psWaiters,
		SSESlots: s.cfg.SSESlots, SSEActive: countOccupied(ssSlots), SSEQueued: ssWaiters,
		UploadActive: s.uploadActive, UploadLimit: s.cfg.UploadConcurrency, UploadQueued: uploadWaiters,
		DownloadActive: s.downloadActive, DownloadLimit: s.cfg.DownloadConcurrency, DownloadQueued: downloadWaiters,
		InFlight: s.inFlight, QueueCapacity: s.cfg.QueueCapacity, SampleCount: len(recentOutcomes),
		UpdatedAt: time.Now().UTC(),
		PipelineSlots: s.cfg.PromptSlots + s.cfg.SSESlots,
		ActiveSlots:   countOccupied(psSlots) + countOccupied(ssSlots),
		ExpandActive:  countOccupied(psSlots), ExpandLimit: s.cfg.PromptSlots, ExpandQueued: psWaiters,
		SSELimit: s.cfg.SSESlots, SSETarget: s.cfg.SSESlots,
		QueueDepth: psWaiters + ssWaiters + uploadWaiters + downloadWaiters,
	}
	s.mu.Unlock()

	now := snap.UpdatedAt
	snap.PSSlots = slotSnapshots(psSlots, "ps", now)
	snap.SSSlots = slotSnapshots(ssSlots, "ss", now)
	snap.Slots = append(append([]domain.SlotSnapshot(nil), snap.PSSlots...), snap.SSSlots...)
	if len(recentOutcomes) > 0 {
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
	}
	return snap
}

func countOccupied(slots []*Run) int {
	n := 0
	for _, owner := range slots {
		if owner != nil {
			n++
		}
	}
	return n
}

func slotSnapshots(slots []*Run, pool string, now time.Time) []domain.SlotSnapshot {
	out := make([]domain.SlotSnapshot, len(slots))
	for lane, owner := range slots {
		out[lane] = domain.SlotSnapshot{Lane: lane, Pool: pool}
		if owner != nil {
			out[lane] = owner.slotSnapshot(lane, pool, now)
		}
	}
	return out
}

func (r *Run) slotSnapshot(lane int, pool string, now time.Time) domain.SlotSnapshot {
	r.mu.Lock()
	defer r.mu.Unlock()
	stage := currentStageLocked(r.openSegs)
	return domain.SlotSnapshot{
		Lane: lane, Pool: pool, Occupied: true, TraceID: r.trace.ID, RequestID: r.trace.RequestID,
		Model: r.trace.Model, AccountName: r.trace.AccountName, Stage: stage, WaitingFor: r.waitingFor,
		Status: r.trace.Status, StartedAt: r.trace.StartedAt,
		ActiveMS: max(int64(0), now.Sub(r.trace.StartedAt).Milliseconds()),
	}
}

func currentStageLocked(open map[string]uint64) domain.Stage {
	for _, stage := range []domain.Stage{domain.StageDownload, domain.StageSSE, domain.StagePS, domain.StageUpload, domain.StageQueueDownload, domain.StageQueueSS, domain.StageQueuePS} {
		if _, ok := open[stageKey(stage, -1)]; ok {
			return stage
		}
	}
	for key := range open {
		if strings.HasPrefix(key, string(domain.StageSSE)+":") {
			return domain.StageSSE
		}
		if strings.HasPrefix(key, string(domain.StagePS)+":") {
			return domain.StagePS
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
		for key, segID := range run.openSegs {
			stage, slot := parseStageKey(key)
			started := run.stageWait[key]
			trace.Segments = append(trace.Segments, domain.Segment{
				ID: segID, TraceID: trace.ID, Stage: stage, Slot: slot, StartedAt: started, EndedAt: &now, Outcome: "running",
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
		From: from, To: to, Snapshot: s.Snapshot(),
		Lanes: domain.LaneLayout{PS: s.cfg.PromptSlots, SS: s.cfg.SSESlots},
		Traces: traces,
	}, nil
}

func parseStageKey(key string) (domain.Stage, int) {
	if idx := strings.LastIndex(key, ":"); idx > 0 {
		slot := -1
		fmt.Sscanf(key[idx+1:], "%d", &slot)
		return domain.Stage(key[:idx]), slot
	}
	return domain.Stage(key), -1
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

func newTraceID() (string, error) {
	value, err := security.NewOpaqueToken(18)
	if err != nil || value == "" {
		return "", fmt.Errorf("生成 trace id 失败: %w", err)
	}
	return "ipt_" + value, nil
}
