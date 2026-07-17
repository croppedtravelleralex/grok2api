package account

import (
	"context"
	"io"
	"net/http"
	"path/filepath"
	"strings"
	"testing"
	"time"

	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
	"github.com/chenyme/grok2api/backend/internal/infra/persistence/relational"
	"github.com/chenyme/grok2api/backend/internal/infra/provider"
)

func TestProbeNextBuildChatVerifiesSuccessfulAccount(t *testing.T) {
	service, repository := newBuildChatProbeService(t, http.StatusOK, `{"id":"resp_1","model":"grok-4.5-build-free"}`)
	credential := createBuildProbeAccount(t, repository, "success")

	accountID, found, err := service.ProbeNextBuildChat(context.Background())
	if err != nil || !found || accountID != credential.ID {
		t.Fatalf("account=%d found=%v err=%v", accountID, found, err)
	}
	updated, err := repository.Get(context.Background(), credential.ID)
	if err != nil {
		t.Fatal(err)
	}
	if updated.ObservedModel != "grok-4.5-build-free" || updated.AuthStatus != accountdomain.AuthStatusActive {
		t.Fatalf("updated = %#v", updated)
	}
}

func TestProbeNextBuildChatQuarantinesPermissionDeniedAccount(t *testing.T) {
	service, repository := newBuildChatProbeService(t, http.StatusForbidden, `{"error":{"code":"permission-denied","message":"Access to the chat endpoint is denied"}}`)
	credential := createBuildProbeAccount(t, repository, "denied")

	accountID, found, err := service.ProbeNextBuildChat(context.Background())
	if err == nil || !found || accountID != credential.ID {
		t.Fatalf("account=%d found=%v err=%v", accountID, found, err)
	}
	updated, err := repository.Get(context.Background(), credential.ID)
	if err != nil {
		t.Fatal(err)
	}
	if updated.AuthStatus != accountdomain.AuthStatusReauthRequired || !strings.Contains(updated.LastError, "chat endpoint access denied") {
		t.Fatalf("updated = %#v", updated)
	}
}

func TestProbeNextBuildChatSweepsVerificationPoolInIDOrder(t *testing.T) {
	database, err := relational.OpenSQLite(context.Background(), filepath.Join(t.TempDir(), "build-probe-order.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = database.Close() })
	if err := database.InitializeSchema(context.Background()); err != nil {
		t.Fatal(err)
	}
	repository := relational.NewAccountRepository(database)
	adapter := &buildChatOrderedProbeAdapter{}
	service := NewService(repository, nil, nil, nil, provider.NewRegistry(adapter), nil, nil)
	first := createBuildProbeAccount(t, repository, "first")
	second := createBuildProbeAccount(t, repository, "second")

	accountID, found, err := service.ProbeNextBuildChat(context.Background())
	if err == nil || !found || accountID != first.ID {
		t.Fatalf("first probe account=%d found=%v err=%v", accountID, found, err)
	}
	accountID, found, err = service.ProbeNextBuildChat(context.Background())
	if err != nil || !found || accountID != second.ID {
		t.Fatalf("second probe account=%d found=%v err=%v", accountID, found, err)
	}
	if got := strings.Join(adapter.seen, ","); got != "first,second" {
		t.Fatalf("probe order = %s", got)
	}
}

func TestProbeNextBuildChatRecoversQuarantinedAccountByRefreshingRT(t *testing.T) {
	service, repository, adapter := newBuildChatRecoveryService(t)
	credential := createBuildProbeAccount(t, repository, "recover-with-rt")
	credential.EncryptedRefreshToken = "refresh-old"
	if _, err := repository.Update(context.Background(), credential); err != nil {
		t.Fatal(err)
	}
	if err := service.MarkReauthRequired(context.Background(), credential.ID, "grok_build chat endpoint access denied"); err != nil {
		t.Fatal(err)
	}

	accountID, found, err := service.ProbeNextBuildChat(context.Background())
	if err != nil || !found || accountID != credential.ID {
		t.Fatalf("account=%d found=%v err=%v", accountID, found, err)
	}
	updated, err := repository.Get(context.Background(), credential.ID)
	if err != nil {
		t.Fatal(err)
	}
	if adapter.refreshCount != 1 || updated.AuthStatus != accountdomain.AuthStatusActive || updated.ObservedModel != "grok-4.5-build-free" || updated.FailureCount != 0 || !updated.Enabled {
		t.Fatalf("refreshes=%d updated=%#v", adapter.refreshCount, updated)
	}
}

func TestProbeNextBuildChatBacksOffAndSoftRetiresUnrecoverableAccount(t *testing.T) {
	service, repository := newBuildChatProbeService(t, http.StatusForbidden, `{"error":{"code":"permission-denied","message":"Access to the chat endpoint is denied"}}`)
	now := time.Date(2026, 7, 15, 12, 0, 0, 0, time.UTC)
	service.now = func() time.Time { return now }
	credential := createBuildProbeAccount(t, repository, "retire-after-recovery")
	if err := service.MarkReauthRequired(context.Background(), credential.ID, "grok_build chat endpoint access denied"); err != nil {
		t.Fatal(err)
	}

	for attempt := 1; attempt <= buildRecoveryMaxAttempts; attempt++ {
		_, found, err := service.ProbeNextBuildChat(context.Background())
		if err == nil || !found {
			t.Fatalf("attempt %d found=%v err=%v", attempt, found, err)
		}
		updated, getErr := repository.Get(context.Background(), credential.ID)
		if getErr != nil {
			t.Fatal(getErr)
		}
		if attempt < buildRecoveryMaxAttempts {
			if !updated.Enabled || updated.FailureCount != attempt || updated.CooldownUntil == nil || !updated.CooldownUntil.After(now) {
				t.Fatalf("attempt %d updated=%#v", attempt, updated)
			}
			now = updated.CooldownUntil.Add(time.Second)
			continue
		}
		if updated.Enabled || updated.AuthStatus != accountdomain.AuthStatusReauthRequired || updated.FailureCount != buildRecoveryMaxAttempts || !strings.Contains(updated.LastError, "retired:") {
			t.Fatalf("retired updated=%#v", updated)
		}
	}
}

func TestReimportRevivesSoftRetiredAccount(t *testing.T) {
	service, repository := newBuildChatProbeService(t, http.StatusOK, `{"model":"grok-4.5-build-free"}`)
	credential := createBuildProbeAccount(t, repository, "revive-retired")
	credential.Enabled = false
	credential.AuthStatus = accountdomain.AuthStatusReauthRequired
	credential.LastError = "retired: recovery attempts exhausted"
	credential.FailureCount = buildRecoveryMaxAttempts
	if _, err := repository.Update(context.Background(), credential); err != nil {
		t.Fatal(err)
	}

	reimported, _, err := repository.UpsertByIdentity(context.Background(), accountdomain.Credential{
		Provider: accountdomain.ProviderBuild, AuthType: accountdomain.AuthTypeOAuth, Name: credential.Name, SourceKey: credential.SourceKey,
		EncryptedAccessToken: "access-new", EncryptedRefreshToken: "refresh-new", ExpiresAt: time.Now().Add(time.Hour), AuthStatus: accountdomain.AuthStatusActive,
	})
	if err != nil {
		t.Fatal(err)
	}
	if !reimported.Enabled || reimported.AuthStatus != accountdomain.AuthStatusActive || reimported.FailureCount != 0 || reimported.LastError != "" {
		t.Fatalf("reimported=%#v", reimported)
	}
	_ = service
}

func TestMarkReauthRequiredPreservesSoftRetiredState(t *testing.T) {
	service, repository := newBuildChatProbeService(t, http.StatusOK, `{"model":"grok-4.5-build-free"}`)
	credential := createBuildProbeAccount(t, repository, "preserve-retired")
	credential.Enabled = false
	credential.AuthStatus = accountdomain.AuthStatusReauthRequired
	credential.LastError = "retired: recovery attempts exhausted"
	credential.FailureCount = buildRecoveryMaxAttempts
	if _, err := repository.Update(context.Background(), credential); err != nil {
		t.Fatal(err)
	}

	if err := service.MarkReauthRequired(context.Background(), credential.ID, "OAuth refresh failed: invalid_grant"); err != nil {
		t.Fatal(err)
	}
	updated, err := repository.Get(context.Background(), credential.ID)
	if err != nil {
		t.Fatal(err)
	}
	if updated.Enabled || updated.FailureCount != buildRecoveryMaxAttempts || updated.LastError != credential.LastError {
		t.Fatalf("updated=%#v", updated)
	}
}

func TestProbeNextBuildChatRefreshesBillingForCurrentAccount(t *testing.T) {
	database, err := relational.OpenSQLite(context.Background(), filepath.Join(t.TempDir(), "build-probe-billing.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = database.Close() })
	if err := database.InitializeSchema(context.Background()); err != nil {
		t.Fatal(err)
	}
	repository := relational.NewAccountRepository(database)
	adapter := &buildChatBillingProbeAdapter{}
	service := NewService(repository, nil, nil, nil, provider.NewRegistry(adapter), nil, nil)
	credential := createBuildProbeAccount(t, repository, "billing-probe")
	credential.EncryptedRefreshToken = "refresh-token"
	if _, err := repository.Update(context.Background(), credential); err != nil {
		t.Fatal(err)
	}

	accountID, found, err := service.ProbeNextBuildChat(context.Background())
	if err != nil || !found || accountID != credential.ID {
		t.Fatalf("account=%d found=%v err=%v", accountID, found, err)
	}
	if adapter.billingCount != 1 {
		t.Fatalf("billing refreshes = %d", adapter.billingCount)
	}
	billing, err := repository.GetBilling(context.Background(), credential.ID)
	if err != nil {
		t.Fatal(err)
	}
	if billing.MonthlyLimit != 100 || billing.Used != 12 {
		t.Fatalf("billing = %#v", billing)
	}
}

type buildChatBillingProbeAdapter struct {
	billingCount int
}

func (a *buildChatBillingProbeAdapter) Provider() accountdomain.Provider {
	return accountdomain.ProviderBuild
}
func (a *buildChatBillingProbeAdapter) Definition() provider.Definition {
	return provider.Definition{
		Provider:     accountdomain.ProviderBuild,
		Quota:        provider.QuotaBilling,
		Credential:   provider.CredentialSurface{AuthType: accountdomain.AuthTypeOAuth, Refresh: true},
		Conversation: provider.ConversationSurface{Responses: true},
	}
}
func (a *buildChatBillingProbeAdapter) RefreshCredential(context.Context, accountdomain.Credential) (provider.RefreshedCredential, error) {
	return provider.RefreshedCredential{EncryptedAccessToken: "access", EncryptedRefreshToken: "refresh", ExpiresAt: time.Now().Add(time.Hour)}, nil
}
func (a *buildChatBillingProbeAdapter) GetBilling(context.Context, accountdomain.Credential) (accountdomain.Billing, error) {
	a.billingCount++
	return accountdomain.Billing{MonthlyLimit: 100, Used: 12, SyncedAt: time.Now().UTC()}, nil
}
func (a *buildChatBillingProbeAdapter) ForwardResponse(context.Context, provider.ResponseResourceRequest) (*provider.Response, error) {
	return &provider.Response{StatusCode: http.StatusOK, Status: http.StatusText(http.StatusOK), Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"model":"grok-4.5-build-free"}`))}, nil
}

func TestRefreshTokenImmediatelyVerifiesBuildCapability(t *testing.T) {
	service, repository, adapter := newBuildChatRecoveryService(t)
	credential := createBuildProbeAccount(t, repository, "manual-refresh-verification")
	credential.EncryptedRefreshToken = "refresh-old"
	if _, err := repository.Update(context.Background(), credential); err != nil {
		t.Fatal(err)
	}

	view, err := service.RefreshToken(context.Background(), credential.ID)
	if err != nil {
		t.Fatal(err)
	}
	if adapter.refreshCount != 1 || view.Credential.ObservedModel != "grok-4.5-build-free" || view.Credential.AuthStatus != accountdomain.AuthStatusActive {
		t.Fatalf("refreshes=%d view=%#v", adapter.refreshCount, view)
	}
}

func TestRefreshAllTokensImmediatelyVerifiesBuildCapability(t *testing.T) {
	service, repository, adapter := newBuildChatRecoveryService(t)
	credential := createBuildProbeAccount(t, repository, "bulk-refresh-verification")
	credential.EncryptedRefreshToken = "refresh-old"
	if _, err := repository.Update(context.Background(), credential); err != nil {
		t.Fatal(err)
	}

	succeeded, failed, skipped, err := service.RefreshAllTokens(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	updated, getErr := repository.Get(context.Background(), credential.ID)
	if getErr != nil {
		t.Fatal(getErr)
	}
	if succeeded != 1 || failed != 0 || skipped != 0 || adapter.refreshCount != 1 || updated.ObservedModel != "grok-4.5-build-free" {
		t.Fatalf("result=%d/%d/%d refreshes=%d updated=%#v", succeeded, failed, skipped, adapter.refreshCount, updated)
	}
}

func newBuildChatProbeService(t *testing.T, status int, body string) (*Service, *relational.AccountRepository) {
	t.Helper()
	database, err := relational.OpenSQLite(context.Background(), filepath.Join(t.TempDir(), "build-probe.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = database.Close() })
	if err := database.InitializeSchema(context.Background()); err != nil {
		t.Fatal(err)
	}
	repository := relational.NewAccountRepository(database)
	adapter := buildChatProbeAdapter{status: status, body: body}
	return NewService(repository, nil, nil, nil, provider.NewRegistry(adapter), nil, nil), repository
}

func newBuildChatRecoveryService(t *testing.T) (*Service, *relational.AccountRepository, *buildChatRecoveryAdapter) {
	t.Helper()
	database, err := relational.OpenSQLite(context.Background(), filepath.Join(t.TempDir(), "build-recovery.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = database.Close() })
	if err := database.InitializeSchema(context.Background()); err != nil {
		t.Fatal(err)
	}
	repository := relational.NewAccountRepository(database)
	adapter := &buildChatRecoveryAdapter{}
	return NewService(repository, relational.NewAuditRepository(database), nil, nil, provider.NewRegistry(adapter), nil, nil), repository, adapter
}

func createBuildProbeAccount(t *testing.T, repository *relational.AccountRepository, source string) accountdomain.Credential {
	t.Helper()
	credential, _, err := repository.UpsertByIdentity(context.Background(), accountdomain.Credential{
		Provider: accountdomain.ProviderBuild, AuthType: accountdomain.AuthTypeOAuth, Name: source, SourceKey: source,
		EncryptedAccessToken: "encrypted", ExpiresAt: time.Now().Add(time.Hour), Enabled: true, AuthStatus: accountdomain.AuthStatusActive,
	})
	if err != nil {
		t.Fatal(err)
	}
	return credential
}

type buildChatProbeAdapter struct {
	status int
	body   string
}

type buildChatRecoveryAdapter struct{ refreshCount int }

type buildChatOrderedProbeAdapter struct{ seen []string }

func (a *buildChatRecoveryAdapter) Provider() accountdomain.Provider {
	return accountdomain.ProviderBuild
}
func (a *buildChatRecoveryAdapter) Definition() provider.Definition {
	return provider.Definition{Provider: accountdomain.ProviderBuild, Credential: provider.CredentialSurface{AuthType: accountdomain.AuthTypeOAuth, Refresh: true}, Conversation: provider.ConversationSurface{Responses: true}}
}
func (a *buildChatRecoveryAdapter) RefreshCredential(context.Context, accountdomain.Credential) (provider.RefreshedCredential, error) {
	a.refreshCount++
	return provider.RefreshedCredential{EncryptedAccessToken: "access-new", EncryptedRefreshToken: "refresh-new", ExpiresAt: time.Now().Add(time.Hour)}, nil
}
func (a *buildChatRecoveryAdapter) ForwardResponse(context.Context, provider.ResponseResourceRequest) (*provider.Response, error) {
	return &provider.Response{StatusCode: http.StatusOK, Status: http.StatusText(http.StatusOK), Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"model":"grok-4.5-build-free"}`))}, nil
}

func (a *buildChatOrderedProbeAdapter) Provider() accountdomain.Provider {
	return accountdomain.ProviderBuild
}
func (a *buildChatOrderedProbeAdapter) Definition() provider.Definition {
	return provider.Definition{Provider: accountdomain.ProviderBuild, Conversation: provider.ConversationSurface{Responses: true}}
}
func (a *buildChatOrderedProbeAdapter) ForwardResponse(_ context.Context, request provider.ResponseResourceRequest) (*provider.Response, error) {
	a.seen = append(a.seen, request.Credential.Name)
	status := http.StatusOK
	body := `{"model":"grok-4.5-build-free"}`
	if request.Credential.Name == "first" {
		status = http.StatusServiceUnavailable
		body = `{"error":{"message":"temporary unavailable"}}`
	}
	return &provider.Response{StatusCode: status, Status: http.StatusText(status), Header: make(http.Header), Body: io.NopCloser(strings.NewReader(body))}, nil
}

func (a buildChatProbeAdapter) Provider() accountdomain.Provider { return accountdomain.ProviderBuild }
func (a buildChatProbeAdapter) Definition() provider.Definition {
	return provider.Definition{Provider: accountdomain.ProviderBuild, Conversation: provider.ConversationSurface{Responses: true}}
}
func (a buildChatProbeAdapter) ForwardResponse(context.Context, provider.ResponseResourceRequest) (*provider.Response, error) {
	return &provider.Response{StatusCode: a.status, Status: http.StatusText(a.status), Header: make(http.Header), Body: io.NopCloser(strings.NewReader(a.body))}, nil
}
