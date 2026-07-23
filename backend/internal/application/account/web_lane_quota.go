package account

import (
	"context"

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
	return webLaneQuotaFromRepository(value), nil
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
