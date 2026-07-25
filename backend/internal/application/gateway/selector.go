package gateway

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"

	accountapp "github.com/chenyme/grok2api/backend/internal/application/account"
	"github.com/chenyme/grok2api/backend/internal/domain/account"
	"github.com/chenyme/grok2api/backend/internal/repository"
	"golang.org/x/sync/singleflight"
)

type accountLease struct {
	Credential     account.Credential
	Billing        *account.Billing
	QuotaProbe     bool
	QuotaProbeKind account.QuotaRecoveryKind
	QuotaMode      string
	release        func()
}

const quotaProbeLease = 5 * time.Minute
const successPersistInterval = 30 * time.Second
const candidateCacheTTL = time.Second
const buildDispatchHydrateInitial = 64
const buildDispatchHydrateMax = 256
const buildNormalProbeHydrateLimit = 32
const modelOutcomeSuccessTTL = 30 * time.Minute
const modelSoftStopBaseCooldown = 30 * time.Second
const modelSoftStopMaxCooldown = 5 * time.Minute
const modelOutcomeRetention = time.Hour
const webLiteImageUpstreamModel = "grok-imagine-image"
const defaultExplorationEpsilon = 0.05
const imagineQuotaFreshTTL = 30 * time.Minute

type candidateSnapshot struct {
	values    []account.RoutingCandidate
	expiresAt time.Time
}

type candidateCacheKey struct {
	provider      account.Provider
	upstreamModel string
	quotaMode     string
}

type modelOutcomeKey struct {
	accountID     uint64
	upstreamModel string
}

type modelOutcome struct {
	lastSuccessAt       time.Time
	lastSoftStopAt      time.Time
	softStopUntil       time.Time
	consecutiveSoftStop int
}

type SelectionUnavailableReason string

const (
	SelectionNoAccounts       SelectionUnavailableReason = "no_accounts"
	SelectionNoDispatchIndex  SelectionUnavailableReason = "no_dispatch_index"
	SelectionPinFiltered      SelectionUnavailableReason = "pin_filtered"
	SelectionUnsupportedModel SelectionUnavailableReason = "unsupported_model"
	SelectionCooling          SelectionUnavailableReason = "cooling"
	SelectionModelCooling     SelectionUnavailableReason = "model_cooling"
	SelectionQuotaExhausted   SelectionUnavailableReason = "quota_exhausted"
	SelectionQuotaStale       SelectionUnavailableReason = "quota_stale"
	SelectionSaturated        SelectionUnavailableReason = "saturated"
	SelectionNoChromeTickets  SelectionUnavailableReason = "no_chrome_tickets"
)

// SelectionUnavailableError 保留选号失败的真实原因，避免所有情况都退化成模糊的 503。
type SelectionUnavailableError struct {
	Reason     SelectionUnavailableReason
	RetryAfter time.Duration
	// SelectionReason 对外诊断字段，与 Reason 对齐（Admin/internal）。
	SelectionReason string
}

func (e *SelectionUnavailableError) Error() string {
	if e == nil {
		return "没有可用上游账号"
	}
	switch e.Reason {
	case SelectionUnsupportedModel:
		return "当前账号池不支持该模型"
	case SelectionCooling:
		return "可用上游账号正在冷却"
	case SelectionModelCooling:
		return "可用上游账号的目标模型正在冷却"
	case SelectionQuotaExhausted:
		return "可用上游账号额度等待恢复"
	case SelectionQuotaStale:
		return "可用上游账号 Imagine 额度未同步或已过期"
	case SelectionSaturated:
		return "可用上游账号均达到并发上限"
	case SelectionNoChromeTickets:
		return "Chrome 票池暂无可用票据"
	default:
		return "没有可用上游账号"
	}
}

func (l *accountLease) Release() {
	if l != nil && l.release != nil {
		l.release()
		l.release = nil
	}
}

// buildDispatchSource 提供 Build 调度池有序索引，避免 Acquire 全表线性扫。
type buildDispatchSource interface {
	OrderedDispatchIDs(limit int) []uint64
	DueNormalProbeIDs(now time.Time, limit int) []uint64
	NoteDispatchSelected(id uint64, at time.Time)
	EnsurePoolIndexWarm(ctx context.Context)
}

// webDispatchSource 提供 Web 双轨调度池有序索引。
type webDispatchSource interface {
	OrderedWebDispatchIDs(lane accountapp.WebLane, limit int) []uint64
	NoteWebDispatchSelected(lane accountapp.WebLane, id uint64, at time.Time)
	EnsureWebPoolIndexWarm(ctx context.Context)
}

// ChromeTicketSource 提供各账号可用 Chrome 票数量（选号偏好有票）。
type ChromeTicketSource interface {
	AvailableCounts(ctx context.Context) map[uint64]int64
}

// Selector 实现可替换的 balanced 账号选择策略。
type Selector struct {
	accounts       repository.AccountRepository
	concurrency    repository.ConcurrencyLimiter
	sticky         repository.StickySessionRepository
	stickyTTL      time.Duration
	cooldownBase   time.Duration
	cooldownMax    time.Duration
	capacityWait   time.Duration
	mu             sync.Mutex
	leaseWakeMu    sync.Mutex
	leaseWake      chan struct{}
	lastSelectedAt map[uint64]time.Time
	lastSuccessAt  map[uint64]time.Time
	modelOutcomes  map[modelOutcomeKey]modelOutcome
	candidates     map[candidateCacheKey]candidateSnapshot
	candidateLoads singleflight.Group
	buildDispatch  buildDispatchSource
	webDispatch    webDispatchSource
	chromeTickets  ChromeTicketSource
	tierOrders     interface {
		TierOrder(account.Provider, string) []account.WebTier
	}
	explorationEpsilon float64
	randFloat          func() float64
}

// SetBuildDispatchSource 接入 Build 四池调度索引。
func (s *Selector) SetBuildDispatchSource(source buildDispatchSource) {
	s.mu.Lock()
	s.buildDispatch = source
	s.mu.Unlock()
}

// SetWebDispatchSource 接入 Web 双轨三池调度索引。
func (s *Selector) SetChromeTicketSource(source ChromeTicketSource) {
	s.mu.Lock()
	s.chromeTickets = source
	s.mu.Unlock()
}

func (s *Selector) SetWebDispatchSource(source webDispatchSource) {
	s.mu.Lock()
	s.webDispatch = source
	s.mu.Unlock()
}

func NewSelector(accounts repository.AccountRepository, concurrency repository.ConcurrencyLimiter, sticky repository.StickySessionRepository, tierOrders interface {
	TierOrder(account.Provider, string) []account.WebTier
}, stickyTTL, cooldownBase, cooldownMax time.Duration, capacityWait ...time.Duration) *Selector {
	wait := time.Duration(0)
	if len(capacityWait) > 0 && capacityWait[0] > 0 {
		wait = capacityWait[0]
	}
	return &Selector{
		accounts: accounts, concurrency: concurrency, sticky: sticky, tierOrders: tierOrders,
		stickyTTL: stickyTTL, cooldownBase: cooldownBase, cooldownMax: cooldownMax, capacityWait: wait,
		leaseWake: make(chan struct{}), lastSelectedAt: make(map[uint64]time.Time),
		lastSuccessAt: make(map[uint64]time.Time), modelOutcomes: make(map[modelOutcomeKey]modelOutcome),
		candidates: make(map[candidateCacheKey]candidateSnapshot), explorationEpsilon: defaultExplorationEpsilon,
	}
}

func (s *Selector) UpdateConfig(stickyTTL, cooldownBase, cooldownMax time.Duration, capacityWait ...time.Duration) {
	s.mu.Lock()
	s.stickyTTL = stickyTTL
	s.cooldownBase = cooldownBase
	s.cooldownMax = cooldownMax
	if len(capacityWait) > 0 {
		s.capacityWait = max(time.Duration(0), capacityWait[0])
	}
	s.mu.Unlock()
}

func (s *Selector) routingConfig() (time.Duration, time.Duration, time.Duration, time.Duration) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.stickyTTL, s.cooldownBase, s.cooldownMax, s.capacityWait
}

func (s *Selector) Acquire(ctx context.Context, provider account.Provider, upstreamModel, quotaMode, promptCacheKey string, excluded map[uint64]bool, allowQuotaProbe bool) (*accountLease, error) {
	now := time.Now().UTC()
	stickyKey := promptCacheStickyKey(promptCacheKey)
	values, err := s.loadCandidatesForAcquire(ctx, provider, upstreamModel, quotaMode, now)
	if err != nil {
		return nil, err
	}
	normalCandidates := make([]account.RoutingCandidate, 0, len(values))
	probeCandidates := make([]account.RoutingCandidate, 0, len(values))
	supportedCandidates := 0
	consideredCandidates := 0
	coolingCandidates := 0
	modelCoolingCandidates := 0
	quotaCandidates := 0
	var earliestRetry time.Time
	for _, candidate := range values {
		value := candidate.Credential
		if excluded[value.ID] || value.AuthStatus != account.AuthStatusActive {
			continue
		}
		// Build：未通过能力探测的号不进真实流量（验证池）。
		if provider == account.ProviderBuild && strings.TrimSpace(value.ObservedModel) == "" {
			continue
		}
		consideredCandidates++
		if candidate.ModelCapabilityKnown && !candidate.SupportsModel {
			continue
		}
		supportedCandidates++
		if candidateModelQuotaBlocked(candidate, now) {
			modelCoolingCandidates++
			earliestRetry = earlierFuture(earliestRetry, candidate.ModelQuotaBlock.CooldownUntil, now)
			continue
		}
		if value.CooldownUntil != nil && now.Before(*value.CooldownUntil) {
			coolingCandidates++
			earliestRetry = earlierFuture(earliestRetry, *value.CooldownUntil, now)
			continue
		}
		quotaRecovery := candidate.QuotaRecovery
		if quotaRecovery != nil && quotaRecovery.Status != account.QuotaRecoveryStatusActive {
			if allowQuotaProbe && quotaRecovery.NextProbeAt != nil && !now.Before(*quotaRecovery.NextProbeAt) {
				probeCandidates = append(probeCandidates, candidate)
			} else {
				quotaCandidates++
				if quotaRecovery.NextProbeAt != nil {
					earliestRetry = earlierFuture(earliestRetry, *quotaRecovery.NextProbeAt, now)
				}
			}
			continue
		}
		if candidate.Billing != nil && candidate.Billing.IsExhausted(value.MinimumRemaining) {
			quotaCandidates++
			continue
		}
		// total=0/remaining=0 是 free-usage-gates 对部分已成功生图账号的真实返回，
		// 表示该免费闸门不适用或上限未知，不能误判为模型额度耗尽。
		if candidate.QuotaWindow != nil && candidate.QuotaWindow.Total > 0 && candidate.QuotaWindow.Remaining <= 0 {
			quotaCandidates++
			if candidate.QuotaWindow.ResetAt != nil {
				earliestRetry = earlierFuture(earliestRetry, *candidate.QuotaWindow.ResetAt, now)
			}
			continue
		}
		if requiresImagineQuotaAdmission(upstreamModel, quotaMode) && !candidateImagineQuotaAdmissible(candidate, now) {
			quotaCandidates++
			if candidate.QuotaWindow != nil && candidate.QuotaWindow.SyncedAt != nil {
				retryAt := candidate.QuotaWindow.SyncedAt.Add(imagineQuotaFreshTTL)
				earliestRetry = earlierFuture(earliestRetry, retryAt, now)
			}
			continue
		}
		normalCandidates = append(normalCandidates, candidate)
	}
	if requiresChromeTicketAdmission(upstreamModel) {
		var ticketErr error
		normalCandidates, ticketErr = s.filterChromeTicketCandidates(ctx, normalCandidates)
		if ticketErr != nil {
			return nil, ticketErr
		}
	}
	if len(normalCandidates) == 0 && len(probeCandidates) == 0 {
		reason := SelectionNoAccounts
		switch {
		case consideredCandidates > 0 && supportedCandidates == 0:
			reason = SelectionUnsupportedModel
		case modelCoolingCandidates > 0:
			reason = SelectionModelCooling
		case coolingCandidates > 0:
			reason = SelectionCooling
		case quotaCandidates > 0:
			reason = SelectionQuotaExhausted
		case requiresChromeTicketAdmission(upstreamModel) && s.chromeTicketsEnforced():
			reason = SelectionNoChromeTickets
		}
		return nil, &SelectionUnavailableError{Reason: reason, RetryAfter: retryDelay(now, earliestRetry)}
	}
	if len(probeCandidates) > 0 {
		if err := s.sortCandidates(ctx, probeCandidates, now, s.resolveTierOrder(provider, upstreamModel), upstreamModel); err != nil {
			return nil, err
		}
		s.maybeExploreShuffle(probeCandidates)
		for _, candidate := range probeCandidates {
			lease, err := s.claimAccountSlot(ctx, candidate.Credential, upstreamModel)
			if err != nil {
				return nil, err
			}
			if lease == nil {
				continue
			}
			claimed, err := s.accounts.ClaimQuotaProbe(ctx, candidate.Credential.ID, now, now.Add(quotaProbeLease))
			if err != nil || !claimed {
				lease.Release()
				if err != nil {
					return nil, err
				}
				continue
			}
			lease.QuotaProbe = true
			lease.QuotaProbeKind = candidate.QuotaRecovery.Kind
			lease.Billing = candidate.Billing
			return lease, nil
		}
	}
	if stickyKey != "" {
		stickyID, ok, err := s.sticky.Get(ctx, stickyKey, now)
		if err != nil {
			return nil, fmt.Errorf("读取会话粘滞状态: %w", err)
		}
		if ok {
			for _, candidate := range normalCandidates {
				if candidate.Credential.ID == stickyID {
					lease, acquireErr := s.claimAccountSlot(ctx, candidate.Credential, upstreamModel)
					if acquireErr != nil {
						return nil, acquireErr
					}
					if lease != nil {
						if provider == account.ProviderBuild {
							s.mu.Lock()
							source := s.buildDispatch
							s.mu.Unlock()
							if source != nil {
								source.NoteDispatchSelected(candidate.Credential.ID, now)
							}
						}
						if provider == account.ProviderWeb {
							s.mu.Lock()
							source := s.webDispatch
							s.mu.Unlock()
							if source != nil {
								source.NoteWebDispatchSelected(accountapp.ResolveWebAcquireLane(upstreamModel, quotaMode), candidate.Credential.ID, now)
							}
						}
						lease.Billing = candidate.Billing
						lease.QuotaMode = effectiveQuotaMode(candidate, quotaMode)
						return lease, nil
					}
				}
			}
		}
	}
	_, _, _, capacityWait := s.routingConfig()
	waitDeadline := time.Now().Add(capacityWait)
	for {
		currentTime := time.Now().UTC()
		if provider == account.ProviderBuild {
			normalCandidates = s.orderBuildDispatchCandidates(normalCandidates)
			s.maybeExploreShuffle(normalCandidates)
		} else if err := s.sortCandidates(ctx, normalCandidates, currentTime, s.resolveTierOrder(provider, upstreamModel), upstreamModel); err != nil {
			return nil, err
		} else {
			s.maybeExploreShuffle(normalCandidates)
		}
		for _, candidate := range normalCandidates {
			lease, err := s.claimAccountSlot(ctx, candidate.Credential, upstreamModel)
			if err != nil {
				return nil, err
			}
			if lease == nil {
				continue
			}
			if stickyKey != "" {
				stickyTTL, _, _, _ := s.routingConfig()
				if err := s.sticky.Set(ctx, stickyKey, candidate.Credential.ID, currentTime.Add(stickyTTL)); err != nil {
					lease.Release()
					return nil, fmt.Errorf("写入会话粘滞状态: %w", err)
				}
			}
			if provider == account.ProviderBuild {
				s.mu.Lock()
				source := s.buildDispatch
				s.mu.Unlock()
				if source != nil {
					source.NoteDispatchSelected(candidate.Credential.ID, currentTime)
				}
			}
			if provider == account.ProviderWeb {
				s.mu.Lock()
				source := s.webDispatch
				s.mu.Unlock()
				if source != nil {
					source.NoteWebDispatchSelected(accountapp.ResolveWebAcquireLane(upstreamModel, quotaMode), candidate.Credential.ID, currentTime)
				}
			}
			lease.Billing = candidate.Billing
			lease.QuotaMode = effectiveQuotaMode(candidate, quotaMode)
			return lease, nil
		}
		if capacityWait <= 0 {
			return nil, &SelectionUnavailableError{Reason: SelectionSaturated, RetryAfter: time.Second}
		}
		retry, err := s.awaitLeaseRetry(ctx, waitDeadline)
		if err != nil {
			return nil, err
		}
		if !retry {
			return nil, &SelectionUnavailableError{Reason: SelectionSaturated, RetryAfter: time.Second}
		}
	}
}

func (s *Selector) orderBuildDispatchCandidates(values []account.RoutingCandidate) []account.RoutingCandidate {
	if len(values) <= 1 {
		return values
	}
	s.mu.Lock()
	source := s.buildDispatch
	s.mu.Unlock()
	if source == nil {
		return values
	}
	orderedIDs := source.OrderedDispatchIDs(len(values) + 8)
	if len(orderedIDs) == 0 {
		return values
	}
	byID := make(map[uint64]account.RoutingCandidate, len(values))
	for _, candidate := range values {
		byID[candidate.Credential.ID] = candidate
	}
	result := make([]account.RoutingCandidate, 0, len(values))
	seen := make(map[uint64]bool, len(values))
	for _, id := range orderedIDs {
		candidate, ok := byID[id]
		if !ok || seen[id] {
			continue
		}
		seen[id] = true
		result = append(result, candidate)
	}
	for _, candidate := range values {
		if seen[candidate.Credential.ID] {
			continue
		}
		result = append(result, candidate)
	}
	return result
}

// promptCacheStickyKey 将调用方缓存键压缩为固定长度，仅用于本地账号粘滞索引。
func promptCacheStickyKey(value string) string {
	if value == "" {
		return ""
	}
	digest := sha256.Sum256([]byte(value))
	return hex.EncodeToString(digest[:])
}

// AcquirePinned 为 previous_response_id 等账号归属请求获取指定账号租约。
func (s *Selector) AcquirePinned(ctx context.Context, provider account.Provider, accountID uint64, upstreamModel, quotaMode string, inference bool) (*accountLease, error) {
	now := time.Now().UTC()
	values, err := s.loadCandidates(ctx, provider, upstreamModel, quotaMode, now)
	if err != nil {
		return nil, err
	}
	for _, candidate := range values {
		value := candidate.Credential
		if value.ID != accountID {
			continue
		}
		if !value.Enabled || value.AuthStatus != account.AuthStatusActive {
			return nil, &SelectionUnavailableError{Reason: SelectionNoAccounts}
		}
		if inference {
			if candidate.ModelCapabilityKnown && !candidate.SupportsModel {
				return nil, &SelectionUnavailableError{Reason: SelectionUnsupportedModel}
			}
			if candidateModelQuotaBlocked(candidate, now) {
				return nil, &SelectionUnavailableError{Reason: SelectionModelCooling, RetryAfter: retryDelay(now, candidate.ModelQuotaBlock.CooldownUntil)}
			}
			if value.CooldownUntil != nil && now.Before(*value.CooldownUntil) {
				return nil, &SelectionUnavailableError{Reason: SelectionCooling, RetryAfter: retryDelay(now, *value.CooldownUntil)}
			}
			if recovery := candidate.QuotaRecovery; recovery != nil && recovery.Status != account.QuotaRecoveryStatusActive {
				if recovery.NextProbeAt == nil || now.Before(*recovery.NextProbeAt) {
					var retryAfter time.Duration
					if recovery.NextProbeAt != nil {
						retryAfter = retryDelay(now, *recovery.NextProbeAt)
					}
					return nil, &SelectionUnavailableError{Reason: SelectionQuotaExhausted, RetryAfter: retryAfter}
				}
				lease, err := s.acquirePinnedCapacity(ctx, value, upstreamModel)
				if err != nil {
					return nil, err
				}
				claimed, err := s.accounts.ClaimQuotaProbe(ctx, value.ID, now, now.Add(quotaProbeLease))
				if err != nil || !claimed {
					lease.Release()
					if err != nil {
						return nil, err
					}
					return nil, fmt.Errorf("绑定的上游账号恢复探测已被占用")
				}
				lease.QuotaProbe = true
				lease.QuotaProbeKind = recovery.Kind
				lease.Billing = candidate.Billing
				return lease, nil
			}
			if candidate.Billing != nil && candidate.Billing.IsExhausted(value.MinimumRemaining) {
				return nil, &SelectionUnavailableError{Reason: SelectionQuotaExhausted}
			}
			if candidate.QuotaWindow != nil && candidate.QuotaWindow.Remaining <= 0 {
				var retryAfter time.Duration
				if candidate.QuotaWindow.ResetAt != nil {
					retryAfter = retryDelay(now, *candidate.QuotaWindow.ResetAt)
				}
				return nil, &SelectionUnavailableError{Reason: SelectionQuotaExhausted, RetryAfter: retryAfter}
			}
			if requiresImagineQuotaAdmission(upstreamModel, quotaMode) && !candidateImagineQuotaAdmissible(candidate, now) {
				return nil, &SelectionUnavailableError{Reason: SelectionQuotaExhausted}
			}
		}
		lease, err := s.acquirePinnedCapacity(ctx, value, upstreamModel)
		if err != nil {
			return nil, err
		}
		lease.Billing = candidate.Billing
		lease.QuotaMode = effectiveQuotaMode(candidate, quotaMode)
		return lease, nil
	}
	return nil, &SelectionUnavailableError{Reason: SelectionNoAccounts}
}

// candidateModelQuotaBlocked 让新同步到的明确正额度覆盖旧的临时 model block；
// 0/0 不是恢复证据，仍保留真实 429 建立的冷却。
func candidateModelQuotaBlocked(candidate account.RoutingCandidate, now time.Time) bool {
	if candidate.ModelQuotaBlock == nil || !now.Before(candidate.ModelQuotaBlock.CooldownUntil) {
		return false
	}
	return candidate.QuotaWindow == nil || candidate.QuotaWindow.Total <= 0 || candidate.QuotaWindow.Remaining <= 0
}

func effectiveQuotaMode(candidate account.RoutingCandidate, fallback string) string {
	if candidate.QuotaWindow != nil && candidate.QuotaWindow.Mode == "weekly" {
		return "weekly"
	}
	return fallback
}

func (s *Selector) MarkSuccess(ctx context.Context, credential account.Credential) {
	s.markSuccess(ctx, credential, true)
}

func (s *Selector) markSuccess(ctx context.Context, credential account.Credential, quotaProbe bool) {
	now := time.Now().UTC()
	persist := credential.FailureCount > 0 || credential.CooldownUntil != nil || credential.LastError != ""
	s.mu.Lock()
	if last := s.lastSuccessAt[credential.ID]; last.IsZero() || now.Sub(last) >= successPersistInterval {
		persist = true
	}
	if persist {
		s.lastSuccessAt[credential.ID] = now
	}
	s.mu.Unlock()
	if persist {
		_ = s.accounts.UpdateHealth(ctx, credential.ID, 0, nil, "", true)
	}
	if quotaProbe {
		_ = s.accounts.ClearQuotaRecovery(ctx, credential.ID)
	}
	if quotaProbe || credential.FailureCount > 0 || credential.CooldownUntil != nil || credential.LastError != "" {
		s.invalidateCandidates(credential.Provider)
	}
}

func (s *Selector) MarkFreeQuotaExhausted(ctx context.Context, credential account.Credential, used, limit int64) {
	now := time.Now().UTC()
	nextProbeAt := now.Add(24 * time.Hour)
	_ = s.accounts.SaveQuotaRecovery(ctx, account.QuotaRecovery{
		AccountID: credential.ID, Kind: account.QuotaRecoveryKindFree, Status: account.QuotaRecoveryStatusExhausted,
		ConfirmedUsed: used, ConfirmedLimit: limit, ExhaustedAt: &now,
		NextProbeAt: &nextProbeAt, LastConfirmedAt: &now, UpdatedAt: now,
	})
	_ = s.sticky.DeleteByAccount(ctx, credential.ID)
	s.invalidateCandidates(credential.Provider)
}

func (s *Selector) MarkModelQuotaExhausted(ctx context.Context, credential account.Credential, upstreamModel string, retryAfter time.Duration) {
	upstreamModel = strings.TrimSpace(upstreamModel)
	if upstreamModel == "" {
		s.MarkFreeQuotaExhausted(ctx, credential, 0, 0)
		return
	}
	if retryAfter <= 0 {
		retryAfter = 24 * time.Hour
	}
	until := time.Now().UTC().Add(retryAfter)
	_ = s.accounts.UpsertModelQuotaBlock(ctx, account.ModelQuotaBlock{
		AccountID: credential.ID, UpstreamModel: upstreamModel, Reason: "model_quota_depleted", CooldownUntil: until, UpdatedAt: time.Now().UTC(),
	})
	_ = s.accounts.SaveModelState(ctx, account.ModelState{
		AccountID: credential.ID, UpstreamModel: upstreamModel, Status: account.ModelStatusQuotaExhausted,
		Reason: "usage_limit_reached", ConsecutiveFailures: 1, LastAttemptAt: time.Now().UTC(), CooldownUntil: &until, UpdatedAt: time.Now().UTC(),
	})
	s.invalidateCandidates(credential.Provider)
}

// MarkPaidQuotaExhausted 使用已知真实账期将付费账号移出号池，到期后才允许 Billing 探测。
func (s *Selector) MarkPaidQuotaExhausted(ctx context.Context, credential account.Credential, billing *account.Billing) bool {
	if billing == nil || (billing.MonthlyLimit <= 0 && billing.OnDemandCap <= 0 && billing.OnDemandUsed <= 0 && billing.PrepaidBalance <= 0 && billing.CreditUsagePercent <= 0) {
		return false
	}
	periodEnd, ok := billing.PeriodEnd()
	if !ok {
		return false
	}
	now := time.Now().UTC()
	_ = s.accounts.SaveQuotaRecovery(ctx, account.QuotaRecovery{
		AccountID: credential.ID, Kind: account.QuotaRecoveryKindPaid, Status: account.QuotaRecoveryStatusExhausted,
		ExhaustedAt: &now, NextProbeAt: &periodEnd, LastConfirmedAt: &now, UpdatedAt: now,
	})
	_ = s.sticky.DeleteByAccount(ctx, credential.ID)
	s.invalidateCandidates(credential.Provider)
	return true
}

// MarkQuotaStateChanged 在 Billing 探测改变持久化额度状态后立即失效候选快照。
func (s *Selector) MarkQuotaStateChanged(provider account.Provider) { s.invalidateCandidates(provider) }

// MarkModelSoftStop 记录模型级退避，不修改账号全局健康或其他模型的调度资格。
func (s *Selector) MarkModelSoftStop(ctx context.Context, accountID uint64, upstreamModel string) error {
	upstreamModel = strings.TrimSpace(upstreamModel)
	if accountID == 0 || upstreamModel == "" {
		return nil
	}
	now := time.Now().UTC()
	key := modelOutcomeKey{accountID: accountID, upstreamModel: upstreamModel}
	s.mu.Lock()
	if s.modelOutcomes == nil {
		s.modelOutcomes = make(map[modelOutcomeKey]modelOutcome)
	}
	value := s.modelOutcomes[key]
	if value.lastSoftStopAt.IsZero() || now.Sub(value.lastSoftStopAt) > modelOutcomeSuccessTTL || value.lastSuccessAt.After(value.lastSoftStopAt) {
		value.consecutiveSoftStop = 0
	}
	value.consecutiveSoftStop++
	cooldown := modelSoftStopBaseCooldown
	for count := 1; count < value.consecutiveSoftStop && cooldown < modelSoftStopMaxCooldown; count++ {
		cooldown *= 2
	}
	if cooldown > modelSoftStopMaxCooldown {
		cooldown = modelSoftStopMaxCooldown
	}
	value.lastSoftStopAt = now
	value.softStopUntil = now.Add(cooldown)
	s.modelOutcomes[key] = value
	s.mu.Unlock()
	if s.accounts == nil {
		return nil
	}
	return s.accounts.SaveModelState(ctx, account.ModelState{
		AccountID: accountID, UpstreamModel: upstreamModel, Status: account.ModelStatusSoftStop,
		Reason: "soft_stop", ConsecutiveFailures: value.consecutiveSoftStop,
		LastAttemptAt: now, CooldownUntil: &value.softStopUntil, UpdatedAt: now,
	})
}

// MarkModelSuccess 让近期验证成功的账号在同模型中优先，同时清除它的 soft-stop 退避。
func (s *Selector) MarkModelSuccess(ctx context.Context, accountID uint64, upstreamModel string) error {
	upstreamModel = strings.TrimSpace(upstreamModel)
	if accountID == 0 || upstreamModel == "" {
		return nil
	}
	now := time.Now().UTC()
	key := modelOutcomeKey{accountID: accountID, upstreamModel: upstreamModel}
	s.mu.Lock()
	if s.modelOutcomes == nil {
		s.modelOutcomes = make(map[modelOutcomeKey]modelOutcome)
	}
	s.modelOutcomes[key] = modelOutcome{lastSuccessAt: now}
	s.mu.Unlock()
	if s.accounts == nil {
		return nil
	}
	return s.accounts.SaveModelState(ctx, account.ModelState{
		AccountID: accountID, UpstreamModel: upstreamModel, Status: account.ModelStatusAvailable,
		Reason: "image_generated", LastAttemptAt: now, LastSuccessAt: &now, UpdatedAt: now,
	})
}

func (s *Selector) MarkModelAuthFailed(ctx context.Context, accountID uint64, upstreamModel string) error {
	return s.saveModelFailure(ctx, accountID, upstreamModel, account.ModelStatusAuthFailed, "unauthorized")
}

func (s *Selector) MarkModelSignatureFailed(ctx context.Context, accountID uint64, upstreamModel string) error {
	return s.saveModelFailure(ctx, accountID, upstreamModel, account.ModelStatusSignatureFailed, "anti_bot_rejected")
}

func (s *Selector) saveModelFailure(ctx context.Context, accountID uint64, upstreamModel string, status account.ModelStatus, reason string) error {
	upstreamModel = strings.TrimSpace(upstreamModel)
	if s.accounts == nil || accountID == 0 || upstreamModel == "" {
		return nil
	}
	now := time.Now().UTC()
	return s.accounts.SaveModelState(ctx, account.ModelState{
		AccountID: accountID, UpstreamModel: upstreamModel, Status: status, Reason: reason,
		ConsecutiveFailures: 1, LastAttemptAt: now, UpdatedAt: now,
	})
}

// ConsumeQuota 将成功请求的本地额度变化应用到候选快照，避免为单账号变化清空整个 Provider 缓存。
func (s *Selector) ConsumeQuota(provider account.Provider, accountID uint64, mode string, amount int) {
	if accountID == 0 || mode == "" || mode == "weekly" || amount <= 0 {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	for key, snapshot := range s.candidates {
		if key.provider != provider {
			continue
		}
		for index := range snapshot.values {
			candidate := &snapshot.values[index]
			if candidate.Credential.ID != accountID || candidate.QuotaWindow == nil || candidate.QuotaWindow.Mode != mode {
				continue
			}
			window := *candidate.QuotaWindow
			window.Remaining = max(0, window.Remaining-amount)
			window.UpdatedAt = time.Now().UTC()
			candidate.QuotaWindow = &window
		}
		s.candidates[key] = snapshot
	}
}

func (s *Selector) MarkFailure(ctx context.Context, credential account.Credential, status int, retryAfter time.Duration) {
	failureCount := credential.FailureCount + 1
	_, cooldownBase, cooldownMax, _ := s.routingConfig()
	cooldown := cooldownBase
	for i := 1; i < failureCount && cooldown < cooldownMax; i++ {
		cooldown *= 2
	}
	if cooldown > cooldownMax {
		cooldown = cooldownMax
	}
	if retryAfter > cooldown {
		cooldown = retryAfter
	}
	until := time.Now().UTC().Add(cooldown)
	_ = s.accounts.UpdateHealth(ctx, credential.ID, failureCount, &until, fmt.Sprintf("upstream status %d", status), false)
	s.invalidateCandidates(credential.Provider)
	if status == 401 || status == 402 || status == 403 || status == 429 {
		_ = s.sticky.DeleteByAccount(ctx, credential.ID)
	}
}

func (s *Selector) loadCandidatesForAcquire(ctx context.Context, provider account.Provider, upstreamModel, quotaMode string, now time.Time) ([]account.RoutingCandidate, error) {
	if provider == account.ProviderBuild {
		s.mu.Lock()
		source := s.buildDispatch
		s.mu.Unlock()
		if source == nil {
			return s.loadCandidates(ctx, provider, upstreamModel, quotaMode, now)
		}
		return s.loadBuildCandidatesByIndex(ctx, source, upstreamModel, quotaMode, now)
	}
	if provider == account.ProviderWeb {
		s.mu.Lock()
		source := s.webDispatch
		s.mu.Unlock()
		if source == nil {
			return s.loadCandidates(ctx, provider, upstreamModel, quotaMode, now)
		}
		lane := accountapp.ResolveWebAcquireLane(upstreamModel, quotaMode)
		return s.loadWebCandidatesByIndex(ctx, source, lane, upstreamModel, quotaMode, now)
	}
	return s.loadCandidates(ctx, provider, upstreamModel, quotaMode, now)
}

func (s *Selector) loadWebCandidatesByIndex(ctx context.Context, source webDispatchSource, lane accountapp.WebLane, upstreamModel, quotaMode string, now time.Time) ([]account.RoutingCandidate, error) {
	batch := buildDispatchHydrateInitial
	var sawDispatch bool
	for {
		dispatchIDs := source.OrderedWebDispatchIDs(lane, batch)
		if strings.EqualFold(upstreamModel, webLiteImageUpstreamModel) {
			dispatchIDs = mergeDispatchIDs(s.ticketHolderIDs(ctx), dispatchIDs)
		}
		if len(dispatchIDs) == 0 {
			source.EnsureWebPoolIndexWarm(ctx)
			dispatchIDs = source.OrderedWebDispatchIDs(lane, batch)
			if len(dispatchIDs) == 0 {
				return nil, selectionErr(SelectionNoDispatchIndex)
			}
		}
		sawDispatch = true
		values, err := s.accounts.ListRoutingCandidatesByIDs(ctx, account.ProviderWeb, upstreamModel, quotaMode, dispatchIDs)
		if err != nil {
			return nil, err
		}
		if len(values) > 0 {
			return values, nil
		}
		if len(dispatchIDs) < batch || batch >= buildDispatchHydrateMax {
			break
		}
		batch = min(batch*2, buildDispatchHydrateMax)
	}
	if sawDispatch {
		return nil, selectionErr(SelectionPinFiltered)
	}
	return nil, selectionErr(SelectionNoAccounts)
}

func selectionErr(reason SelectionUnavailableReason) *SelectionUnavailableError {
	return &SelectionUnavailableError{Reason: reason, SelectionReason: string(reason)}
}

func (s *Selector) loadBuildCandidatesByIndex(ctx context.Context, source buildDispatchSource, upstreamModel, quotaMode string, now time.Time) ([]account.RoutingCandidate, error) {
	batch := buildDispatchHydrateInitial
	for {
		dispatchIDs := source.OrderedDispatchIDs(batch)
		ids := mergeBuildHydrateIDs(dispatchIDs, source.DueNormalProbeIDs(now, buildNormalProbeHydrateLimit))
		if len(ids) == 0 {
			source.EnsurePoolIndexWarm(ctx)
			dispatchIDs = source.OrderedDispatchIDs(batch)
			ids = mergeBuildHydrateIDs(dispatchIDs, source.DueNormalProbeIDs(now, buildNormalProbeHydrateLimit))
			if len(ids) == 0 {
				return nil, &SelectionUnavailableError{Reason: SelectionNoAccounts}
			}
		}
		values, err := s.accounts.ListRoutingCandidatesByIDs(ctx, account.ProviderBuild, upstreamModel, quotaMode, ids)
		if err != nil {
			return nil, err
		}
		if len(values) > 0 {
			return values, nil
		}
		if len(dispatchIDs) < batch || batch >= buildDispatchHydrateMax {
			break
		}
		batch = min(batch*2, buildDispatchHydrateMax)
	}
	return nil, nil
}

func mergeBuildHydrateIDs(dispatchIDs, probeIDs []uint64) []uint64 {
	if len(dispatchIDs) == 0 && len(probeIDs) == 0 {
		return nil
	}
	seen := make(map[uint64]struct{}, len(dispatchIDs)+len(probeIDs))
	merged := make([]uint64, 0, len(dispatchIDs)+len(probeIDs))
	for _, id := range dispatchIDs {
		if id == 0 {
			continue
		}
		if _, ok := seen[id]; ok {
			continue
		}
		seen[id] = struct{}{}
		merged = append(merged, id)
	}
	for _, id := range probeIDs {
		if id == 0 {
			continue
		}
		if _, ok := seen[id]; ok {
			continue
		}
		seen[id] = struct{}{}
		merged = append(merged, id)
	}
	return merged
}

func (s *Selector) loadCandidates(ctx context.Context, provider account.Provider, upstreamModel, quotaMode string, now time.Time) ([]account.RoutingCandidate, error) {
	key := candidateCacheKey{provider: provider, upstreamModel: upstreamModel, quotaMode: quotaMode}
	s.mu.Lock()
	if snapshot, ok := s.candidates[key]; ok && now.Before(snapshot.expiresAt) {
		values := append([]account.RoutingCandidate(nil), snapshot.values...)
		s.mu.Unlock()
		return values, nil
	}
	s.mu.Unlock()
	loadKey := string(provider) + "\x00" + upstreamModel + "\x00" + quotaMode
	loaded, err, _ := s.candidateLoads.Do(loadKey, func() (any, error) {
		checkTime := time.Now().UTC()
		s.mu.Lock()
		if snapshot, ok := s.candidates[key]; ok && checkTime.Before(snapshot.expiresAt) {
			values := append([]account.RoutingCandidate(nil), snapshot.values...)
			s.mu.Unlock()
			return values, nil
		}
		s.mu.Unlock()
		values, err := s.accounts.ListRoutingCandidates(ctx, provider, upstreamModel, quotaMode)
		if err != nil {
			return nil, err
		}
		s.mu.Lock()
		s.candidates[key] = candidateSnapshot{values: append([]account.RoutingCandidate(nil), values...), expiresAt: checkTime.Add(candidateCacheTTL)}
		s.mu.Unlock()
		return values, nil
	})
	if err != nil {
		return nil, err
	}
	return append([]account.RoutingCandidate(nil), loaded.([]account.RoutingCandidate)...), nil
}

func (s *Selector) invalidateCandidates(provider account.Provider) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for key := range s.candidates {
		if key.provider == provider {
			delete(s.candidates, key)
		}
	}
}

func (s *Selector) claimAccountSlot(ctx context.Context, value account.Credential, upstreamModel string) (*accountLease, error) {
	limit := accountConcurrencyLimit(value, upstreamModel)
	release, acquired, err := s.concurrency.Acquire(ctx, fmt.Sprintf("account:%d", value.ID), limit)
	if err != nil {
		return nil, fmt.Errorf("获取账号并发租约: %w", err)
	}
	if !acquired {
		return nil, nil
	}
	s.mu.Lock()
	s.lastSelectedAt[value.ID] = time.Now().UTC()
	s.mu.Unlock()
	return &accountLease{Credential: value, release: func() {
		release()
		s.announceLeaseReturn()
	}}, nil
}

func accountConcurrencyLimit(value account.Credential, upstreamModel string) int {
	if strings.EqualFold(strings.TrimSpace(upstreamModel), webLiteImageUpstreamModel) {
		return 1
	}
	if value.MaxConcurrent > 0 {
		return value.MaxConcurrent
	}
	return account.DefaultMaxConcurrent
}

func (s *Selector) acquirePinnedCapacity(ctx context.Context, value account.Credential, upstreamModel string) (*accountLease, error) {
	_, _, _, capacityWait := s.routingConfig()
	deadline := time.Now().Add(capacityWait)
	for {
		lease, err := s.claimAccountSlot(ctx, value, upstreamModel)
		if err != nil || lease != nil {
			return lease, err
		}
		if capacityWait <= 0 {
			return nil, &SelectionUnavailableError{Reason: SelectionSaturated, RetryAfter: time.Second}
		}
		retry, err := s.awaitLeaseRetry(ctx, deadline)
		if err != nil {
			return nil, err
		}
		if !retry {
			return nil, &SelectionUnavailableError{Reason: SelectionSaturated, RetryAfter: time.Second}
		}
	}
}

func (s *Selector) leaseReturnNotice() <-chan struct{} {
	s.leaseWakeMu.Lock()
	defer s.leaseWakeMu.Unlock()
	if s.leaseWake == nil {
		s.leaseWake = make(chan struct{})
	}
	return s.leaseWake
}

func (s *Selector) announceLeaseReturn() {
	s.leaseWakeMu.Lock()
	if s.leaseWake != nil {
		close(s.leaseWake)
	}
	s.leaseWake = make(chan struct{})
	s.leaseWakeMu.Unlock()
}

// awaitLeaseRetry 在本实例归还租约时立即重试；短轮询用于感知其他实例释放的共享并发名额。
func (s *Selector) awaitLeaseRetry(ctx context.Context, deadline time.Time) (bool, error) {
	remaining := time.Until(deadline)
	if remaining <= 0 {
		return false, nil
	}
	notice := s.leaseReturnNotice()
	timer := time.NewTimer(min(remaining, 100*time.Millisecond))
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false, ctx.Err()
	case <-notice:
		return true, nil
	case <-timer.C:
		return time.Now().Before(deadline), nil
	}
}

func earlierFuture(current, candidate, now time.Time) time.Time {
	if candidate.IsZero() || !now.Before(candidate) {
		return current
	}
	if current.IsZero() || candidate.Before(current) {
		return candidate
	}
	return current
}

func retryDelay(now, retryAt time.Time) time.Duration {
	if retryAt.IsZero() || !now.Before(retryAt) {
		return 0
	}
	return retryAt.Sub(now)
}

func (s *Selector) sortCandidates(ctx context.Context, values []account.RoutingCandidate, now time.Time, tierOrder []account.WebTier, upstreamModel string) error {
	s.mu.Lock()
	lastSelected := make(map[uint64]time.Time, len(s.lastSelectedAt))
	for id, value := range s.lastSelectedAt {
		lastSelected[id] = value
	}
	modelRanks := make(map[uint64]int, len(values))
	upstreamModel = strings.TrimSpace(upstreamModel)
	for _, candidate := range values {
		state := candidate.ModelState
		if state == nil || state.UpstreamModel != upstreamModel {
			continue
		}
		switch {
		case state.Status == account.ModelStatusSoftStop && state.CooldownUntil != nil && now.Before(*state.CooldownUntil):
			modelRanks[state.AccountID] = 2
		case state.Status == account.ModelStatusAvailable && state.LastSuccessAt != nil && now.Sub(*state.LastSuccessAt) <= modelOutcomeSuccessTTL:
			modelRanks[state.AccountID] = 0
		}
	}
	for key, outcome := range s.modelOutcomes {
		latest := outcome.lastSuccessAt
		if outcome.lastSoftStopAt.After(latest) {
			latest = outcome.lastSoftStopAt
		}
		if !latest.IsZero() && now.Sub(latest) > modelOutcomeRetention {
			delete(s.modelOutcomes, key)
			continue
		}
		if key.upstreamModel != upstreamModel {
			continue
		}
		switch {
		case now.Before(outcome.softStopUntil):
			modelRanks[key.accountID] = 2
		case !outcome.lastSuccessAt.IsZero() && now.Sub(outcome.lastSuccessAt) <= modelOutcomeSuccessTTL:
			modelRanks[key.accountID] = 0
		default:
			modelRanks[key.accountID] = 1
		}
	}
	s.mu.Unlock()
	remaining := make(map[uint64]float64, len(values))
	imagineRemaining := make(map[uint64]int, len(values))
	imagineFresh := make(map[uint64]bool, len(values))
	fresh := make(map[uint64]bool, len(values))
	ticketCounts := make(map[uint64]int64, len(values))
	s.mu.Lock()
	ticketSource := s.chromeTickets
	s.mu.Unlock()
	if ticketSource != nil && strings.EqualFold(upstreamModel, webLiteImageUpstreamModel) {
		if counts := ticketSource.AvailableCounts(ctx); counts != nil {
			ticketCounts = counts
		}
	}
	inFlight := make(map[uint64]int, len(values))
	concurrencyKeys := make([]string, 0, len(values))
	for _, candidate := range values {
		concurrencyKeys = append(concurrencyKeys, fmt.Sprintf("account:%d", candidate.Credential.ID))
	}
	concurrencySnapshot := make(map[string]int, len(values))
	batchReader, batched := s.concurrency.(repository.ConcurrencySnapshotReader)
	if batched {
		var err error
		concurrencySnapshot, err = batchReader.CurrentMany(ctx, concurrencyKeys)
		if err != nil {
			return fmt.Errorf("批量读取账号并发租约: %w", err)
		}
	}
	for _, candidate := range values {
		value := candidate.Credential
		key := fmt.Sprintf("account:%d", value.ID)
		current, found := concurrencySnapshot[key]
		if !batched {
			var err error
			current, err = s.concurrency.Current(ctx, key)
			if err != nil {
				return fmt.Errorf("读取账号并发租约: %w", err)
			}
		} else if !found {
			current = 0
		}
		inFlight[value.ID] = current
		if candidate.Billing != nil {
			remaining[value.ID] = candidate.Billing.Remaining()
			fresh[value.ID] = now.Sub(candidate.Billing.SyncedAt) <= 30*time.Minute
		}
		if candidate.QuotaWindow != nil && candidate.QuotaWindow.Mode == "imagine" {
			imagineRemaining[value.ID] = candidate.QuotaWindow.Remaining
			imagineFresh[value.ID] = candidateImagineQuotaAdmissible(candidate, now)
		}
	}
	sort.SliceStable(values, func(i, j int) bool {
		leftCandidate, rightCandidate := values[i], values[j]
		left, right := leftCandidate.Credential, rightCandidate.Credential
		if leftCandidate.SupportsModel != rightCandidate.SupportsModel {
			return leftCandidate.SupportsModel
		}
		if leftCandidate.ModelCapabilityKnown != rightCandidate.ModelCapabilityKnown {
			return leftCandidate.ModelCapabilityKnown
		}
		if left.Provider == account.ProviderBuild && right.Provider == account.ProviderBuild {
			leftVerified := strings.TrimSpace(left.ObservedModel) != ""
			rightVerified := strings.TrimSpace(right.ObservedModel) != ""
			if leftVerified != rightVerified {
				return leftVerified
			}
		}
		leftTier, rightTier := tierOrderRank(tierOrder, left.WebTier), tierOrderRank(tierOrder, right.WebTier)
		if leftTier != rightTier {
			return leftTier < rightTier
		}
		leftRank, leftKnown := modelRanks[left.ID]
		if !leftKnown {
			leftRank = 1
		}
		rightRank, rightKnown := modelRanks[right.ID]
		if !rightKnown {
			rightRank = 1
		}
		if leftRank != rightRank {
			return leftRank < rightRank
		}
		if strings.EqualFold(upstreamModel, webLiteImageUpstreamModel) {
			leftTickets, leftOK := ticketCounts[left.ID]
			rightTickets, rightOK := ticketCounts[right.ID]
			if leftOK != rightOK {
				return leftOK
			}
			if leftTickets != rightTickets {
				return leftTickets > rightTickets
			}
			leftFresh, rightFresh := imagineFresh[left.ID], imagineFresh[right.ID]
			if leftFresh != rightFresh {
				return leftFresh
			}
			leftImagine, leftOK := imagineRemaining[left.ID]
			rightImagine, rightOK := imagineRemaining[right.ID]
			if leftOK != rightOK {
				return leftOK
			}
			if leftImagine != rightImagine {
				return leftImagine > rightImagine
			}
		}
		if left.Priority != right.Priority {
			return left.Priority > right.Priority
		}
		if fresh[left.ID] != fresh[right.ID] {
			return fresh[left.ID]
		}
		if inFlight[left.ID] != inFlight[right.ID] {
			return inFlight[left.ID] < inFlight[right.ID]
		}
		if remaining[left.ID] != remaining[right.ID] {
			return remaining[left.ID] > remaining[right.ID]
		}
		if !lastSelected[left.ID].Equal(lastSelected[right.ID]) {
			return lastSelected[left.ID].Before(lastSelected[right.ID])
		}
		return left.ID < right.ID
	})
	return nil
}

func (s *Selector) resolveTierOrder(provider account.Provider, upstreamModel string) []account.WebTier {
	if s.tierOrders == nil {
		return nil
	}
	return s.tierOrders.TierOrder(provider, upstreamModel)
}

func tierOrderRank(order []account.WebTier, tier account.WebTier) int {
	for index, value := range order {
		if value == tier {
			return index
		}
	}
	return len(order)
}

func (s *Selector) explorationRate() float64 {
	s.mu.Lock()
	epsilon := s.explorationEpsilon
	s.mu.Unlock()
	if epsilon <= 0 {
		return 0
	}
	if epsilon > 1 {
		return 1
	}
	return epsilon
}

func (s *Selector) randomUnit() float64 {
	if s.randFloat != nil {
		return s.randFloat()
	}
	var buf [8]byte
	if _, err := rand.Read(buf[:]); err != nil {
		return 0
	}
	return float64(binary.LittleEndian.Uint64(buf[:])>>11) / float64(1<<53)
}

func (s *Selector) maybeExploreShuffle(values []account.RoutingCandidate) {
	if len(values) <= 1 {
		return
	}
	if s.randomUnit() >= s.explorationRate() {
		return
	}
	shuffleRoutingCandidates(values, s.randomUnit)
}

func shuffleRoutingCandidates(values []account.RoutingCandidate, random func() float64) {
	for i := len(values) - 1; i > 0; i-- {
		j := int(random() * float64(i+1))
		if j < 0 {
			j = 0
		}
		if j > i {
			j = i
		}
		values[i], values[j] = values[j], values[i]
	}
}

func requiresImagineQuotaAdmission(upstreamModel, quotaMode string) bool {
	if strings.EqualFold(strings.TrimSpace(upstreamModel), webLiteImageUpstreamModel) {
		return true
	}
	return strings.TrimSpace(quotaMode) == "imagine"
}

func requiresChromeTicketAdmission(upstreamModel string) bool {
	return strings.EqualFold(strings.TrimSpace(upstreamModel), webLiteImageUpstreamModel)
}

func (s *Selector) chromeTicketsEnforced() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.chromeTickets != nil
}

func (s *Selector) ticketHolderIDs(ctx context.Context) []uint64 {
	s.mu.Lock()
	ticketSource := s.chromeTickets
	s.mu.Unlock()
	if ticketSource == nil {
		return nil
	}
	counts := ticketSource.AvailableCounts(ctx)
	if len(counts) == 0 {
		return nil
	}
	ids := make([]uint64, 0, len(counts))
	for id, count := range counts {
		if count > 0 {
			ids = append(ids, id)
		}
	}
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
	return ids
}

func (s *Selector) filterChromeTicketCandidates(ctx context.Context, candidates []account.RoutingCandidate) ([]account.RoutingCandidate, error) {
	s.mu.Lock()
	ticketSource := s.chromeTickets
	s.mu.Unlock()
	if ticketSource == nil {
		return candidates, nil
	}
	counts := ticketSource.AvailableCounts(ctx)
	if len(counts) == 0 {
		return nil, selectionErr(SelectionNoChromeTickets)
	}
	filtered := make([]account.RoutingCandidate, 0, len(candidates))
	for _, candidate := range candidates {
		if counts[candidate.Credential.ID] > 0 {
			filtered = append(filtered, candidate)
		}
	}
	if len(filtered) == 0 {
		return nil, selectionErr(SelectionNoChromeTickets)
	}
	return filtered, nil
}

func mergeDispatchIDs(preferred, current []uint64) []uint64 {
	if len(preferred) == 0 {
		return current
	}
	seen := make(map[uint64]struct{}, len(preferred)+len(current))
	merged := make([]uint64, 0, len(preferred)+len(current))
	for _, id := range preferred {
		if id == 0 {
			continue
		}
		if _, ok := seen[id]; ok {
			continue
		}
		seen[id] = struct{}{}
		merged = append(merged, id)
	}
	for _, id := range current {
		if id == 0 {
			continue
		}
		if _, ok := seen[id]; ok {
			continue
		}
		seen[id] = struct{}{}
		merged = append(merged, id)
	}
	return merged
}

func candidateImagineQuotaAdmissible(candidate account.RoutingCandidate, now time.Time) bool {
	window := candidate.QuotaWindow
	if window == nil || window.Mode != "imagine" {
		return false
	}
	if window.Source != account.QuotaSourceUpstream {
		return false
	}
	if window.Total <= 0 || window.Remaining <= 0 {
		return false
	}
	if window.SyncedAt == nil || now.Sub(*window.SyncedAt) > imagineQuotaFreshTTL {
		return false
	}
	return true
}
