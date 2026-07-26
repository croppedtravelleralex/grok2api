package gateway

import (
	"context"
	"errors"
	"path/filepath"
	"testing"
	"time"

	"github.com/chenyme/grok2api/backend/internal/domain/account"
	"github.com/chenyme/grok2api/backend/internal/infra/persistence/relational"
	"github.com/chenyme/grok2api/backend/internal/infra/runtime/memory"
	"github.com/chenyme/grok2api/backend/internal/repository"
)

func TestSelectorPrioritizesDueQuotaProbeOnce(t *testing.T) {
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "selector.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}

	accounts := relational.NewAccountRepository(database)
	probe, _, err := accounts.UpsertByIdentity(ctx, account.Credential{
		Provider: account.ProviderBuild, Name: "probe", SourceKey: "probe", EncryptedAccessToken: "encrypted", Enabled: true,
		AuthStatus: account.AuthStatusActive, Priority: 10, MaxConcurrent: 1,
		ObservedModel: "grok-4.5-build-free",
	})
	if err != nil {
		t.Fatal(err)
	}
	active, _, err := accounts.UpsertByIdentity(ctx, account.Credential{
		Provider: account.ProviderBuild, Name: "active", SourceKey: "active", EncryptedAccessToken: "encrypted", Enabled: true,
		AuthStatus: account.AuthStatusActive, Priority: 200, MaxConcurrent: 1,
		ObservedModel: "grok-4.5-build-free",
	})
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	due := now.Add(-time.Minute)
	if err := accounts.SaveQuotaRecovery(ctx, account.QuotaRecovery{
		AccountID: probe.ID, Kind: account.QuotaRecoveryKindFree, Status: account.QuotaRecoveryStatusExhausted,
		ConfirmedUsed: 1_065_387, ConfirmedLimit: 1_000_000,
		ExhaustedAt: &now, NextProbeAt: &due, LastConfirmedAt: &now, UpdatedAt: now,
	}); err != nil {
		t.Fatal(err)
	}

	selector := newTestSelector(accounts, memory.NewConcurrencyLimiter(), memory.NewStickyStore(), nil, time.Hour, time.Second, time.Minute)
	lease, err := selector.Acquire(ctx, account.ProviderBuild, "grok-test", "", "", map[uint64]bool{}, true)
	if err != nil {
		t.Fatal(err)
	}
	if lease.Credential.ID != probe.ID || !lease.QuotaProbe {
		t.Fatalf("lease = %#v, want due probe account %d", lease, probe.ID)
	}
	lease.Release()

	lease, err = selector.Acquire(ctx, account.ProviderBuild, "grok-test", "", "", map[uint64]bool{probe.ID: true}, false)
	if err != nil {
		t.Fatal(err)
	}
	if lease.Credential.ID != active.ID || lease.QuotaProbe {
		t.Fatalf("lease = %#v, want active account %d", lease, active.ID)
	}
	lease.Release()

	selector.MarkSuccess(ctx, probe)
	if _, err := accounts.GetQuotaRecovery(ctx, probe.ID); !errors.Is(err, repository.ErrNotFound) {
		t.Fatalf("quota recovery should be cleared, err = %v", err)
	}
}

func TestSelectorSkipsQuotaProbeBeforeDue(t *testing.T) {
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "selector.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}

	accounts := relational.NewAccountRepository(database)
	value, _, err := accounts.UpsertByIdentity(ctx, account.Credential{
		Provider: account.ProviderBuild, Name: "waiting", SourceKey: "waiting", EncryptedAccessToken: "encrypted", Enabled: true,
		AuthStatus: account.AuthStatusActive, Priority: 100, MaxConcurrent: 1,
		ObservedModel: "grok-4.5-build-free",
	})
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	next := now.Add(time.Hour)
	if err := accounts.SaveQuotaRecovery(ctx, account.QuotaRecovery{
		AccountID: value.ID, Kind: account.QuotaRecoveryKindFree, Status: account.QuotaRecoveryStatusExhausted,
		NextProbeAt: &next, UpdatedAt: now,
	}); err != nil {
		t.Fatal(err)
	}

	selector := newTestSelector(accounts, memory.NewConcurrencyLimiter(), memory.NewStickyStore(), nil, time.Hour, time.Second, time.Minute)
	if _, err := selector.Acquire(ctx, account.ProviderBuild, "grok-test", "", "", map[uint64]bool{}, true); err == nil {
		t.Fatal("expected no account before next probe time")
	}
}

func TestSelectorUsesPaidWeeklyPoolAsWebQuotaGate(t *testing.T) {
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "weekly-web.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	accounts := relational.NewAccountRepository(database)
	value, _, err := accounts.UpsertByIdentity(ctx, account.Credential{
		Provider: account.ProviderWeb, AuthType: account.AuthTypeSSO, Name: "paid-web", SourceKey: "paid-web",
		EncryptedAccessToken: "encrypted", Enabled: true, AuthStatus: account.AuthStatusActive, MaxConcurrent: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	resetAt := now.Add(7 * 24 * time.Hour)
	if err := accounts.SaveQuotaWindows(ctx, value.ID, account.WebTierSuper, now, []account.QuotaWindow{
		{AccountID: value.ID, Mode: "weekly", Remaining: 0, Total: 10000, UsagePercent: 100, ResetAt: &resetAt, SyncedAt: &now, Source: account.QuotaSourceUpstream},
		{AccountID: value.ID, Mode: "fast", Remaining: 30, Total: 30, ResetAt: &resetAt, SyncedAt: &now, Source: account.QuotaSourceUpstream},
	}); err != nil {
		t.Fatal(err)
	}
	selector := newTestSelector(accounts, memory.NewConcurrencyLimiter(), memory.NewStickyStore(), nil, time.Hour, time.Second, time.Minute)
	if _, err := selector.Acquire(ctx, account.ProviderWeb, "", "fast", "", nil, false); err == nil {
		t.Fatal("exhausted weekly pool must take precedence over a stale fast quota window")
	}
	if err := accounts.SaveQuotaWindows(ctx, value.ID, account.WebTierSuper, now, []account.QuotaWindow{
		{AccountID: value.ID, Mode: "weekly", Remaining: 8900, Total: 10000, UsagePercent: 11, ResetAt: &resetAt, SyncedAt: &now, Source: account.QuotaSourceUpstream},
		{AccountID: value.ID, Mode: "fast", Remaining: 0, Total: 30, ResetAt: &resetAt, SyncedAt: &now, Source: account.QuotaSourceUpstream},
	}); err != nil {
		t.Fatal(err)
	}
	selector.MarkQuotaStateChanged(account.ProviderWeb)
	lease, err := selector.Acquire(ctx, account.ProviderWeb, "", "fast", "", nil, false)
	if err != nil {
		t.Fatal(err)
	}
	defer lease.Release()
	if lease.QuotaMode != "weekly" {
		t.Fatalf("quota mode = %q, want weekly", lease.QuotaMode)
	}
}

func TestSelectorClaimsPaidBillingProbeAfterPeriodEnd(t *testing.T) {
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "paid-probe.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	accounts := relational.NewAccountRepository(database)
	value, _, err := accounts.UpsertByIdentity(ctx, account.Credential{Provider: account.ProviderBuild, Name: "paid", SourceKey: "paid", EncryptedAccessToken: "encrypted", AuthStatus: account.AuthStatusActive, MaxConcurrent: 1, ObservedModel: "grok-4.5-build-free"})
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	due := now.Add(-time.Minute)
	if err := accounts.SaveQuotaRecovery(ctx, account.QuotaRecovery{AccountID: value.ID, Kind: account.QuotaRecoveryKindPaid, Status: account.QuotaRecoveryStatusExhausted, NextProbeAt: &due, UpdatedAt: now}); err != nil {
		t.Fatal(err)
	}
	selector := newTestSelector(accounts, memory.NewConcurrencyLimiter(), memory.NewStickyStore(), nil, time.Hour, time.Second, time.Minute)
	lease, err := selector.Acquire(ctx, account.ProviderBuild, "", "", "", map[uint64]bool{}, true)
	if err != nil {
		t.Fatal(err)
	}
	defer lease.Release()
	if !lease.QuotaProbe || lease.QuotaProbeKind != account.QuotaRecoveryKindPaid {
		t.Fatalf("lease = %#v", lease)
	}
}

func TestSelectorOnlyUsesAccountsSupportingRequestedModel(t *testing.T) {
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "selector-model.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}

	accounts := relational.NewAccountRepository(database)
	models := relational.NewModelRepository(database)
	unsupported, _, err := accounts.UpsertByIdentity(ctx, account.Credential{
		Provider: account.ProviderBuild, Name: "basic", SourceKey: "basic", EncryptedAccessToken: "encrypted", AuthStatus: account.AuthStatusActive,
		Priority: 500, MaxConcurrent: 1,
		ObservedModel: "grok-4.5-build-free",
	})
	if err != nil {
		t.Fatal(err)
	}
	supported, _, err := accounts.UpsertByIdentity(ctx, account.Credential{
		Provider: account.ProviderBuild, Name: "premium", SourceKey: "premium", EncryptedAccessToken: "encrypted", AuthStatus: account.AuthStatusActive,
		Priority: 100, MaxConcurrent: 1,
		ObservedModel: "grok-4.5-build-free",
	})
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	if err := models.ReplaceAccountCapabilities(ctx, unsupported.ID, []string{"grok-basic"}, now); err != nil {
		t.Fatal(err)
	}
	if err := models.ReplaceAccountCapabilities(ctx, supported.ID, []string{"grok-basic", "grok-premium"}, now); err != nil {
		t.Fatal(err)
	}

	selector := newTestSelector(accounts, memory.NewConcurrencyLimiter(), memory.NewStickyStore(), nil, time.Hour, time.Second, time.Minute)
	lease, err := selector.Acquire(ctx, account.ProviderBuild, "grok-premium", "", "", map[uint64]bool{}, true)
	if err != nil {
		t.Fatal(err)
	}
	defer lease.Release()
	if lease.Credential.ID != supported.ID {
		t.Fatalf("selected account = %d, want %d", lease.Credential.ID, supported.ID)
	}
}

func TestSelectorUsesOnlyVerifiedBuildAccountsWhenAvailable(t *testing.T) {
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "selector-verified-build.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}

	accounts := relational.NewAccountRepository(database)
	unverified, _, err := accounts.UpsertByIdentity(ctx, account.Credential{
		Provider: account.ProviderBuild, Name: "unverified", SourceKey: "unverified", EncryptedAccessToken: "encrypted", AuthStatus: account.AuthStatusActive,
		Priority: 500, MaxConcurrent: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	verified, _, err := accounts.UpsertByIdentity(ctx, account.Credential{
		Provider: account.ProviderBuild, Name: "verified", SourceKey: "verified", EncryptedAccessToken: "encrypted", AuthStatus: account.AuthStatusActive,
		Priority: 1, MaxConcurrent: 1,
		ObservedModel: "grok-4.5-build-free",
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := accounts.UpdateObservedModel(ctx, verified.ID, "grok-4.5-build-free", time.Now().UTC()); err != nil {
		t.Fatal(err)
	}

	selector := newTestSelector(accounts, memory.NewConcurrencyLimiter(), memory.NewStickyStore(), nil, time.Hour, time.Second, time.Minute)
	lease, err := selector.Acquire(ctx, account.ProviderBuild, "grok-4.5", "", "", nil, false)
	if err != nil {
		t.Fatal(err)
	}
	defer lease.Release()
	if lease.Credential.ID != verified.ID || lease.Credential.ID == unverified.ID {
		t.Fatalf("selected account = %d, want verified %d", lease.Credential.ID, verified.ID)
	}
}

func TestSelectorKeepsWebQuotaModesIsolated(t *testing.T) {
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "selector-web-quota.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	accounts := relational.NewAccountRepository(database)
	value, _, err := accounts.UpsertByIdentity(ctx, account.Credential{
		Provider: account.ProviderWeb, AuthType: account.AuthTypeSSO, WebTier: account.WebTierSuper,
		Name: "web", SourceKey: "web", EncryptedAccessToken: "encrypted", AuthStatus: account.AuthStatusActive, MaxConcurrent: 2,
	})
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	resetAt := now.Add(time.Hour)
	if err := accounts.SaveQuotaWindows(ctx, value.ID, account.WebTierSuper, now, []account.QuotaWindow{
		{AccountID: value.ID, Mode: "fast", Remaining: 0, Total: 20, ResetAt: &resetAt, Source: account.QuotaSourceUpstream},
		{AccountID: value.ID, Mode: "auto", Remaining: 5, Total: 10, ResetAt: &resetAt, Source: account.QuotaSourceUpstream},
	}); err != nil {
		t.Fatal(err)
	}
	selector := newTestSelector(accounts, memory.NewConcurrencyLimiter(), memory.NewStickyStore(), nil, time.Hour, time.Second, time.Minute)
	if _, err := selector.Acquire(ctx, account.ProviderWeb, "grok-chat", "fast", "", nil, false); err == nil {
		t.Fatal("exhausted fast mode should not be selected")
	}
	lease, err := selector.Acquire(ctx, account.ProviderWeb, "grok-chat-auto", "auto", "", nil, false)
	if err != nil {
		t.Fatal(err)
	}
	defer lease.Release()
	if lease.Credential.ID != value.ID || lease.QuotaMode != "auto" {
		t.Fatalf("lease = %#v", lease)
	}
}

func TestSelectorHonorsWebTierPoolOrderBeforeAccountPriority(t *testing.T) {
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "selector-web-tier.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	accounts := relational.NewAccountRepository(database)
	for index, tier := range []account.WebTier{account.WebTierBasic, account.WebTierSuper, account.WebTierHeavy} {
		if _, _, err := accounts.UpsertByIdentity(ctx, account.Credential{
			Provider: account.ProviderWeb, AuthType: account.AuthTypeSSO, WebTier: tier,
			Name: string(tier), SourceKey: string(tier), EncryptedAccessToken: "encrypted",
			AuthStatus: account.AuthStatusActive, Priority: 300 - index*100, MaxConcurrent: 1,
		}); err != nil {
			t.Fatal(err)
		}
	}
	selector := newTestSelector(accounts, memory.NewConcurrencyLimiter(), memory.NewStickyStore(), staticTierOrder{order: []account.WebTier{account.WebTierHeavy, account.WebTierSuper, account.WebTierBasic}}, time.Hour, time.Second, time.Minute)
	lease, err := selector.Acquire(ctx, account.ProviderWeb, "fast-prefer-best", "fast", "", nil, false)
	if err != nil {
		t.Fatal(err)
	}
	defer lease.Release()
	if lease.Credential.WebTier != account.WebTierHeavy {
		t.Fatalf("selected tier = %s", lease.Credential.WebTier)
	}
}

func TestSelectorBuildAcquireAvoidsFullTableList(t *testing.T) {
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "build-index-acquire.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}

	accounts := relational.NewAccountRepository(database)
	active, _, err := accounts.UpsertByIdentity(ctx, account.Credential{
		Provider: account.ProviderBuild, Name: "dispatch", SourceKey: "dispatch", EncryptedAccessToken: "encrypted",
		Enabled: true, AuthStatus: account.AuthStatusActive, Priority: 100, MaxConcurrent: 1,
		ObservedModel: "grok-4.5-build-free",
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := accounts.UpsertByIdentity(ctx, account.Credential{
		Provider: account.ProviderBuild, Name: "other", SourceKey: "other", EncryptedAccessToken: "encrypted",
		Enabled: true, AuthStatus: account.AuthStatusActive, Priority: 1, MaxConcurrent: 1,
		ObservedModel: "grok-4.5-build-free",
	}); err != nil {
		t.Fatal(err)
	}

	counter := &countingAccountRepo{AccountRepository: accounts}
	dispatch := &stubBuildDispatchSource{dispatchIDs: []uint64{active.ID}}
	selector := newTestSelector(counter, memory.NewConcurrencyLimiter(), memory.NewStickyStore(), nil, time.Hour, time.Second, time.Minute)
	selector.SetBuildDispatchSource(dispatch)

	lease, err := selector.Acquire(ctx, account.ProviderBuild, "grok-test", "", "", nil, false)
	if err != nil {
		t.Fatal(err)
	}
	defer lease.Release()
	if lease.Credential.ID != active.ID {
		t.Fatalf("lease account = %d, want %d", lease.Credential.ID, active.ID)
	}
	if counter.listRoutingCandidatesCalls != 0 {
		t.Fatalf("ListRoutingCandidates calls = %d, want 0", counter.listRoutingCandidatesCalls)
	}
	if counter.listRoutingCandidatesByIDsCalls != 1 {
		t.Fatalf("ListRoutingCandidatesByIDs calls = %d, want 1", counter.listRoutingCandidatesByIDsCalls)
	}
}

func TestSelectorBuildAcquireMergesDueNormalProbeIDs(t *testing.T) {
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "build-normal-probe.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}

	accounts := relational.NewAccountRepository(database)
	probe, _, err := accounts.UpsertByIdentity(ctx, account.Credential{
		Provider: account.ProviderBuild, Name: "probe", SourceKey: "probe", EncryptedAccessToken: "encrypted",
		Enabled: true, AuthStatus: account.AuthStatusActive, Priority: 10, MaxConcurrent: 1,
		ObservedModel: "grok-4.5-build-free",
	})
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	due := now.Add(-time.Minute)
	if err := accounts.SaveQuotaRecovery(ctx, account.QuotaRecovery{
		AccountID: probe.ID, Kind: account.QuotaRecoveryKindFree, Status: account.QuotaRecoveryStatusExhausted,
		ExhaustedAt: &now, NextProbeAt: &due, LastConfirmedAt: &now, UpdatedAt: now,
	}); err != nil {
		t.Fatal(err)
	}

	counter := &countingAccountRepo{AccountRepository: accounts}
	dispatch := &stubBuildDispatchSource{normalProbeIDs: []uint64{probe.ID}}
	selector := newTestSelector(counter, memory.NewConcurrencyLimiter(), memory.NewStickyStore(), nil, time.Hour, time.Second, time.Minute)
	selector.SetBuildDispatchSource(dispatch)

	lease, err := selector.Acquire(ctx, account.ProviderBuild, "grok-test", "", "", nil, true)
	if err != nil {
		t.Fatal(err)
	}
	defer lease.Release()
	if lease.Credential.ID != probe.ID || !lease.QuotaProbe {
		t.Fatalf("lease = %#v, want due probe account %d", lease, probe.ID)
	}
	if counter.listRoutingCandidatesCalls != 0 {
		t.Fatalf("ListRoutingCandidates calls = %d, want 0", counter.listRoutingCandidatesCalls)
	}
	if counter.listRoutingCandidatesByIDsCalls != 1 {
		t.Fatalf("ListRoutingCandidatesByIDs calls = %d, want 1", counter.listRoutingCandidatesByIDsCalls)
	}
}

type countingAccountRepo struct {
	repository.AccountRepository
	listRoutingCandidatesCalls       int
	listRoutingCandidatesByIDsCalls  int
}

func (c *countingAccountRepo) ListRoutingCandidates(ctx context.Context, provider account.Provider, upstreamModel, quotaMode string) ([]account.RoutingCandidate, error) {
	c.listRoutingCandidatesCalls++
	return c.AccountRepository.ListRoutingCandidates(ctx, provider, upstreamModel, quotaMode)
}

func (c *countingAccountRepo) ListRoutingCandidatesByIDs(ctx context.Context, provider account.Provider, upstreamModel, quotaMode string, ids []uint64) ([]account.RoutingCandidate, error) {
	c.listRoutingCandidatesByIDsCalls++
	return c.AccountRepository.ListRoutingCandidatesByIDs(ctx, provider, upstreamModel, quotaMode, ids)
}

type stubBuildDispatchSource struct {
	dispatchIDs    []uint64
	normalProbeIDs []uint64
	warmCalls      int
}

func (s *stubBuildDispatchSource) OrderedDispatchIDs(limit int) []uint64 {
	if limit <= 0 || len(s.dispatchIDs) == 0 {
		return nil
	}
	if len(s.dispatchIDs) <= limit {
		return append([]uint64(nil), s.dispatchIDs...)
	}
	return append([]uint64(nil), s.dispatchIDs[:limit]...)
}

func (s *stubBuildDispatchSource) DueNormalProbeIDs(time.Time, int) []uint64 {
	return append([]uint64(nil), s.normalProbeIDs...)
}

func (s *stubBuildDispatchSource) NoteDispatchSelected(uint64, time.Time) {}

func (s *stubBuildDispatchSource) EnsurePoolIndexWarm(context.Context) {
	s.warmCalls++
}

func TestSelectorPropagatesConcurrencyStoreFailure(t *testing.T) {
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "selector-runtime-error.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	accounts := relational.NewAccountRepository(database)
	if _, _, err := accounts.UpsertByIdentity(ctx, account.Credential{
		Provider: account.ProviderBuild, Name: "active", SourceKey: "active", EncryptedAccessToken: "encrypted",
		AuthStatus: account.AuthStatusActive, MaxConcurrent: 1,
		ObservedModel: "grok-4.5-build-free",
	}); err != nil {
		t.Fatal(err)
	}

	runtimeErr := errors.New("runtime store unavailable")
	selector := newTestSelector(accounts, failingConcurrencyLimiter{err: runtimeErr}, memory.NewStickyStore(), nil, time.Hour, time.Second, time.Minute)
	if _, err := selector.Acquire(ctx, account.ProviderBuild, "", "", "", map[uint64]bool{}, true); !errors.Is(err, runtimeErr) {
		t.Fatalf("Acquire error = %v, want wrapped runtime error", err)
	}
}

func TestPromptCacheStickyKeyIsFixedLengthAndStable(t *testing.T) {
	first := promptCacheStickyKey("cache-key")
	if len(first) != 64 || first != promptCacheStickyKey("cache-key") {
		t.Fatalf("sticky key = %q", first)
	}
	if first == promptCacheStickyKey("another-key") {
		t.Fatal("different prompt cache keys produced the same sticky key")
	}
	if promptCacheStickyKey("") != "" {
		t.Fatal("empty prompt cache key should remain empty")
	}
}

func TestSelectorUsesBatchConcurrencySnapshot(t *testing.T) {
	limiter := &batchConcurrencyLimiter{values: map[string]int{"account:1": 2, "account:2": 1}}
	selector := &Selector{concurrency: limiter, lastSelectedAt: make(map[uint64]time.Time)}
	values := []account.RoutingCandidate{
		{Credential: account.Credential{ID: 1, Priority: 1}},
		{Credential: account.Credential{ID: 2, Priority: 1}},
	}
	if err := selector.sortCandidates(context.Background(), values, time.Now().UTC(), nil, "model"); err != nil {
		t.Fatal(err)
	}
	if limiter.batchCalls != 1 || limiter.currentCalls != 0 || values[0].Credential.ID != 2 {
		t.Fatalf("batchCalls=%d currentCalls=%d values=%#v", limiter.batchCalls, limiter.currentCalls, values)
	}
}

func TestSelectorRanksRecentModelSuccessBeforeUnknownAndSoftStop(t *testing.T) {
	selector := &Selector{
		concurrency:    memory.NewConcurrencyLimiter(),
		lastSelectedAt: make(map[uint64]time.Time),
	}
	model := "grok-imagine-image"
	if err := selector.MarkModelSoftStop(context.Background(), 1, model); err != nil {
		t.Fatal(err)
	}
	if err := selector.MarkModelSuccess(context.Background(), 2, model); err != nil {
		t.Fatal(err)
	}
	values := []account.RoutingCandidate{
		{Credential: account.Credential{ID: 1, Priority: 1}},
		{Credential: account.Credential{ID: 2, Priority: 1}},
		{Credential: account.Credential{ID: 3, Priority: 1}},
	}
	if err := selector.sortCandidates(context.Background(), values, time.Now().UTC(), nil, model); err != nil {
		t.Fatal(err)
	}
	got := []uint64{values[0].Credential.ID, values[1].Credential.ID, values[2].Credential.ID}
	if got[0] != 2 || got[1] != 3 || got[2] != 1 {
		t.Fatalf("model outcome order=%v, want [2 3 1]", got)
	}
}

func TestSelectorModelOutcomeDoesNotAffectOtherModels(t *testing.T) {
	selector := &Selector{
		concurrency:    memory.NewConcurrencyLimiter(),
		lastSelectedAt: make(map[uint64]time.Time),
	}
	if err := selector.MarkModelSoftStop(context.Background(), 1, "grok-imagine-image"); err != nil {
		t.Fatal(err)
	}
	values := []account.RoutingCandidate{
		{Credential: account.Credential{ID: 1, Priority: 1}},
		{Credential: account.Credential{ID: 2, Priority: 1}},
	}
	if err := selector.sortCandidates(context.Background(), values, time.Now().UTC(), nil, "grok-fast"); err != nil {
		t.Fatal(err)
	}
	if values[0].Credential.ID != 1 {
		t.Fatalf("other model order=%v, want account 1 unchanged", []uint64{values[0].Credential.ID, values[1].Credential.ID})
	}
}

// assertPersistedModelOutcome 校验排序所依赖的持久化前置条件。
//
// 重启后的排序完全由 sortCandidates 的 modelRanks 决定：soft-stop 账号要拿到
// rank 2，必须同时满足 Status=soft_stop、CooldownUntil 非空、且仍在未来；
// 成功账号要拿到 rank 0，必须 Status=available 且 LastSuccessAt 在
// modelOutcomeSuccessTTL 内。任一条件不成立，两个账号就会同为默认 rank 1，
// 排序退化到最末的 ID 升序 tie-break，表现为难以解读的顺序错乱
// （CI 上曾报 "acquire 1 account = 1, want 2"）。
//
// 在这里显式断言，可让前置条件失效时直接指出是哪个字段的问题。
func assertPersistedModelOutcome(t *testing.T, accounts repository.AccountRepository, ctx context.Context, softStoppedID, succeededID uint64) {
	t.Helper()
	states, err := accounts.GetModelStates(ctx, []uint64{softStoppedID, succeededID})
	if err != nil {
		t.Fatalf("读取持久化模型状态: %v", err)
	}
	pick := func(id uint64) *account.ModelState {
		for _, state := range states[id] {
			if state.UpstreamModel == "grok-imagine-image" {
				return &state
			}
		}
		return nil
	}
	now := time.Now().UTC()
	soft := pick(softStoppedID)
	switch {
	case soft == nil:
		t.Fatalf("账号 %d 未持久化 grok-imagine-image 模型状态", softStoppedID)
	case soft.Status != account.ModelStatusSoftStop:
		t.Fatalf("账号 %d 状态 = %q，want soft_stop", softStoppedID, soft.Status)
	case soft.CooldownUntil == nil:
		t.Fatalf("账号 %d 的 CooldownUntil 为空，soft-stop 降权会丢失", softStoppedID)
	case !now.Before(*soft.CooldownUntil):
		t.Fatalf("账号 %d 的 CooldownUntil=%s 已过期（now=%s），soft-stop 降权会丢失",
			softStoppedID, soft.CooldownUntil.Format(time.RFC3339Nano), now.Format(time.RFC3339Nano))
	}
	ok := pick(succeededID)
	switch {
	case ok == nil:
		t.Fatalf("账号 %d 未持久化 grok-imagine-image 模型状态", succeededID)
	case ok.Status != account.ModelStatusAvailable:
		t.Fatalf("账号 %d 状态 = %q，want available", succeededID, ok.Status)
	case ok.LastSuccessAt == nil:
		t.Fatalf("账号 %d 的 LastSuccessAt 为空，成功加权会丢失", succeededID)
	case now.Sub(*ok.LastSuccessAt) > modelOutcomeSuccessTTL:
		t.Fatalf("账号 %d 的 LastSuccessAt=%s 已超出 TTL %s，成功加权会丢失",
			succeededID, ok.LastSuccessAt.Format(time.RFC3339Nano), modelOutcomeSuccessTTL)
	}
}

func TestSelectorPersistsModelOutcomeRankingAcrossRestart(t *testing.T) {
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "persisted-model-outcome.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	accounts := relational.NewAccountRepository(database)
	create := func(name string) account.Credential {
		value, _, createErr := accounts.UpsertByIdentity(ctx, account.Credential{
			Provider: account.ProviderWeb, AuthType: account.AuthTypeSSO, WebTier: account.WebTierBasic,
			Name: name, SourceKey: name, EncryptedAccessToken: "encrypted", Enabled: true,
			AuthStatus: account.AuthStatusActive, Priority: 1, MaxConcurrent: 1,
		})
		if createErr != nil {
			t.Fatal(createErr)
		}
		seedImagineQuota(t, accounts, ctx, value.ID, 5, 10)
		return value
	}
	softStopped := create("soft-stopped")
	unknown := create("unknown")
	succeeded := create("succeeded")
	selector := newTestSelector(accounts, memory.NewConcurrencyLimiter(), memory.NewStickyStore(), nil, time.Hour, time.Minute, time.Hour)
	if err := selector.MarkModelSoftStop(ctx, softStopped.ID, "grok-imagine-image"); err != nil {
		t.Fatal(err)
	}
	if err := selector.MarkModelSuccess(ctx, succeeded.ID, "grok-imagine-image"); err != nil {
		t.Fatal(err)
	}
	assertPersistedModelOutcome(t, accounts, ctx, softStopped.ID, succeeded.ID)

	selector = newTestSelector(accounts, memory.NewConcurrencyLimiter(), memory.NewStickyStore(), nil, time.Hour, time.Minute, time.Hour)
	excluded := map[uint64]bool{}
	want := []uint64{succeeded.ID, unknown.ID, softStopped.ID}
	for index, wantID := range want {
		lease, acquireErr := selector.Acquire(ctx, account.ProviderWeb, "grok-imagine-image", "imagine", "", excluded, false)
		if acquireErr != nil {
			t.Fatalf("acquire %d: %v", index, acquireErr)
		}
		if lease.Credential.ID != wantID {
			t.Fatalf("acquire %d account = %d, want %d", index, lease.Credential.ID, wantID)
		}
		excluded[lease.Credential.ID] = true
		lease.Release()
	}
}

func TestSelectorConsumesOnlyMatchingQuotaSnapshot(t *testing.T) {
	key := candidateCacheKey{provider: account.ProviderWeb, upstreamModel: "chat", quotaMode: "fast"}
	selector := &Selector{candidates: map[candidateCacheKey]candidateSnapshot{
		key: {values: []account.RoutingCandidate{{
			Credential: account.Credential{ID: 7}, QuotaWindow: &account.QuotaWindow{AccountID: 7, Mode: "fast", Remaining: 10},
		}}},
	}}
	selector.ConsumeQuota(account.ProviderWeb, 7, "fast", 3)
	window := selector.candidates[key].values[0].QuotaWindow
	if window == nil || window.Remaining != 7 {
		t.Fatalf("quota window = %#v", window)
	}
}

func TestSelectorTreatsZeroTotalModelQuotaAsUnknown(t *testing.T) {
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "unknown-model-quota.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	accounts := relational.NewAccountRepository(database)
	credential, _, err := accounts.UpsertByIdentity(ctx, account.Credential{
		Provider: account.ProviderWeb, AuthType: account.AuthTypeSSO, WebTier: account.WebTierBasic,
		Name: "unknown-imagine", SourceKey: "unknown-imagine", EncryptedAccessToken: "encrypted",
		Enabled: true, AuthStatus: account.AuthStatusActive, MaxConcurrent: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	if err := accounts.SaveQuotaWindows(ctx, credential.ID, account.WebTierBasic, now, []account.QuotaWindow{{
		AccountID: credential.ID, Mode: "imagine", Remaining: 0, Total: 0,
		SyncedAt: &now, Source: account.QuotaSourceUpstream, UpdatedAt: now,
	}}); err != nil {
		t.Fatal(err)
	}
	selector := newTestSelector(accounts, memory.NewConcurrencyLimiter(), memory.NewStickyStore(), nil, time.Hour, time.Second, time.Minute)
	if _, err := selector.Acquire(ctx, account.ProviderWeb, "grok-imagine-image", "imagine", "", nil, false); err == nil {
		t.Fatal("0/0 imagine quota should be blocked until positive upstream evidence exists")
	} else {
		var unavailable *SelectionUnavailableError
		if !errors.As(err, &unavailable) || unavailable.Reason != SelectionQuotaExhausted {
			t.Fatalf("0/0 imagine quota error = %v", err)
		}
	}
	if err := accounts.SaveQuotaWindows(ctx, credential.ID, account.WebTierBasic, now, []account.QuotaWindow{{
		AccountID: credential.ID, Mode: "imagine", Remaining: 0, Total: 10,
		SyncedAt: &now, Source: account.QuotaSourceUpstream, UpdatedAt: now,
	}}); err != nil {
		t.Fatal(err)
	}
	selector = newTestSelector(accounts, memory.NewConcurrencyLimiter(), memory.NewStickyStore(), nil, time.Hour, time.Second, time.Minute)
	if _, err := selector.Acquire(ctx, account.ProviderWeb, "grok-imagine-image", "imagine", "", nil, false); err == nil {
		t.Fatal("0/10 model quota should be blocked as exhausted")
	} else {
		var unavailable *SelectionUnavailableError
		if !errors.As(err, &unavailable) || unavailable.Reason != SelectionQuotaExhausted {
			t.Fatalf("0/10 model quota error = %v", err)
		}
	}
	if err := accounts.SaveQuotaWindows(ctx, credential.ID, account.WebTierSuper, now, []account.QuotaWindow{
		{AccountID: credential.ID, Mode: "weekly", Remaining: 0, Total: 10000, SyncedAt: &now, Source: account.QuotaSourceUpstream, UpdatedAt: now},
		{AccountID: credential.ID, Mode: "imagine", Remaining: 5, Total: 10, SyncedAt: &now, Source: account.QuotaSourceUpstream, UpdatedAt: now},
	}); err != nil {
		t.Fatal(err)
	}
	if err := accounts.UpsertModelQuotaBlock(ctx, account.ModelQuotaBlock{
		AccountID: credential.ID, UpstreamModel: "grok-imagine-image", Reason: "old_usage_limit",
		CooldownUntil: now.Add(time.Hour), UpdatedAt: now,
	}); err != nil {
		t.Fatal(err)
	}
	selector = newTestSelector(accounts, memory.NewConcurrencyLimiter(), memory.NewStickyStore(), nil, time.Hour, time.Second, time.Minute)
	lease, err := selector.Acquire(ctx, account.ProviderWeb, "grok-imagine-image", "imagine", "", nil, false)
	if err != nil {
		t.Fatalf("explicit Imagine quota should take precedence over weekly: %v", err)
	}
	lease.Release()
}

func TestSelectorWaitsBrieflyForAccountCapacity(t *testing.T) {
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "capacity-wait.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	accounts := relational.NewAccountRepository(database)
	if _, _, err := accounts.UpsertByIdentity(ctx, account.Credential{
		Provider: account.ProviderBuild, Name: "capacity", SourceKey: "capacity", EncryptedAccessToken: "encrypted",
		Enabled: true, AuthStatus: account.AuthStatusActive, MaxConcurrent: 1,
		ObservedModel: "grok-4.5-build-free",
	}); err != nil {
		t.Fatal(err)
	}
	selector := newTestSelector(accounts, memory.NewConcurrencyLimiter(), memory.NewStickyStore(), nil, time.Hour, time.Second, time.Minute, 300*time.Millisecond)
	first, err := selector.Acquire(ctx, account.ProviderBuild, "model", "", "", nil, false)
	if err != nil {
		t.Fatal(err)
	}
	type result struct {
		lease *accountLease
		err   error
	}
	resultCh := make(chan result, 1)
	go func() {
		lease, acquireErr := selector.Acquire(ctx, account.ProviderBuild, "model", "", "", nil, false)
		resultCh <- result{lease: lease, err: acquireErr}
	}()
	select {
	case value := <-resultCh:
		t.Fatalf("second acquire returned before capacity release: %v", value.err)
	case <-time.After(30 * time.Millisecond):
	}
	first.Release()
	select {
	case value := <-resultCh:
		if value.err != nil || value.lease == nil {
			t.Fatalf("second acquire lease=%v err=%v", value.lease, value.err)
		}
		value.lease.Release()
	case <-time.After(time.Second):
		t.Fatal("second acquire did not wake after capacity release")
	}
}

func TestSelectorSerializesWebLiteImagePerAccount(t *testing.T) {
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "web-lite-account-capacity.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	accounts := relational.NewAccountRepository(database)
	preferred, _, err := accounts.UpsertByIdentity(ctx, account.Credential{
		Provider: account.ProviderWeb, AuthType: account.AuthTypeSSO, WebTier: account.WebTierBasic,
		Name: "preferred", SourceKey: "preferred", EncryptedAccessToken: "encrypted", Enabled: true,
		AuthStatus: account.AuthStatusActive, Priority: 100, MaxConcurrent: 4,
	})
	if err != nil {
		t.Fatal(err)
	}
	alternate, _, err := accounts.UpsertByIdentity(ctx, account.Credential{
		Provider: account.ProviderWeb, AuthType: account.AuthTypeSSO, WebTier: account.WebTierBasic,
		Name: "alternate", SourceKey: "alternate", EncryptedAccessToken: "encrypted", Enabled: true,
		AuthStatus: account.AuthStatusActive, Priority: 50, MaxConcurrent: 4,
	})
	if err != nil {
		t.Fatal(err)
	}
	seedImagineQuota(t, accounts, ctx, preferred.ID, 8, 10)
	seedImagineQuota(t, accounts, ctx, alternate.ID, 6, 10)
	selector := newTestSelector(accounts, memory.NewConcurrencyLimiter(), memory.NewStickyStore(), nil, time.Hour, time.Second, time.Minute)
	if err := selector.MarkModelSuccess(ctx, preferred.ID, "grok-imagine-image"); err != nil {
		t.Fatal(err)
	}

	first, err := selector.Acquire(ctx, account.ProviderWeb, "grok-imagine-image", "imagine", "", nil, false)
	if err != nil {
		t.Fatal(err)
	}
	defer first.Release()
	if first.Credential.ID != preferred.ID {
		t.Fatalf("first account = %d, want preferred %d", first.Credential.ID, preferred.ID)
	}

	second, err := selector.Acquire(ctx, account.ProviderWeb, "grok-imagine-image", "imagine", "", nil, false)
	if err != nil {
		t.Fatal(err)
	}
	defer second.Release()
	if second.Credential.ID != alternate.ID {
		t.Fatalf("second account = %d, want alternate %d", second.Credential.ID, alternate.ID)
	}
}

func TestAccountConcurrencyLimitOnlySerializesWebLiteImage(t *testing.T) {
	credential := account.Credential{MaxConcurrent: 4}
	if got := accountConcurrencyLimit(credential, "grok-imagine-image"); got != 1 {
		t.Fatalf("Lite image limit = %d, want 1", got)
	}
	if got := accountConcurrencyLimit(credential, "grok-chat-fast"); got != 4 {
		t.Fatalf("chat limit = %d, want configured 4", got)
	}
	if got := accountConcurrencyLimit(account.Credential{}, "grok-chat-fast"); got != account.DefaultMaxConcurrent {
		t.Fatalf("default chat limit = %d, want %d", got, account.DefaultMaxConcurrent)
	}
}

func TestSelectorAppliesPersistedCooldownOnlyToMatchingModel(t *testing.T) {
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "model-cooldown.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	accounts := relational.NewAccountRepository(database)
	credential, _, err := accounts.UpsertByIdentity(ctx, account.Credential{
		Provider: account.ProviderBuild, Name: "model-cooling", SourceKey: "model-cooling", EncryptedAccessToken: "encrypted",
		Enabled: true, AuthStatus: account.AuthStatusActive, MaxConcurrent: 1,
		ObservedModel: "grok-4.5-build-free",
	})
	if err != nil {
		t.Fatal(err)
	}
	until := time.Now().UTC().Add(time.Hour)
	if err := accounts.UpsertModelQuotaBlock(ctx, account.ModelQuotaBlock{AccountID: credential.ID, UpstreamModel: "limited-model", Reason: "test", CooldownUntil: until}); err != nil {
		t.Fatal(err)
	}
	if err := accounts.UpsertModelQuotaBlock(ctx, account.ModelQuotaBlock{AccountID: credential.ID, UpstreamModel: "limited-model", Reason: "shorter", CooldownUntil: time.Now().UTC().Add(time.Minute)}); err != nil {
		t.Fatal(err)
	}
	selector := newTestSelector(accounts, memory.NewConcurrencyLimiter(), memory.NewStickyStore(), nil, time.Hour, time.Second, time.Minute)
	if _, err := selector.Acquire(ctx, account.ProviderBuild, "limited-model", "", "", nil, false); err == nil {
		t.Fatal("matching model cooldown was ignored")
	} else {
		var unavailable *SelectionUnavailableError
		if !errors.As(err, &unavailable) || unavailable.Reason != SelectionModelCooling || unavailable.RetryAfter < 30*time.Minute {
			t.Fatalf("error = %v", err)
		}
	}
	lease, err := selector.Acquire(ctx, account.ProviderBuild, "other-model", "", "", nil, false)
	if err != nil {
		t.Fatalf("other model was blocked: %v", err)
	}
	lease.Release()
}

type failingConcurrencyLimiter struct{ err error }

type batchConcurrencyLimiter struct {
	values       map[string]int
	batchCalls   int
	currentCalls int
}

func (l *batchConcurrencyLimiter) Acquire(context.Context, string, int) (func(), bool, error) {
	return func() {}, true, nil
}

func (l *batchConcurrencyLimiter) Current(context.Context, string) (int, error) {
	l.currentCalls++
	return 0, nil
}

func (l *batchConcurrencyLimiter) CurrentMany(_ context.Context, keys []string) (map[string]int, error) {
	l.batchCalls++
	values := make(map[string]int, len(keys))
	for _, key := range keys {
		values[key] = l.values[key]
	}
	return values, nil
}

type staticTierOrder struct{ order []account.WebTier }

func (value staticTierOrder) TierOrder(account.Provider, string) []account.WebTier {
	return value.order
}

func (f failingConcurrencyLimiter) Acquire(context.Context, string, int) (func(), bool, error) {
	return nil, false, f.err
}

func (f failingConcurrencyLimiter) Current(context.Context, string) (int, error) {
	return 0, nil
}

func TestExplorationShuffleSkipsWhenEpsilonZero(t *testing.T) {
	selector := &Selector{explorationEpsilon: 0, randFloat: func() float64 { return 0 }}
	values := []account.RoutingCandidate{
		{Credential: account.Credential{ID: 1, Priority: 10}},
		{Credential: account.Credential{ID: 2, Priority: 1}},
	}
	selector.maybeExploreShuffle(values)
	if values[0].Credential.ID != 1 {
		t.Fatalf("epsilon=0 should preserve order, got %#v", values)
	}
}

func TestExplorationShuffleReordersWhenEpsilonOne(t *testing.T) {
	step := 0
	selector := &Selector{
		explorationEpsilon: 1,
		randFloat: func() float64 {
			step++
			if step == 1 {
				return 0 // trigger explore
			}
			return 0 // always pick index 0 in Fisher-Yates
		},
	}
	values := []account.RoutingCandidate{
		{Credential: account.Credential{ID: 1, Priority: 100}},
		{Credential: account.Credential{ID: 2, Priority: 1}},
		{Credential: account.Credential{ID: 3, Priority: 1}},
	}
	selector.maybeExploreShuffle(values)
	if values[0].Credential.ID == 1 {
		t.Fatal("epsilon=1 should shuffle candidate order")
	}
}

func TestExplorationShufflePreservesCandidateSet(t *testing.T) {
	selector := &Selector{explorationEpsilon: 1, randFloat: func() float64 { return 0.42 }}
	values := []account.RoutingCandidate{
		{Credential: account.Credential{ID: 10}},
		{Credential: account.Credential{ID: 20}},
		{Credential: account.Credential{ID: 30}},
	}
	selector.maybeExploreShuffle(values)
	seen := map[uint64]bool{}
	for _, value := range values {
		seen[value.Credential.ID] = true
	}
	if len(seen) != 3 || !seen[10] || !seen[20] || !seen[30] {
		t.Fatalf("shuffle must preserve candidate set: %#v", values)
	}
}

func seedImagineQuota(t *testing.T, accounts *relational.AccountRepository, ctx context.Context, accountID uint64, remaining, total int) {
	t.Helper()
	now := time.Now().UTC()
	if err := accounts.SaveQuotaWindows(ctx, accountID, account.WebTierBasic, now, []account.QuotaWindow{{
		AccountID: accountID, Mode: "imagine", Remaining: remaining, Total: total,
		SyncedAt: &now, Source: account.QuotaSourceUpstream, UpdatedAt: now,
	}}); err != nil {
		t.Fatal(err)
	}
}

// newTestSelector 构造关闭 epsilon-greedy 探索的 Selector。
//
// 生产默认 defaultExplorationEpsilon=0.05：每次选号有 5% 概率调用
// maybeExploreShuffle 随机打乱候选顺序，用于避免长期饿死低排名账号。任何断言
// 「选中哪个账号」或「候选顺序」的用例都必须关掉它，否则会偶发失败 —— 整包
// -count=40 压测曾同时暴露 4 个用例受此影响。
//
// 需要验证探索行为本身的用例请直接构造 &Selector{explorationEpsilon: ...}。
func newTestSelector(accounts repository.AccountRepository, concurrency repository.ConcurrencyLimiter, sticky repository.StickySessionRepository, tierOrders interface {
	TierOrder(account.Provider, string) []account.WebTier
}, stickyTTL, cooldownBase, cooldownMax time.Duration, capacityWait ...time.Duration) *Selector {
	selector := NewSelector(accounts, concurrency, sticky, tierOrders, stickyTTL, cooldownBase, cooldownMax, capacityWait...)
	selector.explorationEpsilon = 0
	return selector
}

type staticChromeTicketSource map[uint64]int64

func (s staticChromeTicketSource) AvailableCounts(context.Context) map[uint64]int64 {
	return map[uint64]int64(s)
}

func TestSelectorPrefersChromeTicketHoldersAndFallsBackWhenPoolEmpty(t *testing.T) {
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "chrome-ticket-filter.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	accounts := relational.NewAccountRepository(database)
	withTicket, _, err := accounts.UpsertByIdentity(ctx, account.Credential{
		Provider: account.ProviderWeb, AuthType: account.AuthTypeSSO, WebTier: account.WebTierBasic,
		Name: "ticketed", SourceKey: "ticketed", EncryptedAccessToken: "encrypted", Enabled: true,
		AuthStatus: account.AuthStatusActive, Priority: 10, MaxConcurrent: 4,
	})
	if err != nil {
		t.Fatal(err)
	}
	withoutTicket, _, err := accounts.UpsertByIdentity(ctx, account.Credential{
		Provider: account.ProviderWeb, AuthType: account.AuthTypeSSO, WebTier: account.WebTierBasic,
		Name: "bare", SourceKey: "bare", EncryptedAccessToken: "encrypted", Enabled: true,
		AuthStatus: account.AuthStatusActive, Priority: 100, MaxConcurrent: 4,
	})
	if err != nil {
		t.Fatal(err)
	}
	seedImagineQuota(t, accounts, ctx, withTicket.ID, 8, 10)
	seedImagineQuota(t, accounts, ctx, withoutTicket.ID, 8, 10)
	selector := newTestSelector(accounts, memory.NewConcurrencyLimiter(), memory.NewStickyStore(), nil, time.Hour, time.Second, time.Minute)
	selector.SetChromeTicketSource(staticChromeTicketSource{})

	// 票池为空时不再硬失败，回退到无票路径。
	emptyPoolLease, err := selector.Acquire(ctx, account.ProviderWeb, "grok-imagine-image", "imagine", "", nil, false)
	if err != nil {
		t.Fatalf("expected fallback to ticketless path when pool empty, got %v", err)
	}
	fallbackID := emptyPoolLease.Credential.ID
	emptyPoolLease.Release()

	// 有票时票偏好生效：选中持票账号，而不是回退时的那个。
	selector.SetChromeTicketSource(staticChromeTicketSource{withTicket.ID: 1})
	if fallbackID == withTicket.ID {
		t.Fatalf("fallback already picked the ticket holder %d, preference is untestable", withTicket.ID)
	}
	lease, err := selector.Acquire(ctx, account.ProviderWeb, "grok-imagine-image", "imagine", "", nil, false)
	if err != nil {
		t.Fatal(err)
	}
	defer lease.Release()
	if lease.Credential.ID != withTicket.ID {
		t.Fatalf("selected account = %d, want ticketed %d", lease.Credential.ID, withTicket.ID)
	}
}
