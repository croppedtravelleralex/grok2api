package account

import (
	"context"
	"errors"
	"strings"
	"sync"
	"time"

	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
	"github.com/chenyme/grok2api/backend/internal/repository"
)

const maxRecentBuildProbeResults = 20

type BuildProbeMode string
type BuildProbeOutcome string

const (
	BuildProbeModeVerification BuildProbeMode = "verification"
	BuildProbeModeNormal       BuildProbeMode = "normal"
	BuildProbeModeDelete       BuildProbeMode = "delete"
	BuildProbeModeDispatch     BuildProbeMode = "dispatch"

	BuildProbeOutcomeVerified  BuildProbeOutcome = "verified"
	BuildProbeOutcomeNormalOK  BuildProbeOutcome = "normalOk"
	BuildProbeOutcomeDispatch  BuildProbeOutcome = "dispatchOk"
	BuildProbeOutcomeCooldown   BuildProbeOutcome = "cooldown"
	BuildProbeOutcomeFailed    BuildProbeOutcome = "failed"
	BuildProbeOutcomeDeletable BuildProbeOutcome = "deletable"
	BuildProbeOutcomeDeleted   BuildProbeOutcome = "deleted"
)

type BuildProbeCurrent struct {
	AccountID   uint64
	AccountName string
	Mode        BuildProbeMode
	StartedAt   time.Time
}

type BuildProbeResult struct {
	AccountID   uint64
	AccountName string
	Mode        BuildProbeMode
	Outcome     BuildProbeOutcome
	Pool        string
	Error       string
	StartedAt   time.Time
	CompletedAt time.Time
	Duration    time.Duration
}

type BuildProbeStatistics struct {
	Attempts            int64
	Succeeded           int64
	Failed              int64
	Verified            int64
	NormalOK            int64
	DispatchOK          int64
	CooledDown          int64
	Deletable           int64
	Deleted             int64
	ConsecutiveFailures int64
	LaneAttempts        BuildProbeLaneAttempts
}

type BuildProbeLaneAttempts struct {
	Verification int64
	Normal       int64
	Delete       int64
	Dispatch     int64
}

type BuildProbePoolSummary struct {
	Dispatch     int64
	Normal       int64
	Verification int64
	Delete       int64
}

type BuildProbeStatus struct {
	Enabled         bool
	Running         bool
	PurgeApply      bool
	Interval        time.Duration
	IdleInterval    time.Duration
	InitialDelay    time.Duration
	StartedAt       *time.Time
	NextRunAt       *time.Time
	LastCompletedAt *time.Time
	LastError       string
	Current         *BuildProbeCurrent
	Statistics      BuildProbeStatistics
	Pools           BuildProbePoolSummary
	Recent          []BuildProbeResult
}

type buildProbeMonitor struct {
	mu              sync.RWMutex
	enabled         bool
	purgeApply      bool
	interval        time.Duration
	idleInterval    time.Duration
	initialDelay    time.Duration
	startedAt       *time.Time
	nextRunAt       *time.Time
	lastCompletedAt *time.Time
	lastError       string
	current         *BuildProbeCurrent
	statistics      BuildProbeStatistics
	recent          []BuildProbeResult
}

func (s *Service) ConfigureBuildProbe(interval, idleInterval, initialDelay time.Duration) {
	now := s.now()
	s.buildProbe.configure(now, interval, idleInterval, initialDelay)
}

func (s *Service) ConfigureBuildProbePurgeApply(enabled bool) {
	s.buildProbe.setPurgeApply(enabled)
}

func (s *Service) SetBuildProbePurgeApply(enabled bool) {
	s.buildProbe.setPurgeApply(enabled)
}

func (s *Service) ScheduleBuildProbe(next time.Time) {
	s.buildProbe.schedule(next)
}

func (s *Service) BuildProbeStatus(ctx context.Context) (BuildProbeStatus, error) {
	status := s.buildProbe.snapshot()
	values, _, err := s.accounts.List(ctx, repository.AccountListQuery{
		Page: repository.PageQuery{Limit: maxCredentialExportAccounts},
		Filter: repository.AccountListFilter{
			Provider: string(accountdomain.ProviderBuild),
			Now:      s.now(),
		},
	})
	if err != nil {
		return status, mapRepositoryError(err)
	}
	ids := make([]uint64, 0, len(values))
	for _, value := range values {
		ids = append(ids, value.ID)
	}
	recoveries, err := s.accounts.GetQuotaRecoveries(ctx, ids)
	if err != nil {
		return status, mapRepositoryError(err)
	}
	status.Pools = summarizeBuildProbePools(values, recoveries, s.now())
	return status, nil
}

func summarizeBuildProbePools(values []accountdomain.Credential, recoveries map[uint64]accountdomain.QuotaRecovery, now time.Time) BuildProbePoolSummary {
	result := BuildProbePoolSummary{}
	for _, value := range values {
		var recovery *accountdomain.QuotaRecovery
		if item, ok := recoveries[value.ID]; ok {
			copy := item
			recovery = &copy
		}
		switch AccountPoolAt(value, now, recovery) {
		case PoolDispatch:
			result.Dispatch++
		case PoolNormal:
			result.Normal++
		case PoolVerification:
			result.Verification++
		case PoolDelete:
			result.Delete++
		}
	}
	return result
}

func (s *Service) observeBuildProbe(ctx context.Context, candidate accountdomain.Credential, mode BuildProbeMode, run func() (uint64, bool, error)) (uint64, bool, error) {
	startedAt := s.now()
	s.buildProbe.start(candidate, mode, startedAt)
	accountID, found, err := run()
	completedAt := s.now()
	updated := candidate
	if value, getErr := s.accounts.Get(ctx, candidate.ID); getErr == nil {
		updated = value
	}
	s.buildProbe.finish(updated, mode, startedAt, completedAt, err)
	if mode == BuildProbeModeDelete && (errors.Is(err, errPurgeDeletable) || errors.Is(err, errPurgeDeleted)) {
		return accountID, found, nil
	}
	if errors.Is(err, errPurgeDeletable) || errors.Is(err, errPurgeDeleted) {
		return accountID, found, nil
	}
	return accountID, found, err
}

func (m *buildProbeMonitor) configure(now time.Time, interval, idleInterval, initialDelay time.Duration) {
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

func (m *buildProbeMonitor) setPurgeApply(enabled bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.purgeApply = enabled
}

func (m *buildProbeMonitor) purgeApplyEnabled() bool {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.purgeApply
}

func (m *buildProbeMonitor) schedule(next time.Time) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if !m.enabled {
		return
	}
	value := next
	m.nextRunAt = &value
}

func (m *buildProbeMonitor) start(candidate accountdomain.Credential, mode BuildProbeMode, startedAt time.Time) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.nextRunAt = nil
	m.current = &BuildProbeCurrent{AccountID: candidate.ID, AccountName: candidate.Name, Mode: mode, StartedAt: startedAt}
}

func (m *buildProbeMonitor) finish(candidate accountdomain.Credential, mode BuildProbeMode, startedAt, completedAt time.Time, probeErr error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	pool := AccountPoolAt(candidate, completedAt, nil)
	if errors.Is(probeErr, errPurgeDeleted) {
		pool = PoolDelete
	}
	outcome := buildProbeOutcome(mode, pool, probeErr)
	errorMessage := ""
	if probeErr != nil {
		errorMessage = strings.TrimSpace(probeErr.Error())
		if len(errorMessage) > 512 {
			errorMessage = errorMessage[:512]
		}
	}
	m.statistics.Attempts++
	switch mode {
	case BuildProbeModeVerification:
		m.statistics.LaneAttempts.Verification++
	case BuildProbeModeNormal:
		m.statistics.LaneAttempts.Normal++
	case BuildProbeModeDelete:
		m.statistics.LaneAttempts.Delete++
	case BuildProbeModeDispatch:
		m.statistics.LaneAttempts.Dispatch++
	}
	switch {
	case errors.Is(probeErr, errPurgeDeleted):
		m.statistics.Failed++
		m.statistics.Deleted++
		m.statistics.ConsecutiveFailures++
	case errors.Is(probeErr, errPurgeDeletable) || outcome == BuildProbeOutcomeDeletable:
		m.statistics.Failed++
		m.statistics.Deletable++
		m.statistics.ConsecutiveFailures++
	case probeErr == nil:
		m.statistics.Succeeded++
		m.statistics.ConsecutiveFailures = 0
		switch mode {
		case BuildProbeModeVerification:
			m.statistics.Verified++
		case BuildProbeModeNormal:
			m.statistics.NormalOK++
		case BuildProbeModeDispatch:
			m.statistics.DispatchOK++
		}
	default:
		m.statistics.Failed++
		m.statistics.ConsecutiveFailures++
		if outcome == BuildProbeOutcomeCooldown {
			m.statistics.CooledDown++
		}
	}
	result := BuildProbeResult{
		AccountID: candidate.ID, AccountName: candidate.Name, Mode: mode, Outcome: outcome, Pool: pool,
		Error: errorMessage, StartedAt: startedAt, CompletedAt: completedAt, Duration: completedAt.Sub(startedAt),
	}
	m.recent = append([]BuildProbeResult{result}, m.recent...)
	if len(m.recent) > maxRecentBuildProbeResults {
		m.recent = m.recent[:maxRecentBuildProbeResults]
	}
	m.current = nil
	completed := completedAt
	m.lastCompletedAt = &completed
	m.lastError = errorMessage
}

func buildProbeOutcome(mode BuildProbeMode, pool string, probeErr error) BuildProbeOutcome {
	if errors.Is(probeErr, errPurgeDeleted) {
		return BuildProbeOutcomeDeleted
	}
	if errors.Is(probeErr, errPurgeDeletable) || pool == PoolDelete {
		if probeErr != nil {
			return BuildProbeOutcomeDeletable
		}
	}
	if probeErr == nil {
		switch mode {
		case BuildProbeModeNormal:
			return BuildProbeOutcomeNormalOK
		case BuildProbeModeDispatch:
			return BuildProbeOutcomeDispatch
		default:
			return BuildProbeOutcomeVerified
		}
	}
	if pool == PoolNormal {
		return BuildProbeOutcomeCooldown
	}
	return BuildProbeOutcomeFailed
}

func (m *buildProbeMonitor) snapshot() BuildProbeStatus {
	m.mu.RLock()
	defer m.mu.RUnlock()
	result := BuildProbeStatus{
		Enabled: m.enabled, Running: m.current != nil, PurgeApply: m.purgeApply,
		Interval: m.interval, IdleInterval: m.idleInterval,
		InitialDelay: m.initialDelay, StartedAt: cloneTime(m.startedAt), NextRunAt: cloneTime(m.nextRunAt),
		LastCompletedAt: cloneTime(m.lastCompletedAt), LastError: m.lastError, Statistics: m.statistics,
		Recent: append([]BuildProbeResult(nil), m.recent...),
	}
	if m.current != nil {
		current := *m.current
		result.Current = &current
	}
	return result
}

func cloneTime(value *time.Time) *time.Time {
	if value == nil {
		return nil
	}
	copy := *value
	return &copy
}
