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

func TestBuildProbeStatusTracksRunningAccountAndQuarantineTransition(t *testing.T) {
	adapter := &blockingBuildProbeAdapter{
		started: make(chan accountdomain.Credential, 1),
		release: make(chan struct{}),
		status:  http.StatusForbidden,
		body:    `{"error":{"code":"permission-denied","message":"Access to the chat endpoint is denied"}}`,
	}
	service, repository := newBuildChatProbeServiceWithAdapter(t, adapter)
	service.ConfigureBuildProbe(30*time.Second, 5*time.Minute, 2*time.Minute)
	credential := createBuildProbeAccount(t, repository, "visual-probe")

	done := make(chan error, 1)
	go func() {
		_, _, err := service.ProbeNextBuildChat(context.Background())
		done <- err
	}()

	select {
	case started := <-adapter.started:
		if started.ID != credential.ID {
			t.Fatalf("started account = %d", started.ID)
		}
	case <-time.After(time.Second):
		t.Fatal("probe did not start")
	}

	running, err := service.BuildProbeStatus(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if !running.Enabled || !running.Running || running.Current == nil || running.Current.AccountID != credential.ID || running.Current.Mode != BuildProbeModeVerification {
		t.Fatalf("running status = %#v", running)
	}

	close(adapter.release)
	if err := <-done; err == nil {
		t.Fatal("expected permission denied probe to fail")
	}

	completed, err := service.BuildProbeStatus(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if completed.Running || completed.Statistics.Attempts != 1 || completed.Statistics.Failed != 1 || completed.Statistics.Quarantined != 1 {
		t.Fatalf("completed status = %#v", completed)
	}
	if completed.Pools.Quarantine != 1 || len(completed.Recent) != 1 || completed.Recent[0].Outcome != BuildProbeOutcomeQuarantine {
		t.Fatalf("completed pools/recent = %#v", completed)
	}
}

func TestBuildProbeStatusCountsFailedRecoveryInRecoveryPool(t *testing.T) {
	service, repository := newBuildChatProbeService(t, http.StatusForbidden, `{"error":{"code":"permission-denied","message":"Access to the chat endpoint is denied"}}`)
	service.ConfigureBuildProbe(30*time.Second, 5*time.Minute, 2*time.Minute)
	credential := createBuildProbeAccount(t, repository, "recovery-visual")
	if err := service.MarkReauthRequired(context.Background(), credential.ID, "grok_build chat endpoint access denied"); err != nil {
		t.Fatal(err)
	}

	if _, found, err := service.ProbeNextBuildChat(context.Background()); err == nil || !found {
		t.Fatalf("found=%v err=%v", found, err)
	}
	status, err := service.BuildProbeStatus(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if status.Statistics.RecoveryQueued != 1 || status.Pools.Recovery != 1 || status.Recent[0].Outcome != BuildProbeOutcomeRecovery {
		t.Fatalf("status = %#v", status)
	}
}

type blockingBuildProbeAdapter struct {
	started chan accountdomain.Credential
	release chan struct{}
	status  int
	body    string
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

func (a *blockingBuildProbeAdapter) Provider() accountdomain.Provider {
	return accountdomain.ProviderBuild
}
func (a *blockingBuildProbeAdapter) Definition() provider.Definition {
	return provider.Definition{Provider: accountdomain.ProviderBuild, Conversation: provider.ConversationSurface{Responses: true}}
}
func (a *blockingBuildProbeAdapter) ForwardResponse(_ context.Context, request provider.ResponseResourceRequest) (*provider.Response, error) {
	a.started <- request.Credential
	<-a.release
	return &provider.Response{StatusCode: a.status, Status: http.StatusText(a.status), Header: make(http.Header), Body: io.NopCloser(strings.NewReader(a.body))}, nil
}
