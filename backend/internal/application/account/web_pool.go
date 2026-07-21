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
	id       uint64
	priority int
	fastRem  int
	autoRem  int
	enabled  bool
	active   bool
	cooling  bool
}

// ReconcileWebPools 按额度把 Web 账号拉进/踢出调度池：图池看 fast>0 且无 Imagine block，对话池看 auto/fast>0；各最多 50。
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
		cooling := value.CooldownUntil != nil && value.CooldownUntil.After(now)
		candidates = append(candidates, webPoolCandidate{
			id: value.ID, priority: value.Priority, fastRem: fastRem, autoRem: autoRem,
			enabled: value.Enabled, active: value.AuthStatus == accountdomain.AuthStatusActive,
			cooling: cooling || blocks[value.ID],
		})
	}

	imageIDs := selectWebPoolIDs(candidates, webImagePoolCap, func(c webPoolCandidate) bool {
		return c.active && !c.cooling && c.fastRem > 0
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
		return c.active && !c.cooling && (c.fastRem > 0 || c.autoRem > 0)
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

	desired := make(map[uint64]struct{}, len(imageIDs)+len(chatIDs))
	for _, id := range imageIDs {
		desired[id] = struct{}{}
	}
	for _, id := range chatIDs {
		desired[id] = struct{}{}
	}

	toEnable := make([]uint64, 0)
	toDisable := make([]uint64, 0)
	enabledNow := make([]uint64, 0)
	for _, candidate := range candidates {
		_, want := desired[candidate.id]
		if want && !candidate.enabled {
			toEnable = append(toEnable, candidate.id)
		}
		if !want && candidate.enabled {
			toDisable = append(toDisable, candidate.id)
		}
		if want {
			enabledNow = append(enabledNow, candidate.id)
		}
	}

	enabledFlag := true
	disabledFlag := false
	if len(toEnable) > 0 {
		if _, err := s.accounts.UpdateMany(ctx, toEnable, repository.AccountUpdates{Enabled: &enabledFlag}); err != nil {
			return WebPoolSnapshot{}, err
		}
	}
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
		EnabledAdded: len(toEnable), EnabledRemoved: len(toDisable),
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
		cooling := value.CooldownUntil != nil && value.CooldownUntil.After(now)
		candidates = append(candidates, webPoolCandidate{
			id: value.ID, priority: value.Priority, fastRem: fastRem, autoRem: autoRem,
			enabled: value.Enabled, active: value.AuthStatus == accountdomain.AuthStatusActive,
			cooling: cooling || blocks[value.ID],
		})
	}
	imageIDs := selectWebPoolIDs(candidates, webImagePoolCap, func(c webPoolCandidate) bool {
		return c.active && !c.cooling && c.fastRem > 0
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
		return c.active && !c.cooling && (c.fastRem > 0 || c.autoRem > 0)
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
