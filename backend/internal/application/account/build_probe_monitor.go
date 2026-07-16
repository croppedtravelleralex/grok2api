package account

import (
	"context"
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
	BuildProbeModeRecovery     BuildProbeMode = "recovery"

	BuildProbeOutcomeVerified   BuildProbeOutcome = "verified"
	BuildProbeOutcomeRecovered  BuildProbeOutcome = "recovered"
	BuildProbeOutcomeCooldown   BuildProbeOutcome = "cooldown"
	BuildProbeOutcomeQuarantine BuildProbeOutcome = "quarantine"
	BuildProbeOutcomeRecovery   BuildProbeOutcome = "recovery"
	BuildProbeOutcomeRetired    BuildProbeOutcome = "retired"
	BuildProbeOutcomeFailed     BuildProbeOutcome = "failed"
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
	Recovered           int64
	CooledDown          int64
	Quarantined         int64
	RecoveryQueued      int64
	Retired             int64
	ConsecutiveFailures int64
}

type BuildProbePoolSummary struct {
	Production   int64
	Verification int64
	Cooldown     int64
	Quarantine   int64
	Recovery     int64
	Retired      int64
	Disabled     int64
}

type BuildProbeStatus struct {
	Enabled         bool
	Running         bool
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
	status.Pools = summarizeBuildProbePools(values, s.now())
	return status, nil
}

func summarizeBuildProbePools(values []accountdomain.Credential, now time.Time) BuildProbePoolSummary {
	result := BuildProbePoolSummary{}
	for _, value := range values {
		switch AccountPoolAt(value, now) {
		case "production":
			result.Production++
		case "verification":
			result.Verification++
		case "cooldown":
			result.Cooldown++
		case "quarantine":
			result.Quarantine++
		case "recovery":
			result.Recovery++
		case "retired":
			result.Retired++
		case "disabled":
			result.Disabled++
		}
	}
	return result
}

func AccountPoolAt(value accountdomain.Credential, now time.Time) string {
	if !value.Enabled {
		if strings.HasPrefix(strings.ToLower(strings.TrimSpace(value.LastError)), "retired:") {
			return "retired"
		}
		return "disabled"
	}
	if value.AuthStatus == accountdomain.AuthStatusReauthRequired {
		if value.CooldownUntil != nil && value.CooldownUntil.After(now) {
			return "recovery"
		}
		return "quarantine"
	}
	if value.CooldownUntil != nil && value.CooldownUntil.After(now) {
		return "cooldown"
	}
	if value.Provider == accountdomain.ProviderBuild && strings.TrimSpace(value.ObservedModel) == "" {
		return "verification"
	}
	return "production"
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
	pool := AccountPoolAt(candidate, completedAt)
	outcome := buildProbeOutcome(mode, pool, probeErr)
	errorMessage := ""
	if probeErr != nil {
		errorMessage = strings.TrimSpace(probeErr.Error())
		if len(errorMessage) > 512 {
			errorMessage = errorMessage[:512]
		}
	}
	m.statistics.Attempts++
	if probeErr == nil {
		m.statistics.Succeeded++
		m.statistics.ConsecutiveFailures = 0
		if mode == BuildProbeModeRecovery {
			m.statistics.Recovered++
		} else {
			m.statistics.Verified++
		}
	} else {
		m.statistics.Failed++
		m.statistics.ConsecutiveFailures++
		switch outcome {
		case BuildProbeOutcomeCooldown:
			m.statistics.CooledDown++
		case BuildProbeOutcomeQuarantine:
			m.statistics.Quarantined++
		case BuildProbeOutcomeRecovery:
			m.statistics.RecoveryQueued++
		case BuildProbeOutcomeRetired:
			m.statistics.Retired++
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
	if probeErr == nil {
		if mode == BuildProbeModeRecovery {
			return BuildProbeOutcomeRecovered
		}
		return BuildProbeOutcomeVerified
	}
	switch pool {
	case "cooldown":
		return BuildProbeOutcomeCooldown
	case "quarantine":
		return BuildProbeOutcomeQuarantine
	case "recovery":
		return BuildProbeOutcomeRecovery
	case "retired":
		return BuildProbeOutcomeRetired
	default:
		return BuildProbeOutcomeFailed
	}
}

func (m *buildProbeMonitor) snapshot() BuildProbeStatus {
	m.mu.RLock()
	defer m.mu.RUnlock()
	result := BuildProbeStatus{
		Enabled: m.enabled, Running: m.current != nil, Interval: m.interval, IdleInterval: m.idleInterval,
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
