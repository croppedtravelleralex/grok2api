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
	if AccountPoolAt(updated, time.Now().UTC(), nil) != PoolDispatch {
		t.Fatalf("pool = %s", AccountPoolAt(updated, time.Now().UTC(), nil))
	}
}

func TestProbeNextBuildChatMarksDeletableOnPermissionDenied(t *testing.T) {
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
	if updated.Enabled || !strings.HasPrefix(strings.ToLower(updated.LastError), "deletable:") {
		t.Fatalf("updated = %#v", updated)
	}
}

func TestProbeNextBuildChatSweepsVerificationPool(t *testing.T) {
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

	seen := map[uint64]bool{}
	for i := 0; i < 4; i++ {
		accountID, found, err := service.ProbeNextBuildChat(context.Background())
		if !found {
			continue
		}
		if err != nil && accountID == 0 {
			t.Fatalf("probe err=%v", err)
		}
		seen[accountID] = true
	}
	if !seen[first.ID] || !seen[second.ID] {
		t.Fatalf("seen=%v first=%d second=%d adapter=%v", seen, first.ID, second.ID, adapter.seen)
	}
}

func TestProbeNextBuildChatDeletesDeletableWhenApplyOn(t *testing.T) {
	service, repository := newBuildChatProbeService(t, http.StatusOK, `{"model":"grok-4.5"}`)
	service.SetBuildProbePurgeApply(true)
	credential := createBuildProbeAccount(t, repository, "to-delete")
	if err := service.markBuildDeletable(context.Background(), credential.ID, "test delete"); err != nil {
		t.Fatal(err)
	}
	accountID, found, err := service.ProbeNextBuildChat(context.Background())
	if err != nil || !found || accountID != credential.ID {
		t.Fatalf("account=%d found=%v err=%v", accountID, found, err)
	}
	if _, err := repository.Get(context.Background(), credential.ID); err == nil {
		t.Fatal("expected account deleted")
	}
}

func TestMarkReauthRequiredMarksBuildDeletable(t *testing.T) {
	service, repository := newBuildChatProbeService(t, http.StatusOK, `{"model":"grok-4.5"}`)
	credential := createBuildProbeAccount(t, repository, "reauth")
	if err := service.MarkReauthRequired(context.Background(), credential.ID, "invalid_grant"); err != nil {
		t.Fatal(err)
	}
	updated, err := repository.Get(context.Background(), credential.ID)
	if err != nil {
		t.Fatal(err)
	}
	if updated.Enabled || !strings.Contains(updated.LastError, "deletable:") {
		t.Fatalf("updated=%#v", updated)
	}
}

func TestMigrateBuildDeadAccountsToDeletePool(t *testing.T) {
	service, repository := newBuildChatProbeService(t, http.StatusOK, `{"model":"grok-4.5"}`)
	credential := createBuildProbeAccount(t, repository, "legacy")
	credential.Enabled = false
	credential.LastError = "retired: old"
	if _, err := repository.Update(context.Background(), credential); err != nil {
		t.Fatal(err)
	}
	count, err := service.MigrateBuildDeadAccountsToDeletePool(context.Background())
	if err != nil || count < 1 {
		t.Fatalf("count=%d err=%v", count, err)
	}
	updated, err := repository.Get(context.Background(), credential.ID)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(strings.ToLower(updated.LastError), "deletable:") {
		t.Fatalf("updated=%#v", updated)
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

func newBuildChatProbeServiceWithAdapter(t *testing.T, adapter provider.Adapter) (*Service, *relational.AccountRepository) {
	t.Helper()
	database, err := relational.OpenSQLite(context.Background(), filepath.Join(t.TempDir(), "build-probe-monitor.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = database.Close() })
	if err := database.InitializeSchema(context.Background()); err != nil {
		t.Fatal(err)
	}
	repository := relational.NewAccountRepository(database)
	return NewService(repository, nil, nil, nil, provider.NewRegistry(adapter), nil, nil), repository
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

type buildChatOrderedProbeAdapter struct{ seen []string }

func (a *buildChatOrderedProbeAdapter) Provider() accountdomain.Provider {
	return accountdomain.ProviderBuild
}
func (a *buildChatOrderedProbeAdapter) Definition() provider.Definition {
	return provider.Definition{Provider: accountdomain.ProviderBuild, Conversation: provider.ConversationSurface{Responses: true}}
}
func (a *buildChatOrderedProbeAdapter) ForwardResponse(_ context.Context, request provider.ResponseResourceRequest) (*provider.Response, error) {
	a.seen = append(a.seen, request.Credential.Name)
	return &provider.Response{StatusCode: http.StatusOK, Status: http.StatusText(http.StatusOK), Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"model":"grok-4.5-build-free"}`))}, nil
}

func (a buildChatProbeAdapter) Provider() accountdomain.Provider { return accountdomain.ProviderBuild }
func (a buildChatProbeAdapter) Definition() provider.Definition {
	return provider.Definition{Provider: accountdomain.ProviderBuild, Conversation: provider.ConversationSurface{Responses: true}}
}
func (a buildChatProbeAdapter) ForwardResponse(context.Context, provider.ResponseResourceRequest) (*provider.Response, error) {
	return &provider.Response{StatusCode: a.status, Status: http.StatusText(a.status), Header: make(http.Header), Body: io.NopCloser(strings.NewReader(a.body))}, nil
}
