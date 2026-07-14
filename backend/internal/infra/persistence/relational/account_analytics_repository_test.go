package relational

import (
	"context"
	"testing"
	"time"

	"github.com/chenyme/grok2api/backend/internal/domain/account"
)

func TestAccountAnalyticsCapturesStatusTypeTierAndQuotaHistory(t *testing.T) {
	ctx := context.Background()
	repository := NewAccountRepository(openTestDatabase(t))
	now := time.Date(2026, 7, 14, 10, 7, 0, 0, time.UTC)

	build, _, err := repository.UpsertByIdentity(ctx, account.Credential{
		Provider: account.ProviderBuild, AuthType: account.AuthTypeOAuth, Name: "paid-build", SourceKey: "paid-build",
		EncryptedAccessToken: testEncryptedToken, AuthStatus: account.AuthStatusActive,
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := repository.SaveBilling(ctx, account.Billing{AccountID: build.ID, MonthlyLimit: 100, Used: 25, SyncedAt: now}); err != nil {
		t.Fatal(err)
	}
	web, _, err := repository.UpsertByIdentity(ctx, account.Credential{
		Provider: account.ProviderWeb, AuthType: account.AuthTypeSSO, WebTier: account.WebTierSuper, Name: "web-super", SourceKey: "web-super",
		EncryptedAccessToken: testEncryptedToken, AuthStatus: account.AuthStatusActive,
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := repository.SaveQuotaWindows(ctx, web.ID, account.WebTierSuper, now, []account.QuotaWindow{{AccountID: web.ID, Mode: "weekly", Remaining: 60, Total: 100, SyncedAt: &now, UpdatedAt: now}}); err != nil {
		t.Fatal(err)
	}

	created, err := repository.CaptureAccountPoolSnapshots(ctx, now)
	if err != nil {
		t.Fatal(err)
	}
	if len(created) != 3 {
		t.Fatalf("snapshots = %#v", created)
	}
	rows, err := repository.ListAccountPoolSnapshots(ctx, now.Add(-time.Hour), now.Add(time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	byProvider := make(map[account.Provider]struct {
		Total, Available, Paid, TierSuper int64
		QuotaRemaining, QuotaTotal        float64
	}, len(rows))
	for _, row := range rows {
		byProvider[row.Provider] = struct {
			Total, Available, Paid, TierSuper int64
			QuotaRemaining, QuotaTotal        float64
		}{
			Total: row.Total, Available: row.Available, Paid: row.Paid, TierSuper: row.TierSuper,
			QuotaRemaining: row.QuotaRemaining, QuotaTotal: row.QuotaTotal,
		}
	}
	if row := byProvider[account.ProviderBuild]; row.Total != 1 || row.Available != 1 || row.Paid != 1 || row.QuotaRemaining != 75 || row.QuotaTotal != 100 {
		t.Fatalf("build snapshot = %#v", row)
	}
	if row := byProvider[account.ProviderWeb]; row.Total != 1 || row.Available != 1 || row.TierSuper != 1 || row.QuotaRemaining != 60 || row.QuotaTotal != 100 {
		t.Fatalf("web snapshot = %#v", row)
	}
	if !rows[0].BucketAt.Equal(now.Truncate(15 * time.Minute)) {
		t.Fatalf("bucket = %s", rows[0].BucketAt)
	}
}
