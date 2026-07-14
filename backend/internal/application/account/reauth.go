package account

import (
	"context"
	"strings"
	"time"

	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
	"github.com/chenyme/grok2api/backend/internal/infra/provider"
)

// ReauthenticateInput 是定向替换现有账号凭据的输入，不允许改变账号身份。
type ReauthenticateInput struct {
	Name         string
	AccessToken  string
	RefreshToken string
	OIDCClientID string
	ExpiresAt    time.Time
	SSOToken     string
	WebTier      accountdomain.WebTier
}

// Reauthenticate 使用新 RT 或 SSO 恢复指定账号，并清除旧的认证、刷新和冷却失败状态。
func (s *Service) Reauthenticate(ctx context.Context, id uint64, input ReauthenticateInput) (accountdomain.Credential, error) {
	current, err := s.accounts.Get(ctx, id)
	if err != nil {
		return accountdomain.Credential{}, mapRepositoryError(err)
	}
	name := strings.TrimSpace(input.Name)
	if name == "" {
		name = current.Name
	}
	seed := provider.CredentialSeed{
		Provider: current.Provider, AuthType: current.AuthType, WebTier: current.WebTier,
		Name: name, Email: current.Email, UserID: current.UserID, TeamID: current.TeamID,
		SourceKey: current.SourceKey, OIDCClientID: current.OIDCClientID,
	}
	switch current.Provider {
	case accountdomain.ProviderBuild:
		seed.AccessToken = strings.TrimSpace(input.AccessToken)
		seed.RefreshToken = strings.TrimSpace(input.RefreshToken)
		seed.OIDCClientID = strings.TrimSpace(input.OIDCClientID)
		if seed.OIDCClientID == "" {
			seed.OIDCClientID = current.OIDCClientID
		}
		seed.ExpiresAt = input.ExpiresAt.UTC()
		if seed.AccessToken == "" && seed.RefreshToken == "" {
			return accountdomain.Credential{}, invalidInput("Build 账号必须提供 accessToken 或 refreshToken")
		}
	case accountdomain.ProviderWeb, accountdomain.ProviderConsole:
		seed.AccessToken = strings.TrimSpace(input.SSOToken)
		if seed.AccessToken == "" {
			return accountdomain.Credential{}, invalidInput("Web/Console 账号必须提供 ssoToken")
		}
		if input.WebTier != "" {
			if input.WebTier != accountdomain.WebTierAuto && input.WebTier != accountdomain.WebTierBasic && input.WebTier != accountdomain.WebTierSuper && input.WebTier != accountdomain.WebTierHeavy {
				return accountdomain.Credential{}, invalidInput("webTier 无效")
			}
			seed.WebTier = input.WebTier
		}
	default:
		return accountdomain.Credential{}, ErrUnsupported
	}

	replacement, err := s.credentialFromSeed(seed)
	if err != nil {
		return accountdomain.Credential{}, err
	}
	replacement.ID = current.ID
	replacement.SourceKey = current.SourceKey
	replacement.Enabled = true
	replacement.AuthStatus = accountdomain.AuthStatusActive
	replacement.Priority = current.Priority
	replacement.MaxConcurrent = current.MaxConcurrent
	replacement.MinimumRemaining = current.MinimumRemaining
	replacement.ObservedModel = current.ObservedModel
	replacement.ObservedModelAt = current.ObservedModelAt
	replacement.LastUsedAt = current.LastUsedAt
	replacement.LinkedAccountID = current.LinkedAccountID
	replacement.LinkedAccountName = current.LinkedAccountName
	replacement.LinkedProvider = current.LinkedProvider
	replacement.CreatedAt = current.CreatedAt
	replacement.UpdatedAt = s.now()
	if current.Provider == accountdomain.ProviderBuild && replacement.EncryptedRefreshToken != "" {
		dueAt := s.now()
		replacement.RefreshDueAt = &dueAt
	}
	updated, err := s.accounts.Update(ctx, replacement)
	if err != nil {
		return accountdomain.Credential{}, mapRepositoryError(err)
	}
	s.clearRefreshState(id)
	s.WakeCredentialRefresh()
	return updated, nil
}
