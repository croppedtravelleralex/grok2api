package imagepipeline

import (
	"context"

	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
)

type AccountLease interface {
	Credential() accountdomain.Credential
	Release()
}

type AccountProvider interface {
	AcquireForUpload(ctx context.Context) (AccountLease, error)
	AcquireForPS(ctx context.Context, run *Run) (AccountLease, error)
	AcquireForSS(ctx context.Context, run *Run) (AccountLease, error)
}

type accountProviderKey struct{}

func WithAccountProvider(ctx context.Context, provider AccountProvider) context.Context {
	if provider == nil {
		return ctx
	}
	return context.WithValue(ctx, accountProviderKey{}, provider)
}

func AccountProviderFromContext(ctx context.Context) AccountProvider {
	if ctx == nil {
		return nil
	}
	provider, _ := ctx.Value(accountProviderKey{}).(AccountProvider)
	return provider
}
