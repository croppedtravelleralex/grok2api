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
	return NewService(repository, nil, nil, nil, provider.NewRegistry(adapter), nil, nil), repository, adapter
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

func (a buildChatProbeAdapter) Provider() accountdomain.Provider { return accountdomain.ProviderBuild }
func (a buildChatProbeAdapter) Definition() provider.Definition {
	return provider.Definition{Provider: accountdomain.ProviderBuild, Conversation: provider.ConversationSurface{Responses: true}}
}
func (a buildChatProbeAdapter) ForwardResponse(context.Context, provider.ResponseResourceRequest) (*provider.Response, error) {
	return &provider.Response{StatusCode: a.status, Status: http.StatusText(a.status), Header: make(http.Header), Body: io.NopCloser(strings.NewReader(a.body))}, nil
}
