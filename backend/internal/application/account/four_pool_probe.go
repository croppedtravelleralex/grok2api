package account

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/chenyme/grok2api/backend/internal/application/account/poolindex"
	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
	"github.com/chenyme/grok2api/backend/internal/repository"
)

const (
	PoolDispatch     = "dispatch"
	PoolNormal       = "normal"
	PoolVerification = "verification"
	PoolDelete       = "delete"

	buildDispatchFailLimit = 2
	deletablePrefix        = "deletable:"
)

func (s *Service) initPoolIndex() {
	s.buildProbeMu.Lock()
	defer s.buildProbeMu.Unlock()
	if s.dispatchIndex != nil {
		return
	}
	s.dispatchIndex = poolindex.NewDispatchIndex()
	s.verifyHeap = poolindex.NewDueHeap()
	s.normalHeap = poolindex.NewDueHeap()
	s.deleteHeap = poolindex.NewDueHeap()
	s.dispatchProbeHeap = poolindex.NewDueHeap()
	s.maintenanceDRR = poolindex.NewDRRScheduler()
}

func (s *Service) ensurePoolIndexWarm(ctx context.Context) {
	s.initPoolIndex()
	s.buildProbeMu.Lock()
	empty := s.verifyHeap.Len()+s.normalHeap.Len()+s.deleteHeap.Len()+s.dispatchIndex.Len() == 0
	s.buildProbeMu.Unlock()
	if empty {
		_ = s.RebuildBuildPoolIndex(ctx)
	}
}

// EnsurePoolIndexWarm 在索引全空时从 DB 重建四池索引（Selector 热路径兜底）。
func (s *Service) EnsurePoolIndexWarm(ctx context.Context) {
	s.ensurePoolIndexWarm(ctx)
}

// RebuildBuildPoolIndex 从数据库重建四池热路径索引。
func (s *Service) RebuildBuildPoolIndex(ctx context.Context) error {
	s.initPoolIndex()
	values, _, err := s.accounts.List(ctx, repository.AccountListQuery{
		Page:   repository.PageQuery{Limit: maxCredentialExportAccounts},
		Filter: repository.AccountListFilter{Provider: string(accountdomain.ProviderBuild), Now: s.now()},
	})
	if err != nil {
		return mapRepositoryError(err)
	}
	ids := make([]uint64, 0, len(values))
	for _, value := range values {
		ids = append(ids, value.ID)
	}
	recoveries, err := s.accounts.GetQuotaRecoveries(ctx, ids)
	if err != nil {
		return mapRepositoryError(err)
	}
	billings, err := s.accounts.GetBillings(ctx, ids)
	if err != nil {
		return mapRepositoryError(err)
	}
	s.buildProbeMu.Lock()
	s.dispatchIndex = poolindex.NewDispatchIndex()
	s.verifyHeap = poolindex.NewDueHeap()
	s.normalHeap = poolindex.NewDueHeap()
	s.deleteHeap = poolindex.NewDueHeap()
	s.dispatchProbeHeap = poolindex.NewDueHeap()
	s.maintenanceDRR = poolindex.NewDRRScheduler()
	now := s.now()
	for _, value := range values {
		var recovery *accountdomain.QuotaRecovery
		if item, ok := recoveries[value.ID]; ok {
			copy := item
			recovery = &copy
		}
		var billing *accountdomain.Billing
		if item, ok := billings[value.ID]; ok {
			copy := item
			billing = &copy
		}
		s.indexAccountLocked(value, recovery, billing, now)
	}
	s.buildProbeMu.Unlock()
	return nil
}

// MigrateBuildDeadAccountsToDeletePool 将旧恢复/退役 backlog 标为可删。
func (s *Service) MigrateBuildDeadAccountsToDeletePool(ctx context.Context) (int, error) {
	values, _, err := s.accounts.List(ctx, repository.AccountListQuery{
		Page:   repository.PageQuery{Limit: maxCredentialExportAccounts},
		Filter: repository.AccountListFilter{Provider: string(accountdomain.ProviderBuild), Now: s.now()},
	})
	if err != nil {
		return 0, mapRepositoryError(err)
	}
	migrated := 0
	for _, value := range values {
		errText := strings.ToLower(strings.TrimSpace(value.LastError))
		needs := false
		reason := value.LastError
		if value.AuthStatus == accountdomain.AuthStatusReauthRequired {
			needs = true
			if reason == "" {
				reason = "migrated from reauthRequired"
			}
		}
		if !value.Enabled && strings.HasPrefix(errText, "retired:") {
			needs = true
		}
		if !needs || strings.HasPrefix(errText, deletablePrefix) {
			continue
		}
		if err := s.markBuildDeletable(ctx, value.ID, reason); err != nil {
			return migrated, err
		}
		migrated++
	}
	return migrated, nil
}

func (s *Service) indexAccountLocked(value accountdomain.Credential, recovery *accountdomain.QuotaRecovery, billing *accountdomain.Billing, now time.Time) {
	s.verifyHeap.Remove(value.ID)
	s.normalHeap.Remove(value.ID)
	s.deleteHeap.Remove(value.ID)
	s.dispatchProbeHeap.Remove(value.ID)
	s.dispatchIndex.Remove(value.ID)
	pool := AccountPoolAt(value, now, recovery)
	if pool == "" {
		return
	}
	due := now
	if value.CooldownUntil != nil && value.CooldownUntil.After(now) {
		due = *value.CooldownUntil
	}
	switch pool {
	case PoolVerification:
		s.verifyHeap.Upsert(value.ID, value.CreatedAt)
	case PoolNormal:
		if recovery != nil && recovery.NextProbeAt != nil && recovery.NextProbeAt.After(due) {
			due = *recovery.NextProbeAt
		}
		s.normalHeap.Upsert(value.ID, due)
	case PoolDelete:
		s.deleteHeap.Upsert(value.ID, value.UpdatedAt)
	case PoolDispatch:
		lastSelected := time.Time{}
		if value.LastUsedAt != nil {
			lastSelected = *value.LastUsedAt
		}
		quotaKnown, quotaRemaining := poolindex.DispatchQuota(billing, recovery)
		s.dispatchIndex.Upsert(poolindex.DispatchEntry{
			ID: value.ID, Priority: value.Priority, QuotaKnown: quotaKnown, QuotaRemaining: quotaRemaining, LastSelectedAt: lastSelected,
		})
		probeAt := value.UpdatedAt
		if probeAt.IsZero() {
			probeAt = now.Add(-time.Hour)
		}
		s.dispatchProbeHeap.Upsert(value.ID, probeAt)
	}
}

func (s *Service) syncAccountIndex(ctx context.Context, id uint64) {
	s.initPoolIndex()
	value, err := s.accounts.Get(ctx, id)
	if err != nil {
		s.buildProbeMu.Lock()
		s.verifyHeap.Remove(id)
		s.normalHeap.Remove(id)
		s.deleteHeap.Remove(id)
		s.dispatchProbeHeap.Remove(id)
		s.dispatchIndex.Remove(id)
		s.buildProbeMu.Unlock()
		return
	}
	var recovery *accountdomain.QuotaRecovery
	if items, err := s.accounts.GetQuotaRecoveries(ctx, []uint64{id}); err == nil {
		if item, ok := items[id]; ok {
			copy := item
			recovery = &copy
		}
	}
	var billing *accountdomain.Billing
	if AccountPoolAt(value, s.now(), recovery) == PoolDispatch {
		if items, err := s.accounts.GetBillings(ctx, []uint64{id}); err == nil {
			if item, ok := items[id]; ok {
				copy := item
				billing = &copy
			}
		}
	}
	s.buildProbeMu.Lock()
	s.indexAccountLocked(value, recovery, billing, s.now())
	s.buildProbeMu.Unlock()
}

// AccountPoolAt 返回四池之一：dispatch / normal / verification / delete。
// 手动禁用（无 deletable:/retired: 前缀）返回空串，不进入四池索引。
func AccountPoolAt(value accountdomain.Credential, now time.Time, recovery *accountdomain.QuotaRecovery) string {
	errText := strings.ToLower(strings.TrimSpace(value.LastError))
	if strings.HasPrefix(errText, deletablePrefix) || strings.HasPrefix(errText, "retired:") {
		return PoolDelete
	}
	if !value.Enabled {
		return ""
	}
	if value.AuthStatus == accountdomain.AuthStatusReauthRequired {
		return PoolDelete
	}
	if value.Provider == accountdomain.ProviderBuild && strings.TrimSpace(value.ObservedModel) == "" {
		return PoolVerification
	}
	if recovery != nil && (recovery.Status == accountdomain.QuotaRecoveryStatusExhausted || recovery.Status == accountdomain.QuotaRecoveryStatusProbing) {
		return PoolNormal
	}
	if value.CooldownUntil != nil && value.CooldownUntil.After(now) {
		return PoolNormal
	}
	return PoolDispatch
}

// DueNormalProbeIDs 返回到期普通池账号 ID，供 Selector 合并 quota probe 候选。
func (s *Service) DueNormalProbeIDs(now time.Time, limit int) []uint64 {
	s.initPoolIndex()
	s.buildProbeMu.Lock()
	defer s.buildProbeMu.Unlock()
	return s.normalHeap.DueIDs(now, limit)
}

// OrderedDispatchIDs 返回调度池热路径有序 ID，供 Selector 优先试租约。
func (s *Service) OrderedDispatchIDs(limit int) []uint64 {
	s.initPoolIndex()
	s.buildProbeMu.Lock()
	defer s.buildProbeMu.Unlock()
	entries := s.dispatchIndex.Ascend(limit)
	ids := make([]uint64, 0, len(entries))
	for _, entry := range entries {
		ids = append(ids, entry.ID)
	}
	return ids
}

// NoteDispatchSelected 在成功租约后更新调度公平序。
func (s *Service) NoteDispatchSelected(id uint64, at time.Time) {
	s.initPoolIndex()
	s.buildProbeMu.Lock()
	defer s.buildProbeMu.Unlock()
	s.dispatchIndex.TouchSelected(id, at)
}

func (s *Service) markBuildDeletable(ctx context.Context, id uint64, reason string) error {
	value, err := s.accounts.Get(ctx, id)
	if err != nil {
		return mapRepositoryError(err)
	}
	errText := strings.ToLower(strings.TrimSpace(value.LastError))
	if strings.HasPrefix(errText, deletablePrefix) && !value.Enabled {
		s.syncAccountIndex(ctx, id)
		return nil
	}
	reason = strings.TrimSpace(reason)
	if reason == "" {
		reason = "marked deletable"
	}
	value.Enabled = false
	value.AuthStatus = accountdomain.AuthStatusReauthRequired
	value.CooldownUntil = nil
	value.LastError = deletablePrefix + " " + reason
	if len(value.LastError) > 512 {
		value.LastError = value.LastError[:512]
	}
	if _, err := s.accounts.Update(ctx, value); err != nil {
		return mapRepositoryError(err)
	}
	if s.sticky != nil {
		_ = s.sticky.DeleteByAccount(ctx, id)
	}
	s.syncAccountIndex(ctx, id)
	return nil
}

// DispatchProbeTick 调度探针：只巡检调度池。
func (s *Service) DispatchProbeTick(ctx context.Context) (uint64, bool, error) {
	s.ensurePoolIndexWarm(ctx)
	now := s.now()
	s.buildProbeMu.Lock()
	id, ok := s.dispatchProbeHeap.PopDue(now)
	if !ok {
		id, ok = s.dispatchProbeHeap.PopAny()
	}
	s.buildProbeMu.Unlock()
	if !ok {
		return 0, false, nil
	}
	candidate, err := s.accounts.Get(ctx, id)
	if err != nil {
		s.syncAccountIndex(ctx, id)
		return 0, false, mapRepositoryError(err)
	}
	var recovery *accountdomain.QuotaRecovery
	if items, getErr := s.accounts.GetQuotaRecoveries(ctx, []uint64{id}); getErr == nil {
		if item, exists := items[id]; exists {
			copy := item
			recovery = &copy
		}
	}
	if AccountPoolAt(candidate, now, recovery) != PoolDispatch {
		s.syncAccountIndex(ctx, id)
		return id, true, nil
	}
	return s.observeBuildProbe(ctx, candidate, BuildProbeModeDispatch, func() (uint64, bool, error) {
		return s.runDispatchProbe(ctx, candidate)
	})
}

func (s *Service) runDispatchProbe(ctx context.Context, candidate accountdomain.Credential) (uint64, bool, error) {
	ready, err := s.prepareBuildProbeCredential(ctx, candidate, false)
	if err != nil {
		if isTerminalBuildError(err.Error()) {
			_ = s.markBuildDeletable(ctx, candidate.ID, "dispatch refresh failed: "+err.Error())
			return candidate.ID, true, fmt.Errorf("%w: %v", errPurgeDeletable, err)
		}
		until := s.now().Add(15 * time.Minute)
		_ = s.accounts.UpdateHealth(ctx, candidate.ID, candidate.FailureCount+1, &until, "dispatch probe refresh failed", true)
		s.syncAccountIndex(ctx, candidate.ID)
		return candidate.ID, true, err
	}
	s.refreshBuildProbeBilling(ctx, ready.ID)
	_, probeErr := s.probeBuildChatCapabilityOnly(ctx, ready)
	if probeErr != nil {
		reason := probeErr.Error()
		if isTerminalBuildError(reason) || ready.FailureCount+1 >= buildDispatchFailLimit {
			_ = s.markBuildDeletable(ctx, ready.ID, reason)
			return ready.ID, true, fmt.Errorf("%w: %s", errPurgeDeletable, reason)
		}
		until := s.now().Add(15 * time.Minute)
		_ = s.accounts.UpdateHealth(ctx, ready.ID, ready.FailureCount+1, &until, "dispatch probe: "+reason, true)
		s.syncAccountIndex(ctx, ready.ID)
		return ready.ID, true, probeErr
	}
	_ = s.accounts.UpdateHealth(ctx, ready.ID, 0, nil, "", true)
	s.syncAccountIndex(ctx, ready.ID)
	s.buildProbeMu.Lock()
	s.dispatchProbeHeap.Upsert(ready.ID, s.now())
	s.buildProbeMu.Unlock()
	return ready.ID, true, nil
}

// MaintenanceProbeTick 维护探针：DRR 选择验证/普通/删除。
func (s *Service) MaintenanceProbeTick(ctx context.Context) (uint64, bool, error) {
	s.ensurePoolIndexWarm(ctx)
	now := s.now()
	s.buildProbeMu.Lock()
	_, verifyReady := s.verifyHeap.PeekDue(now)
	_, normalReady := s.normalHeap.PeekDue(now)
	deleteReady := s.deleteHeap.Len() > 0
	hasWork := [3]bool{verifyReady, normalReady, deleteReady}
	lane, ok := s.maintenanceDRR.Next(hasWork)
	var id uint64
	if ok {
		switch lane {
		case poolindex.LaneVerification:
			id, ok = s.verifyHeap.PopDue(now)
		case poolindex.LaneNormal:
			id, ok = s.normalHeap.PopDue(now)
		case poolindex.LaneDelete:
			id, ok = s.deleteHeap.PopAny()
		}
	}
	s.buildProbeMu.Unlock()
	if !ok || id == 0 {
		return 0, false, nil
	}
	candidate, err := s.accounts.Get(ctx, id)
	if err != nil {
		s.syncAccountIndex(ctx, id)
		return 0, false, mapRepositoryError(err)
	}
	switch lane {
	case poolindex.LaneVerification:
		return s.observeBuildProbe(ctx, candidate, BuildProbeModeVerification, func() (uint64, bool, error) {
			return s.runVerificationProbe(ctx, candidate)
		})
	case poolindex.LaneNormal:
		return s.observeBuildProbe(ctx, candidate, BuildProbeModeNormal, func() (uint64, bool, error) {
			return s.runNormalProbe(ctx, candidate)
		})
	default:
		return s.observeBuildProbe(ctx, candidate, BuildProbeModeDelete, func() (uint64, bool, error) {
			return s.runDeleteProbe(ctx, candidate)
		})
	}
}

// ProbeNextBuildChat 兼容入口：走维护探针。
func (s *Service) ProbeNextBuildChat(ctx context.Context) (uint64, bool, error) {
	return s.MaintenanceProbeTick(ctx)
}

func (s *Service) runVerificationProbe(ctx context.Context, candidate accountdomain.Credential) (uint64, bool, error) {
	ready, err := s.prepareBuildProbeCredential(ctx, candidate, false)
	if err != nil {
		if isTerminalBuildError(err.Error()) {
			_ = s.markBuildDeletable(ctx, candidate.ID, "verification refresh failed: "+err.Error())
			return candidate.ID, true, err
		}
		s.cooldownBuildProbe(ctx, candidate, 0)
		s.syncAccountIndex(ctx, candidate.ID)
		return candidate.ID, true, err
	}
	s.refreshBuildProbeBilling(ctx, ready.ID)
	accountID, found, probeErr := s.probeBuildChatCredential(ctx, ready, false)
	s.syncAccountIndex(ctx, candidate.ID)
	return accountID, found, probeErr
}

func (s *Service) runNormalProbe(ctx context.Context, candidate accountdomain.Credential) (uint64, bool, error) {
	s.refreshBuildProbeBilling(ctx, candidate.ID)
	ready, err := s.prepareBuildProbeCredential(ctx, candidate, false)
	if err != nil {
		if isTerminalBuildError(err.Error()) {
			_ = s.markBuildDeletable(ctx, candidate.ID, "normal refresh failed: "+err.Error())
			return candidate.ID, true, err
		}
		until := s.now().Add(15 * time.Minute)
		_ = s.accounts.UpdateHealth(ctx, candidate.ID, candidate.FailureCount+1, &until, "normal probe refresh failed", true)
		s.syncAccountIndex(ctx, candidate.ID)
		return candidate.ID, true, err
	}
	observed, probeErr := s.probeBuildChatCapabilityOnly(ctx, ready)
	if probeErr != nil {
		if isTerminalBuildError(probeErr.Error()) {
			_ = s.markBuildDeletable(ctx, ready.ID, probeErr.Error())
			return ready.ID, true, probeErr
		}
		until := s.now().Add(15 * time.Minute)
		_ = s.accounts.UpdateHealth(ctx, ready.ID, ready.FailureCount+1, &until, "normal probe: "+probeErr.Error(), true)
		s.syncAccountIndex(ctx, ready.ID)
		return ready.ID, true, probeErr
	}
	if strings.TrimSpace(observed) != "" {
		_ = s.ObserveResponseModel(ctx, ready.ID, observed)
	}
	_ = s.accounts.UpdateHealth(ctx, ready.ID, 0, nil, "", true)
	_ = s.accounts.ClearQuotaRecovery(ctx, ready.ID)
	s.syncAccountIndex(ctx, ready.ID)
	return ready.ID, true, nil
}

func (s *Service) runDeleteProbe(ctx context.Context, candidate accountdomain.Credential) (uint64, bool, error) {
	if !s.buildProbe.purgeApplyEnabled() {
		return candidate.ID, true, fmt.Errorf("%w: purge apply disabled", errPurgeDeletable)
	}
	errText := strings.ToLower(strings.TrimSpace(candidate.LastError))
	if candidate.Enabled && !strings.HasPrefix(errText, deletablePrefix) && !strings.HasPrefix(errText, "retired:") {
		s.syncAccountIndex(ctx, candidate.ID)
		return candidate.ID, true, nil
	}
	if err := s.Delete(ctx, candidate.ID); err != nil {
		return candidate.ID, true, err
	}
	s.syncAccountIndex(ctx, candidate.ID)
	return candidate.ID, true, fmt.Errorf("%w: deleted", errPurgeDeleted)
}

func isTerminalBuildError(reason string) bool {
	text := strings.ToLower(reason)
	return strings.Contains(text, "invalid_grant") ||
		strings.Contains(text, "access denied") ||
		strings.Contains(text, "permission-denied") ||
		strings.Contains(text, "permission_denied") ||
		strings.Contains(text, "requires a fresh rt") ||
		strings.Contains(text, "credential rejected")
}
