package app

import (
	"context"
	"encoding/json"
	"errors"
	"path/filepath"
	"strings"
	"testing"
	"time"

	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
	modeldomain "github.com/chenyme/grok2api/backend/internal/domain/model"
	"github.com/chenyme/grok2api/backend/internal/infra/persistence/relational"
	"github.com/chenyme/grok2api/backend/internal/infra/provider"
)

func TestReadinessStartupReportDoesNotExposeInternalErrors(t *testing.T) {
	state := newStartupState(0)
	state.recordError(errors.New("postgres://private-host/internal"))
	_, _, report, _ := state.snapshot()
	payload, err := json.Marshal(newReadinessStartupReport(report))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(payload), "private-host") || !strings.Contains(string(payload), `"errorCount":1`) {
		t.Fatalf("public readiness leaked internal error: %s", payload)
	}
}

func TestReadinessKeepsBuildReadyWhenWebIsUnavailable(t *testing.T) {
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "readiness.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	accounts := relational.NewAccountRepository(database)
	models := relational.NewModelRepository(database)
	now := time.Now().UTC()
	build, _, err := accounts.UpsertByIdentity(ctx, accountdomain.Credential{
		Provider: accountdomain.ProviderBuild, Name: "build-ready", SourceKey: "build-ready",
		EncryptedAccessToken: "access", EncryptedRefreshToken: "refresh", ExpiresAt: now.Add(time.Hour),
		Enabled: true, AuthStatus: accountdomain.AuthStatusActive, MaxConcurrent: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := models.UpsertRoutes(ctx, []modeldomain.Route{
		{PublicID: "build-model", Provider: accountdomain.ProviderBuild, UpstreamModel: "build-model", Capability: modeldomain.CapabilityResponses, Enabled: true},
		{PublicID: "web-model", Provider: accountdomain.ProviderWeb, UpstreamModel: "web-model", Capability: modeldomain.CapabilityChat, Enabled: true},
	}); err != nil {
		t.Fatal(err)
	}
	if err := models.ReplaceAccountCapabilities(ctx, build.ID, []string{"build-model"}, now); err != nil {
		t.Fatal(err)
	}
	state := newStartupState(0)
	state.setPhase("running")
	state.setStatsig("unavailable", "test", 0)
	snapshot := readinessSnapshot(ctx, state, func(context.Context) error { return nil }, models, accounts, provider.NewRegistry())
	if !snapshot.Ready || snapshot.State != "degraded" {
		t.Fatalf("snapshot = %#v", snapshot)
	}
	if snapshot.Components["grok_build"].State != "ready" || snapshot.Components["grok_web"].State != "unavailable" {
		t.Fatalf("components = %#v", snapshot.Components)
	}
}

func TestReadinessRestoresPersistedCooldownWithoutUpstreamProbe(t *testing.T) {
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "cooldown-readiness.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	accounts := relational.NewAccountRepository(database)
	models := relational.NewModelRepository(database)
	now := time.Now().UTC()
	cooldownUntil := now.Add(10 * time.Minute)
	build, _, err := accounts.UpsertByIdentity(ctx, accountdomain.Credential{
		Provider: accountdomain.ProviderBuild, Name: "cooling", SourceKey: "cooling",
		EncryptedAccessToken: "access", EncryptedRefreshToken: "refresh", ExpiresAt: now.Add(time.Hour),
		Enabled: true, AuthStatus: accountdomain.AuthStatusActive, MaxConcurrent: 1, CooldownUntil: &cooldownUntil,
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := models.UpsertRoutes(ctx, []modeldomain.Route{{PublicID: "build-model", Provider: accountdomain.ProviderBuild, UpstreamModel: "build-model", Capability: modeldomain.CapabilityResponses, Enabled: true}}); err != nil {
		t.Fatal(err)
	}
	if err := models.ReplaceAccountCapabilities(ctx, build.ID, []string{"build-model"}, now); err != nil {
		t.Fatal(err)
	}
	state := newStartupState(0)
	state.setPhase("running")
	snapshot := readinessSnapshot(ctx, state, func(context.Context) error { return nil }, models, accounts, provider.NewRegistry())
	if snapshot.Ready || snapshot.State != "not_ready" || snapshot.Components["grok_build"].State != "unavailable" {
		t.Fatalf("snapshot = %#v", snapshot)
	}
}

func TestWebQuotaCatchupSettingsRespectPandaLimits(t *testing.T) {
	t.Setenv("GROK2API_WEB_QUOTA_STARTUP_LIMIT", "0")
	t.Setenv("GROK2API_WEB_QUOTA_CATCHUP_LIMIT", "1")
	t.Setenv("GROK2API_WEB_QUOTA_CATCHUP_INITIAL_DELAY", "10m")
	t.Setenv("GROK2API_WEB_QUOTA_CATCHUP_EVERY", "45m")

	if got := webQuotaStartupLimit(); got != 0 {
		t.Fatalf("startup limit = %d", got)
	}
	if got := webQuotaCatchupLimit(); got != 1 {
		t.Fatalf("catchup limit = %d", got)
	}
	if got := webQuotaCatchupInitialDelay(); got != 10*time.Minute {
		t.Fatalf("initial delay = %s", got)
	}
	if got := webQuotaCatchupInterval(); got != 45*time.Minute {
		t.Fatalf("interval = %s", got)
	}
}

func TestWebQuotaCatchupSettingsRejectUnsafeValues(t *testing.T) {
	t.Setenv("GROK2API_WEB_QUOTA_STARTUP_LIMIT", "10000")
	t.Setenv("GROK2API_WEB_QUOTA_CATCHUP_LIMIT", "invalid")
	t.Setenv("GROK2API_WEB_QUOTA_CATCHUP_INITIAL_DELAY", "1s")
	t.Setenv("GROK2API_WEB_QUOTA_CATCHUP_EVERY", "1m")

	if got := webQuotaStartupLimit(); got != defaultWebQuotaStartupLimit {
		t.Fatalf("unsafe startup limit = %d", got)
	}
	if got := webQuotaCatchupLimit(); got != defaultWebQuotaCatchupLimit {
		t.Fatalf("invalid catchup limit = %d", got)
	}
	if got := webQuotaCatchupInitialDelay(); got != defaultWebQuotaCatchupInitialDelay {
		t.Fatalf("unsafe initial delay = %s", got)
	}
	if got := webQuotaCatchupInterval(); got != defaultWebQuotaCatchupInterval {
		t.Fatalf("unsafe interval = %s", got)
	}
}

func TestBuildChatProbeSettingsAreDisabledByDefaultAndBounded(t *testing.T) {
	t.Setenv("GROK2API_BUILD_CHAT_PROBE_EVERY", "")
	if got := buildChatProbeInterval(); got != 0 {
		t.Fatalf("default probe interval = %s", got)
	}
	t.Setenv("GROK2API_BUILD_CHAT_PROBE_EVERY", "30s")
	t.Setenv("GROK2API_BUILD_CHAT_PROBE_INITIAL_DELAY", "2m")
	t.Setenv("GROK2API_BUILD_CHAT_PROBE_IDLE_EVERY", "5m")
	if got := buildChatProbeInterval(); got != 30*time.Second {
		t.Fatalf("probe interval = %s", got)
	}
	if got := buildChatProbeInitialDelay(); got != 2*time.Minute {
		t.Fatalf("probe initial delay = %s", got)
	}
	if got := buildChatProbeIdleInterval(); got != 5*time.Minute {
		t.Fatalf("probe idle interval = %s", got)
	}
	t.Setenv("GROK2API_BUILD_CHAT_PROBE_EVERY", "10s")
	if got := buildChatProbeInterval(); got != 0 {
		t.Fatalf("unsafe probe interval = %s", got)
	}
	t.Setenv("GROK2API_BUILD_CHAT_PROBE_IDLE_EVERY", "20s")
	if got := buildChatProbeIdleInterval(); got != defaultBuildChatProbeIdleInterval {
		t.Fatalf("unsafe probe idle interval = %s", got)
	}
}

func TestDisabledBuildChatProbeWaitsForShutdown(t *testing.T) {
	t.Setenv("GROK2API_BUILD_CHAT_PROBE_EVERY", "")
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		(&Application{}).runBuildChatProbe(ctx)
		close(done)
	}()
	select {
	case <-done:
		t.Fatal("disabled build chat probe returned before shutdown")
	case <-time.After(20 * time.Millisecond):
	}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("disabled build chat probe did not stop with context")
	}
}
