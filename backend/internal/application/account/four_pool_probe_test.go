package account

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
	"github.com/chenyme/grok2api/backend/internal/infra/persistence/relational"
)

func TestRebuildBuildPoolIndexOrdersDispatchByBillingQuota(t *testing.T) {
	database, err := relational.OpenSQLite(context.Background(), filepath.Join(t.TempDir(), "dispatch-quota.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = database.Close() })
	if err := database.InitializeSchema(context.Background()); err != nil {
		t.Fatal(err)
	}
	repository := relational.NewAccountRepository(database)
	service := NewService(repository, nil, nil, nil, nil, nil, nil)

	low := createDispatchProbeAccount(t, repository, "low", 10)
	high := createDispatchProbeAccount(t, repository, "high", 10)
	now := time.Now().UTC()
	if err := repository.SaveBilling(context.Background(), accountdomain.Billing{
		AccountID: low.ID, MonthlyLimit: 100, Used: 90, SyncedAt: now,
	}); err != nil {
		t.Fatal(err)
	}
	if err := repository.SaveBilling(context.Background(), accountdomain.Billing{
		AccountID: high.ID, MonthlyLimit: 100, Used: 10, SyncedAt: now,
	}); err != nil {
		t.Fatal(err)
	}
	if err := service.RebuildBuildPoolIndex(context.Background()); err != nil {
		t.Fatal(err)
	}
	ids := service.OrderedDispatchIDs(10)
	if len(ids) != 2 || ids[0] != high.ID || ids[1] != low.ID {
		t.Fatalf("dispatch order = %v want [%d %d]", ids, high.ID, low.ID)
	}
}

func createDispatchProbeAccount(t *testing.T, repository *relational.AccountRepository, source string, priority int) accountdomain.Credential {
	t.Helper()
	credential, _, err := repository.UpsertByIdentity(context.Background(), accountdomain.Credential{
		Provider: accountdomain.ProviderBuild, AuthType: accountdomain.AuthTypeOAuth, Name: source, SourceKey: source,
		EncryptedAccessToken: "encrypted", ExpiresAt: time.Now().Add(time.Hour), Enabled: true,
		AuthStatus: accountdomain.AuthStatusActive, ObservedModel: "grok-4.5-build-free", Priority: priority,
	})
	if err != nil {
		t.Fatal(err)
	}
	return credential
}
