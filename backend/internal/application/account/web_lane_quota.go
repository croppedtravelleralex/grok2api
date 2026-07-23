package account

import (
	"context"

	"github.com/chenyme/grok2api/backend/internal/repository"
)

type WebLaneQuotaSummary struct {
	EnabledAccounts      int `json:"enabledAccounts"`
	ChatRemaining        int `json:"chatRemaining"`
	ChatTotal            int `json:"chatTotal"`
	ChatKnownAccounts    int `json:"chatKnownAccounts"`
	ImageRemaining       int `json:"imageRemaining"`
	ImageTotal           int `json:"imageTotal"`
	ImageKnownAccounts   int `json:"imageKnownAccounts"`
	ImageUnknownAccounts int `json:"imageUnknownAccounts"`
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
		EnabledAccounts:      value.EnabledAccounts,
		ChatRemaining:        value.ChatRemaining,
		ChatTotal:            value.ChatTotal,
		ChatKnownAccounts:    value.ChatKnownAccounts,
		ImageRemaining:       value.ImageRemaining,
		ImageTotal:           value.ImageTotal,
		ImageKnownAccounts:   value.ImageKnownAccounts,
		ImageUnknownAccounts: value.ImageUnknownAccounts,
	}
}
