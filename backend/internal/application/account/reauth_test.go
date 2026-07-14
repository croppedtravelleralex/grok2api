package account

import (
	"context"
	"encoding/base64"
	"path/filepath"
	"testing"
	"time"

	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
	"github.com/chenyme/grok2api/backend/internal/infra/persistence/relational"
	"github.com/chenyme/grok2api/backend/internal/infra/provider"
	cliprovider "github.com/chenyme/grok2api/backend/internal/infra/provider/cli"
	webprovider "github.com/chenyme/grok2api/backend/internal/infra/provider/web"
	"github.com/chenyme/grok2api/backend/internal/infra/security"
)

func TestReauthenticateWebAccountKeepsIdentityAndClearsFailureState(t *testing.T) {
	ctx := context.Background()
	service, repository, cipher := newReauthTestService(t)
	result, err := service.ImportWebCredentials(ctx, []byte("old-sso-token"))
	if err != nil {
		t.Fatal(err)
	}
	accountID := result.AccountIDs[0]
	current, err := repository.Get(ctx, accountID)
	if err != nil {
		t.Fatal(err)
	}
	sourceKey := current.SourceKey
	cooldown := time.Now().UTC().Add(time.Hour)
	if err := repository.UpdateHealth(ctx, accountID, 4, &cooldown, "upstream rejected credential", false); err != nil {
		t.Fatal(err)
	}
	if err := service.MarkReauthRequired(ctx, accountID, "credential expired"); err != nil {
		t.Fatal(err)
	}

	updated, err := service.Reauthenticate(ctx, accountID, ReauthenticateInput{SSOToken: "new-sso-token", WebTier: accountdomain.WebTierSuper})
	if err != nil {
		t.Fatal(err)
	}
	if updated.ID != accountID || updated.SourceKey != sourceKey {
		t.Fatalf("identity changed: id=%d source=%q", updated.ID, updated.SourceKey)
	}
	if !updated.Enabled || updated.AuthStatus != accountdomain.AuthStatusActive || updated.FailureCount != 0 || updated.CooldownUntil != nil || updated.LastError != "" {
		t.Fatalf("failure state not cleared: %#v", updated)
	}
	if updated.WebTier != accountdomain.WebTierSuper {
		t.Fatalf("tier = %q", updated.WebTier)
	}
	plain, err := cipher.Decrypt(updated.EncryptedAccessToken)
	if err != nil || plain != "new-sso-token" {
		t.Fatalf("stored SSO token = %q, err = %v", plain, err)
	}
}

func TestReauthenticateBuildAccountAcceptsFreshRefreshToken(t *testing.T) {
	ctx := context.Background()
	service, repository, cipher := newReauthTestService(t)
	result, err := service.ImportCredentials(ctx, []byte(`{"name":"build","user_id":"user-1","refresh_token":"old-refresh"}`))
	if err != nil {
		t.Fatal(err)
	}
	accountID := result.AccountIDs[0]
	current, err := repository.Get(ctx, accountID)
	if err != nil {
		t.Fatal(err)
	}
	current.RefreshFailureCount = 3
	current.LastRefreshErrorCode = "invalid_grant"
	current.AuthStatus = accountdomain.AuthStatusReauthRequired
	current.FailureCount = 2
	current.LastError = "old error"
	if _, err := repository.Update(ctx, current); err != nil {
		t.Fatal(err)
	}

	updated, err := service.Reauthenticate(ctx, accountID, ReauthenticateInput{RefreshToken: "fresh-refresh"})
	if err != nil {
		t.Fatal(err)
	}
	if updated.ID != accountID || updated.SourceKey != current.SourceKey || updated.UserID != "user-1" {
		t.Fatalf("stable build identity changed: %#v", updated)
	}
	if updated.AuthStatus != accountdomain.AuthStatusActive || updated.RefreshFailureCount != 0 || updated.LastRefreshErrorCode != "" || updated.FailureCount != 0 || updated.LastError != "" {
		t.Fatalf("refresh state not cleared: %#v", updated)
	}
	plain, err := cipher.Decrypt(updated.EncryptedRefreshToken)
	if err != nil || plain != "fresh-refresh" {
		t.Fatalf("stored refresh token = %q, err = %v", plain, err)
	}
}

func TestReauthenticateRejectsWrongCredentialShape(t *testing.T) {
	ctx := context.Background()
	service, _, _ := newReauthTestService(t)
	result, err := service.ImportWebCredentials(ctx, []byte("old-sso-token"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := service.Reauthenticate(ctx, result.AccountIDs[0], ReauthenticateInput{RefreshToken: "build-only"}); err == nil {
		t.Fatal("expected Web account reauthentication without SSO token to fail")
	}
}

func newReauthTestService(t *testing.T) (*Service, *relational.AccountRepository, *security.Cipher) {
	t.Helper()
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "reauth.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = database.Close() })
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	cipher, err := security.NewCipher(base64.StdEncoding.EncodeToString(make([]byte, 32)))
	if err != nil {
		t.Fatal(err)
	}
	repository := relational.NewAccountRepository(database)
	registry := provider.NewRegistry(
		cliprovider.NewAdapter(cliprovider.Config{}, cipher),
		webprovider.NewAdapter(webprovider.Config{}, nil, cipher, nil, nil),
	)
	return NewService(repository, nil, nil, nil, registry, cipher, nil), repository, cipher
}
