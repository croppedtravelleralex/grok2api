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

func (a buildChatProbeAdapter) Provider() accountdomain.Provider { return accountdomain.ProviderBuild }
func (a buildChatProbeAdapter) Definition() provider.Definition {
	return provider.Definition{Provider: accountdomain.ProviderBuild, Conversation: provider.ConversationSurface{Responses: true}}
}
func (a buildChatProbeAdapter) ForwardResponse(context.Context, provider.ResponseResourceRequest) (*provider.Response, error) {
	return &provider.Response{StatusCode: a.status, Status: http.StatusText(a.status), Header: make(http.Header), Body: io.NopCloser(strings.NewReader(a.body))}, nil
}
