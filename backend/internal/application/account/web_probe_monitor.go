package account

import (
	"context"
	"sort"
	"strings"
	"sync"
	"time"

	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
	"github.com/chenyme/grok2api/backend/internal/repository"
)

const maxRecentWebProbeResults = 20

type WebProbeMode string
type WebProbeOutcome string

const (
	WebProbeModeDispatch           WebProbeMode = "dispatch"
	WebProbeModeRecoveryVerify     WebProbeMode = "recoveryVerify"
	WebProbeModeRecoveryCooldown   WebProbeMode = "recoveryCooldown"
	WebProbeModeDead               WebProbeMode = "dead"

	WebProbeOutcomeDispatchOK WebProbeOutcome = "dispatchOk"
	WebProbeOutcomeRecoveryOK WebProbeOutcome = "recoveryOk"
	WebProbeOutcomeDeadOK     WebProbeOutcome = "deadOk"
	WebProbeOutcomeCooldown   WebProbeOutcome = "cooldown"
	WebProbeOutcomeFailed     WebProbeOutcome = "failed"
)

type WebProbeCurrent struct {
	AccountID   uint64
	AccountName string
	Lane        WebLane
	Mode        WebProbeMode
	StartedAt   time.Time
}

type WebProbeResult struct {
	AccountID   uint64
	AccountName string
	Lane        WebLane
	Mode        WebProbeMode
	Outcome     WebProbeOutcome
	Pool        string
	Error       string
	StartedAt   time.Time
	CompletedAt time.Time
	Duration    time.Duration
}

type WebProbeLaneAttempts struct {
	ImageDispatch         int64
	ChatDispatch          int64
	ImageRecoveryVerify   int64
	ChatRecoveryVerify    int64
	ImageRecoveryCooldown int64
	ChatRecoveryCooldown  int64
	ImageDead             int64
	ChatDead              int64
}

type WebProbeLaneStatistics struct {
	Attempts  int64
	Succeeded int64
	Failed    int64
}

type WebProbeStatistics struct {
	Attempts            int64
	Succeeded           int64
	Failed              int64
	DispatchOK          int64
	RecoveryOK          int64
	DeadOK              int64
	CooledDown          int64
	ConsecutiveFailures int64
	Image               WebProbeLaneStatistics
	Chat                WebProbeLaneStatistics
	LaneAttempts        WebProbeLaneAttempts
}

type WebProbeEffectiveConfig struct {
	ProbeUnknownQuota       bool
	LitePerAccountPerDay    int
	ChatPerAccountPerDay    int
	LiteGlobalPerHour       int
	DeadL2MinInterval       time.Duration
	PipelineL1Threshold     float64
	PipelineL0OnlyThreshold float64
}

type WebLanePoolCounts struct {
	Dispatch int64 `json:"dispatch"`
	Recovery int64 `json:"recovery"`
	Dead     int64 `json:"dead"`
}

type WebThreePoolSummary struct {
	Image WebLanePoolCounts `json:"image"`
	Chat  WebLanePoolCounts `json:"chat"`
}

type WebProbeStatus struct {
	Enabled         bool
	Running         bool
	Interval        time.Duration
	IdleInterval    time.Duration
	InitialDelay    time.Duration
	StartedAt       *time.Time
	NextRunAt       *time.Time
	LastCompletedAt *time.Time
	LastError       string
	Current         *WebProbeCurrent
	Statistics      WebProbeStatistics
	Pools           WebThreePoolSummary
	Budget          WebProbeBudgetSnapshot
	Config          WebProbeEffectiveConfig
	Recent          []WebProbeResult
}

type webProbeMonitor struct {
	mu              sync.RWMutex
	enabled         bool
	interval        time.Duration
	idleInterval    time.Duration
	initialDelay    time.Duration
	startedAt       *time.Time
	nextRunAt       *time.Time
	lastCompletedAt *time.Time
	lastError       string
	current         *WebProbeCurrent
	statistics      WebProbeStatistics
	recent          []WebProbeResult
}

func (s *Service) ConfigureWebProbe(interval, idleInterval, initialDelay time.Duration) {
	s.initWebProbe()
	now := s.now()
	s.webProbe.configure(now, interval, idleInterval, initialDelay)
}

func (s *Service) ScheduleWebProbe(next time.Time) {
	s.initWebProbe()
	s.webProbe.schedule(next)
}

func (s *Service) WebProbeStatus(ctx context.Context) (WebProbeStatus, error) {
	s.initWebProbe()
	status := s.webProbe.snapshot()
	status.Budget = s.webProbeBudget.snapshot(s.now())
	status.Config = s.webProbeEffectiveConfig()
	status.Pools = s.summarizeWebThreePoolsFromIndex()
	return status, nil
}

func (s *Service) summarizeWebThreePools(ctx context.Context) (WebThreePoolSummary, error) {
	summary, _, _, _, err := s.summarizeWebPools(ctx)
	return summary, err
}

func (s *Service) summarizeWebPools(ctx context.Context) (WebThreePoolSummary, WebFourPoolsPublic, []uint64, []uint64, error) {
	values, _, err := s.accounts.List(ctx, repository.AccountListQuery{
		Page:   repository.PageQuery{Limit: maxCredentialExportAccounts},
		Filter: repository.AccountListFilter{Provider: string(accountdomain.ProviderWeb), Now: s.now()},
	})
	if err != nil {
		return WebThreePoolSummary{}, WebFourPoolsPublic{}, nil, nil, mapRepositoryError(err)
	}
	ids := make([]uint64, 0, len(values))
	for _, value := range values {
		ids = append(ids, value.ID)
	}
	windowsByAccount, err := s.accounts.GetQuotaWindows(ctx, ids)
	if err != nil {
		return WebThreePoolSummary{}, WebFourPoolsPublic{}, nil, nil, mapRepositoryError(err)
	}
	blocks, err := s.accounts.GetActiveModelQuotaBlocks(ctx, ids, imagineUpstream, s.now())
	if err != nil {
		return WebThreePoolSummary{}, WebFourPoolsPublic{}, nil, nil, mapRepositoryError(err)
	}
	modelStates, err := s.accounts.GetModelStates(ctx, ids)
	if err != nil {
		return WebThreePoolSummary{}, WebFourPoolsPublic{}, nil, nil, mapRepositoryError(err)
	}
	now := s.now()
	result := WebThreePoolSummary{}
	four := WebFourPoolsPublic{}
	imageDispatchIDs := make([]uint64, 0)
	imageSchedulableIDs := make([]uint64, 0)
	for _, value := range values {
		ctxInput := buildWebPoolContext(value, windowsByAccount[value.ID], modelStates[value.ID], blocks[value.ID], now)
		switch WebPoolAt(WebLaneImage, ctxInput, now) {
		case WebPoolDispatch:
			result.Image.Dispatch++
			four.Image.Dispatch++
			imageDispatchIDs = append(imageDispatchIDs, value.ID)
		case WebPoolNormal:
			result.Image.Recovery++
			four.Image.Normal++
		case WebPoolVerification:
			result.Image.Recovery++
			four.Image.Verification++
		case WebPoolDelete:
			result.Image.Dead++
			four.Image.Delete++
		}
		if imagineQuotaFresh(ctxInput.ImagineWindow, now) {
			imageSchedulableIDs = append(imageSchedulableIDs, value.ID)
		}
		switch WebPoolAt(WebLaneChat, ctxInput, now) {
		case WebPoolDispatch:
			result.Chat.Dispatch++
			four.Chat.Dispatch++
		case WebPoolRecovery:
			result.Chat.Recovery++
			four.Chat.Recovery++
		case WebPoolDead:
			result.Chat.Dead++
			four.Chat.Dead++
		}
	}
	imageDispatchIDs = filterImageDispatchIDsWithGenerations(imageDispatchIDs, windowsByAccount, now)
	result.Image.Dispatch = int64(len(imageDispatchIDs))
	four.Image.Dispatch = len(imageDispatchIDs)
	sort.Slice(imageDispatchIDs, func(i, j int) bool { return imageDispatchIDs[i] < imageDispatchIDs[j] })
	sort.Slice(imageSchedulableIDs, func(i, j int) bool { return imageSchedulableIDs[i] < imageSchedulableIDs[j] })
	return result, four, imageDispatchIDs, imageSchedulableIDs, nil
}

func (s *Service) observeWebProbe(ctx context.Context, candidate accountdomain.Credential, lane WebLane, mode WebProbeMode, run func() (uint64, bool, error)) (uint64, bool, error) {
	startedAt := s.now()
	s.webProbe.start(candidate, lane, mode, startedAt)
	accountID, found, err := run()
	completedAt := s.now()
	updated := candidate
	if value, getErr := s.accounts.Get(ctx, candidate.ID); getErr == nil {
		updated = value
	}
	s.webProbe.finish(updated, lane, mode, startedAt, completedAt, err)
	return accountID, found, err
}

func (m *webProbeMonitor) configure(now time.Time, interval, idleInterval, initialDelay time.Duration) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.enabled = interval > 0
	m.interval = interval
	m.idleInterval = idleInterval
	m.initialDelay = initialDelay
	if !m.enabled {
		m.nextRunAt = nil
		return
	}
	if m.startedAt == nil {
		started := now
		m.startedAt = &started
	}
	next := now.Add(initialDelay)
	m.nextRunAt = &next
}

func (m *webProbeMonitor) schedule(next time.Time) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if !m.enabled {
		return
	}
	value := next
	m.nextRunAt = &value
}

func (m *webProbeMonitor) start(candidate accountdomain.Credential, lane WebLane, mode WebProbeMode, startedAt time.Time) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.nextRunAt = nil
	m.current = &WebProbeCurrent{AccountID: candidate.ID, AccountName: candidate.Name, Lane: lane, Mode: mode, StartedAt: startedAt}
}

func (m *webProbeMonitor) finish(candidate accountdomain.Credential, lane WebLane, mode WebProbeMode, startedAt, completedAt time.Time, probeErr error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	pool := WebPoolAt(lane, buildWebPoolContext(candidate, nil, nil, false, completedAt), completedAt)
	outcome := webProbeOutcome(mode, probeErr)
	errorMessage := ""
	if probeErr != nil {
		errorMessage = strings.TrimSpace(probeErr.Error())
		if len(errorMessage) > 512 {
			errorMessage = errorMessage[:512]
		}
	}
	m.statistics.Attempts++
	recordWebLaneAttempt(&m.statistics.LaneAttempts, lane, mode)
	laneStats := &m.statistics.Image
	if lane == WebLaneChat {
		laneStats = &m.statistics.Chat
	}
	laneStats.Attempts++
	if probeErr == nil {
		m.statistics.Succeeded++
		laneStats.Succeeded++
		m.statistics.ConsecutiveFailures = 0
		switch mode {
		case WebProbeModeDispatch:
			m.statistics.DispatchOK++
		case WebProbeModeDead:
			m.statistics.DeadOK++
		default:
			m.statistics.RecoveryOK++
		}
	} else {
		m.statistics.Failed++
		m.statistics.ConsecutiveFailures++
		if outcome != WebProbeOutcomeCooldown {
			laneStats.Failed++
		}
		if outcome == WebProbeOutcomeCooldown {
			m.statistics.CooledDown++
		}
	}
	result := WebProbeResult{
		AccountID: candidate.ID, AccountName: candidate.Name, Lane: lane, Mode: mode, Outcome: outcome, Pool: pool,
		Error: errorMessage, StartedAt: startedAt, CompletedAt: completedAt, Duration: completedAt.Sub(startedAt),
	}
	m.recent = append([]WebProbeResult{result}, m.recent...)
	if len(m.recent) > maxRecentWebProbeResults {
		m.recent = m.recent[:maxRecentWebProbeResults]
	}
	m.current = nil
	completed := completedAt
	m.lastCompletedAt = &completed
	m.lastError = errorMessage
}

func recordWebLaneAttempt(stats *WebProbeLaneAttempts, lane WebLane, mode WebProbeMode) {
	switch lane {
	case WebLaneImage:
		switch mode {
		case WebProbeModeDispatch:
			stats.ImageDispatch++
		case WebProbeModeRecoveryVerify:
			stats.ImageRecoveryVerify++
		case WebProbeModeRecoveryCooldown:
			stats.ImageRecoveryCooldown++
		case WebProbeModeDead:
			stats.ImageDead++
		}
	case WebLaneChat:
		switch mode {
		case WebProbeModeDispatch:
			stats.ChatDispatch++
		case WebProbeModeRecoveryVerify:
			stats.ChatRecoveryVerify++
		case WebProbeModeRecoveryCooldown:
			stats.ChatRecoveryCooldown++
		case WebProbeModeDead:
			stats.ChatDead++
		}
	}
}

func webProbeOutcome(mode WebProbeMode, probeErr error) WebProbeOutcome {
	if probeErr == nil {
		switch mode {
		case WebProbeModeDispatch:
			return WebProbeOutcomeDispatchOK
		case WebProbeModeDead:
			return WebProbeOutcomeDeadOK
		default:
			return WebProbeOutcomeRecoveryOK
		}
	}
	if mode == WebProbeModeRecoveryCooldown {
		return WebProbeOutcomeCooldown
	}
	return WebProbeOutcomeFailed
}

func (m *webProbeMonitor) snapshot() WebProbeStatus {
	m.mu.RLock()
	defer m.mu.RUnlock()
	result := WebProbeStatus{
		Enabled: m.enabled, Running: m.current != nil,
		Interval: m.interval, IdleInterval: m.idleInterval,
		InitialDelay: m.initialDelay, StartedAt: cloneTime(m.startedAt), NextRunAt: cloneTime(m.nextRunAt),
		LastCompletedAt: cloneTime(m.lastCompletedAt), LastError: m.lastError, Statistics: m.statistics,
		Recent: append([]WebProbeResult(nil), m.recent...),
	}
	if m.current != nil {
		current := *m.current
		result.Current = &current
	}
	return result
}
