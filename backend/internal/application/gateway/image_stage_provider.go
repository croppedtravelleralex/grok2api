package gateway

import (
	"context"
	"fmt"
	"sync"
	"time"

	imagepipelineapp "github.com/chenyme/grok2api/backend/internal/application/imagepipeline"
	"github.com/chenyme/grok2api/backend/internal/domain/audit"
	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
)

const stageAccountPoolFallbackThreshold = 3

type stageAccountLease struct {
	inner      *accountLease
	credential accountdomain.Credential
	release    sync.Once
}

func (l *stageAccountLease) Credential() accountdomain.Credential { return l.credential }

func (l *stageAccountLease) Release() {
	l.release.Do(func() {
		if l.inner != nil {
			l.inner.Release()
		}
	})
}

type stageAccountProvider struct {
	service    *Service
	provider   accountdomain.Provider
	upstream   string
	quotaMode  string
	run        *imagepipelineapp.Run
	excluded   map[uint64]bool
	attempts   int
	markTiming func(time.Duration)
	ensureCred func(context.Context, accountdomain.Credential) (accountdomain.Credential, error)
}

func (s *Service) newStageAccountProvider(run *imagepipelineapp.Run, route accountdomain.Provider, upstream, quotaMode string, attempts int, excluded map[uint64]bool, markSelection func(time.Duration), ensureCred func(context.Context, accountdomain.Credential) (accountdomain.Credential, error)) *stageAccountProvider {
	sharedExcluded := excluded
	if sharedExcluded == nil {
		sharedExcluded = make(map[uint64]bool)
	}
	return &stageAccountProvider{
		service: s, provider: route, upstream: upstream, quotaMode: quotaMode, run: run,
		excluded: sharedExcluded, attempts: attempts, markTiming: markSelection, ensureCred: ensureCred,
	}
}

func (p *stageAccountProvider) AcquireForUpload(ctx context.Context) (imagepipelineapp.AccountLease, error) {
	return p.acquire(ctx, nil, p.excluded)
}

func (p *stageAccountProvider) AcquireForPS(ctx context.Context, run *imagepipelineapp.Run) (imagepipelineapp.AccountLease, error) {
	lease, err := p.acquire(ctx, run, p.excluded)
	if err == nil && run != nil {
		run.SetPSAccount(lease.Credential().ID)
	}
	return lease, err
}

func (p *stageAccountProvider) AcquireForSS(ctx context.Context, run *imagepipelineapp.Run) (imagepipelineapp.AccountLease, error) {
	exclude := make(map[uint64]bool, len(p.excluded)+1)
	for id := range p.excluded {
		exclude[id] = true
	}
	if run != nil {
		if id := run.PSAccountID(); id != nil {
			exclude[*id] = true
		}
	}
	if len(exclude) < stageAccountPoolFallbackThreshold {
		lease, err := p.acquire(ctx, run, exclude)
		if err == nil && run != nil {
			run.SetSSCredential(lease.Credential())
		}
		return lease, err
	}
	lease, err := p.acquire(ctx, run, p.excluded)
	if err == nil && run != nil {
		run.SetSSCredential(lease.Credential())
	}
	return lease, err
}

func (p *stageAccountProvider) acquire(ctx context.Context, run *imagepipelineapp.Run, excluded map[uint64]bool) (imagepipelineapp.AccountLease, error) {
	if excluded == nil {
		excluded = make(map[uint64]bool)
	}
	var lastErr error
	for attempt := 0; attempt < p.attempts; attempt++ {
		start := time.Now()
		lease, err := p.service.selector.Acquire(ctx, p.provider, p.upstream, p.quotaMode, "", excluded, false)
		if p.markTiming != nil {
			p.markTiming(time.Since(start))
		}
		if err != nil {
			lastErr = err
			continue
		}
		if p.upstream == webLiteImageUpstreamModel && p.service.accounts != nil {
			refreshed, refreshErr := p.service.accounts.RefreshQuotaMode(ctx, lease.Credential.ID, "imagine")
			if refreshErr != nil {
				lease.Release()
				excluded[lease.Credential.ID] = true
				p.excluded[lease.Credential.ID] = true
				lastErr = refreshErr
				continue
			}
			if refreshed.Total <= 0 || refreshed.Remaining <= 0 {
				lease.Release()
				excluded[lease.Credential.ID] = true
				p.excluded[lease.Credential.ID] = true
				lastErr = fmt.Errorf("imagine quota exhausted after refresh")
				continue
			}
			p.service.selector.MarkQuotaStateChanged(p.provider)
		}
		credential, err := p.ensureCred(ctx, lease.Credential)
		if err != nil {
			lease.Release()
			return nil, err
		}
		excluded[lease.Credential.ID] = true
		p.excluded[lease.Credential.ID] = true
		if run != nil {
			run.SetAccount(credential.ID, credential.Name)
		}
		return &stageAccountLease{inner: lease, credential: credential}, nil
	}
	if lastErr != nil {
		return nil, fmt.Errorf("%w: %w", ErrNoAvailableAccount, lastErr)
	}
	return nil, ErrNoAvailableAccount
}

func isStagedWebImagePipeline(operation audit.Operation, publicModel string, providerValue accountdomain.Provider) bool {
	if providerValue != accountdomain.ProviderWeb {
		return false
	}
	switch operation {
	case audit.OperationImage:
		return publicModel == "grok-imagine-image"
	case audit.OperationImageEdit:
		return true
	default:
		return false
	}
}

var _ imagepipelineapp.AccountProvider = (*stageAccountProvider)(nil)
