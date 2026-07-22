package account

import (
	"context"
	"sort"
	"time"

	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
	"github.com/chenyme/grok2api/backend/internal/repository"
)

const (
	webImagePoolCap = 50
	webChatPoolCap  = 50
	imagineUpstream = "grok-imagine-image"
)

// WebPoolSnapshot 描述图池 / 对话池当前调度位。
type WebPoolSnapshot struct {
	ImagePoolIDs   []uint64  `json:"imagePoolIds"`
	ChatPoolIDs    []uint64  `json:"chatPoolIds"`
	EnabledIDs     []uint64  `json:"enabledIds"`
	ImagePoolSize  int       `json:"imagePoolSize"`
	ChatPoolSize   int       `json:"chatPoolSize"`
	EnabledCount   int       `json:"enabledCount"`
	ImagePoolCap   int       `json:"imagePoolCap"`
	ChatPoolCap    int       `json:"chatPoolCap"`
	ReconciledAt   time.Time `json:"reconciledAt"`
	EnabledAdded   int       `json:"enabledAdded"`
	EnabledRemoved int       `json:"enabledRemoved"`
	ThreePools     WebThreePoolsPublic `json:"threePools,omitempty"`
}

type webPoolCandidate struct {
	id             uint64
	priority       int
	fastRem        int
	autoRem        int
	imagineWindow  *accountdomain.QuotaWindow
	modelState     *accountdomain.ModelState
	enabled        bool
	active         bool
	cooling        bool // 账号级 cooldown_until
	imagineBlocked bool // grok-imagine-image model block（只挡图池）
}

// ReconcileWebPools 只做「无额度/冷却出池」：在当前已启用账号里踢掉不合格号。
// 不自动 enable 任何新号（进池仍由人工/调度脚本控制），避免把未验证出口能力的账号拉进生产池。
// 图池使用独立 Imagine 额度和模型状态；对话池仍看 auto/fast；快照各最多 50。
func (s *Service) ReconcileWebPools(ctx context.Context) (WebPoolSnapshot, error) {
	now := time.Now().UTC()
	accounts, _, err := s.accounts.List(ctx, repository.AccountListQuery{
		Page:   repository.PageQuery{Offset: 0, Limit: 5000, Sort: repository.SortQuery{Field: "createdAt", Direction: repository.SortAscending}},
		Filter: repository.AccountListFilter{Provider: string(accountdomain.ProviderWeb), Now: now},
	})
	if err != nil {
		return WebPoolSnapshot{}, err
	}
	ids := make([]uint64, 0, len(accounts))
	for _, value := range accounts {
		ids = append(ids, value.ID)
	}
	windowsByAccount, err := s.accounts.GetQuotaWindows(ctx, ids)
	if err != nil {
		return WebPoolSnapshot{}, err
	}
	blocks, err := s.accounts.GetActiveModelQuotaBlocks(ctx, ids, imagineUpstream, now)
	if err != nil {
		return WebPoolSnapshot{}, err
	}
	modelStates, err := s.accounts.GetModelStates(ctx, ids)
	if err != nil {
		return WebPoolSnapshot{}, err
	}

	candidates := make([]webPoolCandidate, 0, len(accounts))
	for _, value := range accounts {
		fastRem, autoRem := quotaRemaining(windowsByAccount[value.ID], "fast"), quotaRemaining(windowsByAccount[value.ID], "auto")
		accountCooling := value.CooldownUntil != nil && value.CooldownUntil.After(now)
		candidates = append(candidates, webPoolCandidate{
			id: value.ID, priority: value.Priority, fastRem: fastRem, autoRem: autoRem,
			imagineWindow: findQuotaWindow(windowsByAccount[value.ID], "imagine"),
			modelState:    findModelState(modelStates[value.ID], imagineUpstream),
			enabled:       value.Enabled, active: value.AuthStatus == accountdomain.AuthStatusActive,
			// cooling=账号级冷却；Imagine model block 单独看，只挡图池，不整号出池。
			cooling:        accountCooling,
			imagineBlocked: blocks[value.ID],
		})
	}

	imageEligible := func(c webPoolCandidate) bool {
		return imagePoolEligible(c, now)
	}
	chatEligible := func(c webPoolCandidate) bool {
		return c.enabled && c.active && !c.cooling && (c.fastRem > 0 || c.autoRem > 0)
	}
	imageIDs := selectWebPoolIDs(candidates, webImagePoolCap, imageEligible, func(a, b webPoolCandidate) bool {
		return imagePoolLess(a, b)
	})
	chatIDs := selectWebPoolIDs(candidates, webChatPoolCap, chatEligible, func(a, b webPoolCandidate) bool {
		if a.priority != b.priority {
			return a.priority > b.priority
		}
		scoreA, scoreB := a.fastRem+a.autoRem, b.fastRem+b.autoRem
		if scoreA != scoreB {
			return scoreA > scoreB
		}
		return a.id < b.id
	})

	// 出池：已启用但 auth 失效 / 账号冷却 / 对话额度耗尽 → disable。绝不自动 enable。
	// Imagine model block 不触发整号 disable（否则 block 到期后无法自动回池）。
	toDisable := make([]uint64, 0)
	enabledNow := make([]uint64, 0)
	for _, candidate := range candidates {
		keep := candidate.enabled && candidate.active && !candidate.cooling && (candidate.fastRem > 0 || candidate.autoRem > 0)
		if candidate.enabled && !keep {
			toDisable = append(toDisable, candidate.id)
			continue
		}
		if keep {
			enabledNow = append(enabledNow, candidate.id)
		}
	}

	disabledFlag := false
	if len(toDisable) > 0 {
		if _, err := s.accounts.UpdateMany(ctx, toDisable, repository.AccountUpdates{Enabled: &disabledFlag}); err != nil {
			return WebPoolSnapshot{}, err
		}
		for _, id := range toDisable {
			_ = s.sticky.DeleteByAccount(ctx, id)
		}
	}

	sort.Slice(enabledNow, func(i, j int) bool { return enabledNow[i] < enabledNow[j] })
	threePools, _ := s.SummarizeWebThreePoolsForSnapshot(ctx)
	return WebPoolSnapshot{
		ImagePoolIDs: imageIDs, ChatPoolIDs: chatIDs, EnabledIDs: enabledNow,
		ImagePoolSize: len(imageIDs), ChatPoolSize: len(chatIDs), EnabledCount: len(enabledNow),
		ImagePoolCap: webImagePoolCap, ChatPoolCap: webChatPoolCap, ReconciledAt: now,
		EnabledAdded: 0, EnabledRemoved: len(toDisable), ThreePools: threePools,
	}, nil
}

// WebPools 返回当前按额度推算的池快照（不改 enabled）。
func (s *Service) WebPools(ctx context.Context) (WebPoolSnapshot, error) {
	now := time.Now().UTC()
	accounts, _, err := s.accounts.List(ctx, repository.AccountListQuery{
		Page:   repository.PageQuery{Offset: 0, Limit: 5000, Sort: repository.SortQuery{Field: "createdAt", Direction: repository.SortAscending}},
		Filter: repository.AccountListFilter{Provider: string(accountdomain.ProviderWeb), Now: now},
	})
	if err != nil {
		return WebPoolSnapshot{}, err
	}
	ids := make([]uint64, 0, len(accounts))
	enabled := make([]uint64, 0)
	for _, value := range accounts {
		ids = append(ids, value.ID)
		if value.Enabled {
			enabled = append(enabled, value.ID)
		}
	}
	windowsByAccount, err := s.accounts.GetQuotaWindows(ctx, ids)
	if err != nil {
		return WebPoolSnapshot{}, err
	}
	blocks, err := s.accounts.GetActiveModelQuotaBlocks(ctx, ids, imagineUpstream, now)
	if err != nil {
		return WebPoolSnapshot{}, err
	}
	modelStates, err := s.accounts.GetModelStates(ctx, ids)
	if err != nil {
		return WebPoolSnapshot{}, err
	}
	candidates := make([]webPoolCandidate, 0, len(accounts))
	for _, value := range accounts {
		fastRem, autoRem := quotaRemaining(windowsByAccount[value.ID], "fast"), quotaRemaining(windowsByAccount[value.ID], "auto")
		accountCooling := value.CooldownUntil != nil && value.CooldownUntil.After(now)
		candidates = append(candidates, webPoolCandidate{
			id: value.ID, priority: value.Priority, fastRem: fastRem, autoRem: autoRem,
			imagineWindow: findQuotaWindow(windowsByAccount[value.ID], "imagine"),
			modelState:    findModelState(modelStates[value.ID], imagineUpstream),
			enabled:       value.Enabled, active: value.AuthStatus == accountdomain.AuthStatusActive,
			cooling: accountCooling, imagineBlocked: blocks[value.ID],
		})
	}
	// 只读快照同样只在当前 enabled 集合内投影，避免把未进池账号显示成调度位。
	imageIDs := selectWebPoolIDs(candidates, webImagePoolCap, func(c webPoolCandidate) bool {
		return imagePoolEligible(c, now)
	}, func(a, b webPoolCandidate) bool {
		return imagePoolLess(a, b)
	})
	chatIDs := selectWebPoolIDs(candidates, webChatPoolCap, func(c webPoolCandidate) bool {
		return c.enabled && c.active && !c.cooling && (c.fastRem > 0 || c.autoRem > 0)
	}, func(a, b webPoolCandidate) bool {
		if a.priority != b.priority {
			return a.priority > b.priority
		}
		scoreA, scoreB := a.fastRem+a.autoRem, b.fastRem+b.autoRem
		if scoreA != scoreB {
			return scoreA > scoreB
		}
		return a.id < b.id
	})
	sort.Slice(enabled, func(i, j int) bool { return enabled[i] < enabled[j] })
	threePools, _ := s.SummarizeWebThreePoolsForSnapshot(ctx)
	return WebPoolSnapshot{
		ImagePoolIDs: imageIDs, ChatPoolIDs: chatIDs, EnabledIDs: enabled,
		ImagePoolSize: len(imageIDs), ChatPoolSize: len(chatIDs), EnabledCount: len(enabled),
		ImagePoolCap: webImagePoolCap, ChatPoolCap: webChatPoolCap, ReconciledAt: now,
		ThreePools: threePools,
	}, nil
}

func quotaRemaining(windows []accountdomain.QuotaWindow, mode string) int {
	for _, window := range windows {
		if window.Mode == mode {
			return window.Remaining
		}
	}
	return 0
}

func findQuotaWindow(windows []accountdomain.QuotaWindow, mode string) *accountdomain.QuotaWindow {
	for index := range windows {
		if windows[index].Mode == mode {
			return &windows[index]
		}
	}
	return nil
}

func findModelState(states []accountdomain.ModelState, upstreamModel string) *accountdomain.ModelState {
	for index := range states {
		if states[index].UpstreamModel == upstreamModel {
			return &states[index]
		}
	}
	return nil
}

func imagePoolEligible(candidate webPoolCandidate, now time.Time) bool {
	if !candidate.enabled || !candidate.active || candidate.cooling {
		return false
	}
	positiveQuota := candidate.imagineWindow != nil && candidate.imagineWindow.Total > 0 && candidate.imagineWindow.Remaining > 0
	if candidate.imagineBlocked && !positiveQuota {
		return false
	}
	if window := candidate.imagineWindow; window != nil && window.Total > 0 {
		if window.Remaining <= 0 {
			return false
		}
		// 新同步到的正额度能够解除旧的 quota_exhausted 结果。
		if candidate.modelState != nil && candidate.modelState.Status == accountdomain.ModelStatusQuotaExhausted {
			return true
		}
	}
	if candidate.modelState == nil {
		return true
	}
	switch candidate.modelState.Status {
	case accountdomain.ModelStatusAuthFailed, accountdomain.ModelStatusSignatureFailed, accountdomain.ModelStatusQuotaExhausted:
		return false
	case accountdomain.ModelStatusSoftStop:
		return candidate.modelState.CooldownUntil != nil && !candidate.modelState.CooldownUntil.After(now)
	default:
		return true
	}
}

func imagePoolLess(a, b webPoolCandidate) bool {
	if a.priority != b.priority {
		return a.priority > b.priority
	}
	rankA, rankB := imagePoolRank(a), imagePoolRank(b)
	if rankA != rankB {
		return rankA > rankB
	}
	remainingA, remainingB := 0, 0
	if a.imagineWindow != nil {
		remainingA = a.imagineWindow.Remaining
	}
	if b.imagineWindow != nil {
		remainingB = b.imagineWindow.Remaining
	}
	if remainingA != remainingB {
		return remainingA > remainingB
	}
	return a.id < b.id
}

func imagePoolRank(candidate webPoolCandidate) int {
	if candidate.modelState != nil && candidate.modelState.Status == accountdomain.ModelStatusAvailable {
		return 2
	}
	if candidate.imagineWindow != nil && candidate.imagineWindow.Total > 0 && candidate.imagineWindow.Remaining > 0 {
		return 1
	}
	if candidate.modelState != nil && candidate.modelState.Status == accountdomain.ModelStatusQuotaAvailable {
		return 1
	}
	return 0
}

func selectWebPoolIDs(candidates []webPoolCandidate, cap int, eligible func(webPoolCandidate) bool, less func(a, b webPoolCandidate) bool) []uint64 {
	filtered := make([]webPoolCandidate, 0, len(candidates))
	for _, candidate := range candidates {
		if eligible(candidate) {
			filtered = append(filtered, candidate)
		}
	}
	sort.SliceStable(filtered, func(i, j int) bool { return less(filtered[i], filtered[j]) })
	if len(filtered) > cap {
		filtered = filtered[:cap]
	}
	ids := make([]uint64, 0, len(filtered))
	for _, candidate := range filtered {
		ids = append(ids, candidate.id)
	}
	return ids
}
