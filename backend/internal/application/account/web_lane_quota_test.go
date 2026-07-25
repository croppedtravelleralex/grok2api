package account

import (
	"testing"
	"time"

	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
)

func TestSummarizeImageDispatchSchedulableQuotaSumsDispatchOnly(t *testing.T) {
	now := time.Date(2026, 7, 25, 10, 0, 0, 0, time.UTC)
	synced := now.Add(-5 * time.Minute)
	fresh := &accountdomain.QuotaWindow{
		Mode: "imagine", Total: 10, Remaining: 3,
		SyncedAt: &synced, Source: accountdomain.QuotaSourceUpstream, UpdatedAt: synced,
	}
	stale := &accountdomain.QuotaWindow{
		Mode: "imagine", Total: 10, Remaining: 8,
		SyncedAt: timePointer(now.Add(-2 * time.Hour)), Source: accountdomain.QuotaSourceUpstream,
		UpdatedAt: now.Add(-2 * time.Hour),
	}
	windows := map[uint64][]accountdomain.QuotaWindow{
		101: {*fresh},
		102: {*stale},
		103: {*fresh},
	}
	dispatchIDs := []uint64{101, 102, 103, 999}
	var out imageDispatchSchedulableQuota
	for _, id := range dispatchIDs {
		imagine := findQuotaWindow(windows[id], "imagine")
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
	if out.accounts != 2 || out.remaining != 6 || out.total != 20 {
		t.Fatalf("quota=%+v want accounts=2 remaining=6 total=20", out)
	}
}
