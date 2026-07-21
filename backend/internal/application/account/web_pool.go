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
}

type webPoolCandidate struct {
	id             uint64
	priority       int
	fastRem        int
	autoRem        int
	enabled        bool
	active         bool
	cooling        bool // 账号级 cooldown_until
	imagineBlocked bool // grok-imagine-image model block（只挡图池）
}

// ReconcileWebPools 只做「无额度/冷却出池」：在当前已启用账号里踢掉不合格号。
// 不自动 enable 任何新号（进池仍由人工/调度脚本控制），避免把未验证出口能力的账号拉进生产池。
// 图池看 fast>0 且无 Imagine block；对话池看 auto/fast>0；快照各最多 50。
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

	candidates := make([]webPoolCandidate, 0, len(accounts))
	for _, value := range accounts {
		fastRem, autoRem := quotaRemaining(windowsByAccount[value.ID], "fast"), quotaRemaining(windowsByAccount[value.ID], "auto")
		accountCooling := value.CooldownUntil != nil && value.CooldownUntil.After(now)
		candidates = append(candidates, webPoolCandidate{
			id: value.ID, priority: value.Priority, fastRem: fastRem, autoRem: autoRem,
			enabled: value.Enabled, active: value.AuthStatus == accountdomain.AuthStatusActive,
			// cooling=账号级冷却；Imagine model block 单独看，只挡图池，不整号出池。
			cooling: accountCooling,
			imagineBlocked: blocks[value.ID],
		})
	}

	imageEligible := func(c webPoolCandidate) bool {
		return c.enabled && c.active && !c.cooling && !c.imagineBlocked && c.fastRem > 0
	}
	chatEligible := func(c webPoolCandidate) bool {
		return c.enabled && c.active && !c.cooling && (c.fastRem > 0 || c.autoRem > 0)
	}
	imageIDs := selectWebPoolIDs(candidates, webImagePoolCap, imageEligible, func(a, b webPoolCandidate) bool {
		if a.priority != b.priority {
			return a.priority > b.priority
		}
		if a.fastRem != b.fastRem {
			return a.fastRem > b.fastRem
		}
		return a.id < b.id
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
	return WebPoolSnapshot{
		ImagePoolIDs: imageIDs, ChatPoolIDs: chatIDs, EnabledIDs: enabledNow,
		ImagePoolSize: len(imageIDs), ChatPoolSize: len(chatIDs), EnabledCount: len(enabledNow),
		ImagePoolCap: webImagePoolCap, ChatPoolCap: webChatPoolCap, ReconciledAt: now,
		EnabledAdded: 0, EnabledRemoved: len(toDisable),
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
	candidates := make([]webPoolCandidate, 0, len(accounts))
	for _, value := range accounts {
		fastRem, autoRem := quotaRemaining(windowsByAccount[value.ID], "fast"), quotaRemaining(windowsByAccount[value.ID], "auto")
		accountCooling := value.CooldownUntil != nil && value.CooldownUntil.After(now)
		candidates = append(candidates, webPoolCandidate{
			id: value.ID, priority: value.Priority, fastRem: fastRem, autoRem: autoRem,
			enabled: value.Enabled, active: value.AuthStatus == accountdomain.AuthStatusActive,
			cooling: accountCooling, imagineBlocked: blocks[value.ID],
		})
	}
	// 只读快照同样只在当前 enabled 集合内投影，避免把未进池账号显示成调度位。
	imageIDs := selectWebPoolIDs(candidates, webImagePoolCap, func(c webPoolCandidate) bool {
		return c.enabled && c.active && !c.cooling && !c.imagineBlocked && c.fastRem > 0
	}, func(a, b webPoolCandidate) bool {
		if a.priority != b.priority {
			return a.priority > b.priority
		}
		if a.fastRem != b.fastRem {
			return a.fastRem > b.fastRem
		}
		return a.id < b.id
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
	return WebPoolSnapshot{
		ImagePoolIDs: imageIDs, ChatPoolIDs: chatIDs, EnabledIDs: enabled,
		ImagePoolSize: len(imageIDs), ChatPoolSize: len(chatIDs), EnabledCount: len(enabled),
		ImagePoolCap: webImagePoolCap, ChatPoolCap: webChatPoolCap, ReconciledAt: now,
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
