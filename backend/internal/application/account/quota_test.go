package account

import (
	"testing"
	"time"

	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
)

func TestNewQuotaViewFreeUsesObservedRollingTokens(t *testing.T) {
	quota := newQuotaView(&accountdomain.Billing{IsUnifiedBillingUser: true}, 250_000, nil, "grok-4.5-build-free")
	if quota.Type != QuotaTypeFree || quota.Unit != "tokens" || quota.Limit != 1_000_000 || quota.LimitKnown || quota.Confidence != "observed" {
		t.Fatalf("quota = %#v", quota)
	}
	if quota.Used != 250_000 || quota.Remaining != 750_000 || quota.UsagePercent != 25 || quota.WindowHours != 24 || !quota.Observed {
		t.Fatalf("quota = %#v", quota)
	}
}

func TestNewQuotaViewPaidUsesMonthlyBilling(t *testing.T) {
	quota := newQuotaView(&accountdomain.Billing{MonthlyLimit: 200, Used: 50, BillingPeriodStart: "start", BillingPeriodEnd: "end"}, 900_000, nil, "")
	if quota.Type != QuotaTypePaid || quota.Unit != "credits" || quota.Limit != 200 {
		t.Fatalf("quota = %#v", quota)
	}
	if quota.Used != 50 || quota.Remaining != 150 || quota.UsagePercent != 25 || quota.Observed || !quota.LimitKnown {
		t.Fatalf("quota = %#v", quota)
	}
}

func TestNewQuotaViewPaidShowsBillingProbeState(t *testing.T) {
	now := time.Now().UTC()
	next := now.Add(time.Hour)
	quota := newQuotaView(&accountdomain.Billing{MonthlyLimit: 100, Used: 100}, 0, &accountdomain.QuotaRecovery{
		Kind: accountdomain.QuotaRecoveryKindPaid, Status: accountdomain.QuotaRecoveryStatusExhausted,
		ExhaustedAt: &now, NextProbeAt: &next,
	}, "")
	if quota.Type != QuotaTypePaid || quota.Status != QuotaStatusWaitingReset || quota.NextProbeAt == nil {
		t.Fatalf("quota = %#v", quota)
	}
}

func TestNewQuotaViewUnknownWithoutBillingSnapshot(t *testing.T) {
	quota := newQuotaView(nil, 100, nil, "")
	if quota.Type != QuotaTypeUnknown {
		t.Fatalf("quota = %#v", quota)
	}
}

func TestNewQuotaViewEstimatesFreeFromObservedZeroBillingProfile(t *testing.T) {
	quota := newQuotaView(&accountdomain.Billing{IsUnifiedBillingUser: true, TopUpMethod: "TOP_UP_METHOD_SAVED_PAYMENT_METHOD"}, 100, nil, "")
	if quota.Type != QuotaTypeFree || quota.Source != "billingProfile" || quota.Confidence != "estimated" || quota.Limit != 1_000_000 || quota.LimitKnown {
		t.Fatalf("quota = %#v", quota)
	}
}

func TestNewQuotaViewUsesConfirmedExhaustion(t *testing.T) {
	now := time.Now().UTC()
	next := now.Add(24 * time.Hour)
	quota := newQuotaView(&accountdomain.Billing{}, 250_000, &accountdomain.QuotaRecovery{
		Status: accountdomain.QuotaRecoveryStatusExhausted, ConfirmedUsed: 1_065_387,
		ConfirmedLimit: 1_000_000, ExhaustedAt: &now, NextProbeAt: &next, LastConfirmedAt: &now,
	}, "")
	if quota.Type != QuotaTypeFree || quota.Status != QuotaStatusWaitingReset || !quota.Confirmed {
		t.Fatalf("quota = %#v", quota)
	}
	if quota.Used != 1_065_387 || quota.Limit != 1_000_000 || quota.Remaining != 0 || quota.NextProbeAt == nil || !quota.LimitKnown {
		t.Fatalf("quota = %#v", quota)
	}
}

func TestModelStatesForViewSynthesizesImagineQuotaState(t *testing.T) {
	now := time.Now().UTC()
	credential := accountdomain.Credential{ID: 7, Provider: accountdomain.ProviderWeb}
	tests := []struct {
		name   string
		window accountdomain.QuotaWindow
		stored []accountdomain.ModelState
		want   accountdomain.ModelStatus
		reason string
	}{
		{name: "zero over zero stays unknown", window: accountdomain.QuotaWindow{Mode: "imagine", Total: 0, Remaining: 0, UpdatedAt: now}, want: accountdomain.ModelStatusUnknown, reason: "quota_limit_unknown"},
		{name: "known remaining is quota available", window: accountdomain.QuotaWindow{Mode: "imagine", Total: 10, Remaining: 4, UpdatedAt: now}, want: accountdomain.ModelStatusQuotaAvailable, reason: "quota_remaining_positive"},
		{name: "known zero is exhausted", window: accountdomain.QuotaWindow{Mode: "imagine", Total: 10, Remaining: 0, UpdatedAt: now}, stored: []accountdomain.ModelState{{AccountID: 7, UpstreamModel: imagineUpstreamModel, Status: accountdomain.ModelStatusAvailable}}, want: accountdomain.ModelStatusQuotaExhausted, reason: "quota_remaining_zero"},
		{name: "fresh quota clears old exhaustion", window: accountdomain.QuotaWindow{Mode: "imagine", Total: 10, Remaining: 10, UpdatedAt: now}, stored: []accountdomain.ModelState{{AccountID: 7, UpstreamModel: imagineUpstreamModel, Status: accountdomain.ModelStatusQuotaExhausted}}, want: accountdomain.ModelStatusQuotaAvailable, reason: "quota_remaining_positive"},
		{name: "actual success outranks positive quota", window: accountdomain.QuotaWindow{Mode: "imagine", Total: 10, Remaining: 8, UpdatedAt: now}, stored: []accountdomain.ModelState{{AccountID: 7, UpstreamModel: imagineUpstreamModel, Status: accountdomain.ModelStatusAvailable, Reason: "image_generated"}}, want: accountdomain.ModelStatusAvailable, reason: "image_generated"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			states := modelStatesForView(credential, []accountdomain.QuotaWindow{test.window}, test.stored)
			if len(states) != 1 || states[0].Status != test.want || states[0].Reason != test.reason {
				t.Fatalf("states = %#v, want status=%s reason=%s", states, test.want, test.reason)
			}
		})
	}
}

func TestModelStatesForViewLeavesNonWebStatesUnchanged(t *testing.T) {
	stored := []accountdomain.ModelState{{AccountID: 9, UpstreamModel: "grok-4.5", Status: accountdomain.ModelStatusAvailable}}
	states := modelStatesForView(accountdomain.Credential{ID: 9, Provider: accountdomain.ProviderBuild}, nil, stored)
	if len(states) != 1 || states[0] != stored[0] {
		t.Fatalf("states = %#v", states)
	}
}
