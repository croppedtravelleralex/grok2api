package account

import (
	"context"
	"sort"

	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
	"github.com/chenyme/grok2api/backend/internal/repository"
)

type WebLaneQuotaSummary struct {
	EnabledAccounts int `json:"enabledAccounts"`
	ChatRemaining   int `json:"chatRemaining"`
	ChatTotal       int `json:"chatTotal"`
	ChatKnownAccounts int `json:"chatKnownAccounts"`
	ImageBookRemaining int `json:"imageBookRemaining"`
	ImageBookTotal     int `json:"imageBookTotal"`
	ImageBookAccounts  int `json:"imageBookAccounts"`
	ImageSchedulableRemaining int `json:"imageSchedulableRemaining"`
	ImageSchedulableTotal     int `json:"imageSchedulableTotal"`
	ImageSchedulableAccounts  int `json:"imageSchedulableAccounts"`
	ImageNoCapabilityAccounts   int `json:"imageNoCapabilityAccounts"`
}

func (s *Service) WebLaneQuotaSummary(ctx context.Context) (WebLaneQuotaSummary, error) {
	value, err := s.accounts.SummarizeWebLaneQuota(ctx)
	if err != nil {
		return WebLaneQuotaSummary{}, mapRepositoryError(err)
	}
	out := webLaneQuotaFromRepository(value)
	dispatchSchedulable, err := s.summarizeImageDispatchSchedulableQuota(ctx)
	if err != nil {
		return WebLaneQuotaSummary{}, mapRepositoryError(err)
	}
	out.ImageSchedulableRemaining = dispatchSchedulable.remaining
	out.ImageSchedulableTotal = dispatchSchedulable.total
	out.ImageSchedulableAccounts = dispatchSchedulable.accounts
	return out, nil
}

// imageDispatchIDsWithGenerations 返回四池 dispatch 中、新鲜上游额度且生图次数 > 0 的账号。
func (s *Service) imageDispatchIDsWithGenerations(ctx context.Context) ([]uint64, error) {
	_, _, dispatchIDs, _, err := s.summarizeWebPools(ctx)
	if err != nil {
		return nil, err
	}
	if len(dispatchIDs) == 0 {
		return nil, nil
	}
	windowsByAccount, err := s.accounts.GetQuotaWindows(ctx, dispatchIDs)
	if err != nil {
		return nil, err
	}
	now := s.now()
	out := make([]uint64, 0, len(dispatchIDs))
	for _, id := range dispatchIDs {
		imagine := findQuotaWindow(windowsByAccount[id], "imagine")
		if !imagineQuotaFresh(imagine, now) {
			continue
		}
		if gens, ok := accountdomain.ImagineGenerations(imagine.Remaining, imagine.Total); !ok || gens <= 0 {
			continue
		}
		out = append(out, id)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out, nil
}

type imageDispatchSchedulableQuota struct {
	remaining int
	total     int
	accounts  int
}

// summarizeImageDispatchSchedulableQuota 仅汇总图轨四池 dispatch 账号的新鲜 Imagine 可生图次数。
func (s *Service) summarizeImageDispatchSchedulableQuota(ctx context.Context) (imageDispatchSchedulableQuota, error) {
	dispatchIDs, err := s.imageDispatchIDsWithGenerations(ctx)
	if err != nil {
		return imageDispatchSchedulableQuota{}, err
	}
	if len(dispatchIDs) == 0 {
		return imageDispatchSchedulableQuota{}, nil
	}
	windowsByAccount, err := s.accounts.GetQuotaWindows(ctx, dispatchIDs)
	if err != nil {
		return imageDispatchSchedulableQuota{}, err
	}
	now := s.now()
	var out imageDispatchSchedulableQuota
	for _, id := range dispatchIDs {
		imagine := findQuotaWindow(windowsByAccount[id], "imagine")
		if !imagineQuotaFresh(imagine, now) {
			continue
		}
		if remaining, ok := accountdomain.ImagineGenerations(imagine.Remaining, imagine.Total); ok {
			out.remaining += remaining
		}
		if total, ok := accountdomain.ImagineGenerationsTotal(imagine.Total); ok {
			out.total += total
		}
		out.accounts++
	}
	return out, nil
}

func webLaneQuotaFromRepository(value repository.WebLaneQuotaSummary) WebLaneQuotaSummary {
	return WebLaneQuotaSummary{
		EnabledAccounts:           value.EnabledAccounts,
		ChatRemaining:             value.ChatRemaining,
		ChatTotal:                 value.ChatTotal,
		ChatKnownAccounts:         value.ChatKnownAccounts,
		ImageBookRemaining:        value.ImageBookRemaining,
		ImageBookTotal:            value.ImageBookTotal,
		ImageBookAccounts:         value.ImageBookAccounts,
		ImageSchedulableRemaining: value.ImageSchedulableRemaining,
		ImageSchedulableTotal:     value.ImageSchedulableTotal,
		ImageSchedulableAccounts:  value.ImageSchedulableAccounts,
		ImageNoCapabilityAccounts: value.ImageNoCapabilityAccounts,
	}
}
