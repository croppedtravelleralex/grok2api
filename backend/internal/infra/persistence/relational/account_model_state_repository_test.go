package relational

import (
	"context"
	"testing"
	"time"

	"github.com/chenyme/grok2api/backend/internal/domain/account"
)

func TestAccountModelStatePersistsAndHydratesRoutingCandidate(t *testing.T) {
	ctx := context.Background()
	database := openTestDatabase(t)
	repository := NewAccountRepository(database)
	credential, _, err := repository.UpsertByIdentity(ctx, account.Credential{
		Provider: account.ProviderWeb, AuthType: account.AuthTypeSSO, WebTier: account.WebTierBasic,
		Name: "model-state", SourceKey: "model-state", EncryptedAccessToken: testEncryptedToken,
		Enabled: true, AuthStatus: account.AuthStatusActive, MaxConcurrent: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC().Truncate(time.Second)
	if err := repository.SaveModelState(ctx, account.ModelState{
		AccountID: credential.ID, UpstreamModel: "grok-imagine-image", Status: account.ModelStatusAvailable,
		Reason: "image_generated", LastAttemptAt: now, LastSuccessAt: &now, UpdatedAt: now,
	}); err != nil {
		t.Fatal(err)
	}
	cooldown := now.Add(time.Minute)
	if err := repository.SaveModelState(ctx, account.ModelState{
		AccountID: credential.ID, UpstreamModel: "grok-imagine-image", Status: account.ModelStatusSoftStop,
		Reason: "soft_stop", ConsecutiveFailures: 2, LastAttemptAt: now.Add(time.Second), CooldownUntil: &cooldown, UpdatedAt: now.Add(time.Second),
	}); err != nil {
		t.Fatal(err)
	}
	states, err := repository.GetModelStates(ctx, []uint64{credential.ID})
	if err != nil {
		t.Fatal(err)
	}
	state := states[credential.ID][0]
	if state.Status != account.ModelStatusSoftStop || state.ConsecutiveFailures != 2 || state.LastSuccessAt == nil || !state.LastSuccessAt.Equal(now) || state.CooldownUntil == nil || !state.CooldownUntil.Equal(cooldown) {
		t.Fatalf("state = %#v", state)
	}
	candidates, err := repository.ListRoutingCandidates(ctx, account.ProviderWeb, "grok-imagine-image", "imagine")
	if err != nil {
		t.Fatal(err)
	}
	if len(candidates) != 1 || candidates[0].ModelState == nil || candidates[0].ModelState.Status != account.ModelStatusSoftStop {
		t.Fatalf("candidates = %#v", candidates)
	}
}
