package account

import (
	"testing"
	"time"

	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
)

func TestSelectWebPoolIDsRespectsCapAndOrdering(t *testing.T) {
	candidates := []webPoolCandidate{
		{id: 1, priority: 1, fastRem: 10},
		{id: 2, priority: 5, fastRem: 1},
		{id: 3, priority: 5, fastRem: 9},
		{id: 4, priority: 0, fastRem: 30},
	}
	ids := selectWebPoolIDs(candidates, 2, func(c webPoolCandidate) bool { return c.fastRem > 0 }, func(a, b webPoolCandidate) bool {
		if a.priority != b.priority {
			return a.priority > b.priority
		}
		if a.fastRem != b.fastRem {
			return a.fastRem > b.fastRem
		}
		return a.id < b.id
	})
	if len(ids) != 2 || ids[0] != 3 || ids[1] != 2 {
		t.Fatalf("ids=%v want [3 2]", ids)
	}
}

func TestImagePoolEligibilityKeepsUnknownImagineQuotaRoutable(t *testing.T) {
	now := time.Now().UTC()
	tests := []struct {
		name      string
		candidate webPoolCandidate
		want      bool
	}{
		{name: "zero over zero is unknown", candidate: webPoolCandidate{enabled: true, active: true, imagineWindow: &accountdomain.QuotaWindow{Total: 0, Remaining: 0}}, want: true},
		{name: "known zero is exhausted", candidate: webPoolCandidate{enabled: true, active: true, imagineWindow: &accountdomain.QuotaWindow{Total: 10, Remaining: 0}}, want: false},
		{name: "known positive is available", candidate: webPoolCandidate{enabled: true, active: true, imagineWindow: &accountdomain.QuotaWindow{Total: 10, Remaining: 3}}, want: true},
		{name: "signature failure is unavailable", candidate: webPoolCandidate{enabled: true, active: true, modelState: &accountdomain.ModelState{Status: accountdomain.ModelStatusSignatureFailed}}, want: false},
		{name: "old quota exhaustion cleared by positive refresh", candidate: webPoolCandidate{enabled: true, active: true, imagineBlocked: true, imagineWindow: &accountdomain.QuotaWindow{Total: 10, Remaining: 3}, modelState: &accountdomain.ModelState{Status: accountdomain.ModelStatusQuotaExhausted}}, want: true},
		{name: "active soft stop is unavailable", candidate: webPoolCandidate{enabled: true, active: true, modelState: &accountdomain.ModelState{Status: accountdomain.ModelStatusSoftStop, CooldownUntil: timePointer(now.Add(time.Minute))}}, want: false},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := imagePoolEligible(test.candidate, now); got != test.want {
				t.Fatalf("eligible = %v, want %v", got, test.want)
			}
		})
	}
}

func timePointer(value time.Time) *time.Time { return &value }
