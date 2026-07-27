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

func TestImagePoolEligibilityRequiresFreshPositiveImagineQuota(t *testing.T) {
	now := time.Now().UTC()
	synced := now.Add(-5 * time.Minute)
	freshWindow := func(total, remaining int) *accountdomain.QuotaWindow {
		return &accountdomain.QuotaWindow{
			Mode: "imagine", Total: total, Remaining: remaining,
			SyncedAt: &synced, Source: accountdomain.QuotaSourceUpstream, UpdatedAt: synced,
		}
	}
	tests := []struct {
		name      string
		candidate webPoolCandidate
		want      bool
	}{
		{name: "zero over zero is blocked", candidate: webPoolCandidate{enabled: true, active: true, imagineWindow: freshWindow(0, 0)}, want: false},
		{name: "zero over zero quota available awaits probe", candidate: webPoolCandidate{
			enabled: true, active: true, imagineWindow: freshWindow(0, 0),
			modelState: &accountdomain.ModelState{Status: accountdomain.ModelStatusQuotaAvailable},
		}, want: true},
		{name: "zero over zero recent lite success", candidate: webPoolCandidate{
			enabled: true, active: true, imagineWindow: freshWindow(0, 0),
			modelState: &accountdomain.ModelState{Status: accountdomain.ModelStatusAvailable, LastSuccessAt: timePointer(now.Add(-10 * time.Minute))},
		}, want: true},
		{name: "known zero is exhausted", candidate: webPoolCandidate{enabled: true, active: true, imagineWindow: freshWindow(10, 0)}, want: false},
		{name: "known positive is available", candidate: webPoolCandidate{enabled: true, active: true, imagineWindow: freshWindow(10, 3)}, want: true},
		{name: "stale positive with unknown state is blocked", candidate: webPoolCandidate{
			enabled: true, active: true,
			imagineWindow: &accountdomain.QuotaWindow{
				Mode: "imagine", Total: 10, Remaining: 3,
				SyncedAt: timePointer(now.Add(-2 * time.Hour)), Source: accountdomain.QuotaSourceUpstream,
				UpdatedAt: now.Add(-2 * time.Hour),
			},
			modelState: &accountdomain.ModelState{Status: accountdomain.ModelStatusUnknown},
		}, want: false},
		{name: "signature failure is unavailable", candidate: webPoolCandidate{enabled: true, active: true, modelState: &accountdomain.ModelState{Status: accountdomain.ModelStatusSignatureFailed}}, want: false},
		{name: "old quota exhaustion cleared by positive refresh", candidate: webPoolCandidate{enabled: true, active: true, imagineBlocked: true, imagineWindow: freshWindow(10, 3), modelState: &accountdomain.ModelState{Status: accountdomain.ModelStatusQuotaExhausted}}, want: true},
		{name: "active soft stop is unavailable", candidate: webPoolCandidate{enabled: true, active: true, modelState: &accountdomain.ModelState{Status: accountdomain.ModelStatusSoftStop, CooldownUntil: timePointer(now.Add(time.Minute))}}, want: false},
		{name: "dispatch requires available state", candidate: webPoolCandidate{
			enabled: true, active: true, imagineWindow: freshWindow(10, 3),
			modelState: &accountdomain.ModelState{Status: accountdomain.ModelStatusAvailable},
		}, want: true},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := imagePoolEligible(test.candidate, now); got != test.want {
				t.Fatalf("eligible = %v, want %v", got, test.want)
			}
		})
	}
}

func TestImageDispatchAdmissibleStricterThanEligible(t *testing.T) {
	now := time.Now().UTC()
	synced := now.Add(-5 * time.Minute)
	fresh := &accountdomain.QuotaWindow{
		Mode: "imagine", Total: 10, Remaining: 3,
		SyncedAt: &synced, Source: accountdomain.QuotaSourceUpstream, UpdatedAt: synced,
	}
	exhausted := webPoolCandidate{
		enabled: true, active: true, imagineBlocked: true, imagineWindow: fresh,
		modelState: &accountdomain.ModelState{Status: accountdomain.ModelStatusQuotaExhausted},
	}
	if !imagePoolEligible(exhausted, now) {
		t.Fatal("eligible should allow refreshed exhaustion")
	}
	if imageDispatchAdmissible(exhausted, now) {
		t.Fatal("dispatch must reject quota_exhausted even with fresh window")
	}
	available := webPoolCandidate{
		enabled: true, active: true, imagineWindow: fresh,
		modelState: &accountdomain.ModelState{Status: accountdomain.ModelStatusAvailable},
	}
	if !imageDispatchAdmissible(available, now) {
		t.Fatal("dispatch should accept available + fresh quota")
	}
	unknownRecent := webPoolCandidate{
		enabled: true, active: true,
		imagineWindow: &accountdomain.QuotaWindow{
			Mode: "imagine", Total: 0, Remaining: 0,
			SyncedAt: &synced, Source: accountdomain.QuotaSourceUpstream, UpdatedAt: synced,
		},
		modelState: &accountdomain.ModelState{
			Status: accountdomain.ModelStatusAvailable, LastSuccessAt: timePointer(now.Add(-10 * time.Minute)),
		},
	}
	if !imageDispatchAdmissible(unknownRecent, now) {
		t.Fatal("dispatch should accept unknown gate with recent lite success")
	}
}

func timePointer(value time.Time) *time.Time { return &value }
