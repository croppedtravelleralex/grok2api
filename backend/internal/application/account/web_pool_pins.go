package account

import (
	"context"
	"sort"

	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
	"github.com/chenyme/grok2api/backend/internal/repository"
)

// webRoutePinSets 缓存 Web 路由 pin 约束，供 dispatchIndex 构建时求交。
type webRoutePinSets struct {
	imageRestricted bool
	image           map[uint64]struct{}
}

func (s *Service) refreshWebRoutePinSets(ctx context.Context) {
	sets := webRoutePinSets{}
	if s.accounts != nil {
		ids, restricted, err := s.accounts.ListRouteBoundAccountIDs(ctx, accountdomain.ProviderWeb, imagineUpstream)
		if err == nil && restricted {
			sets.imageRestricted = true
			sets.image = make(map[uint64]struct{}, len(ids))
			for _, id := range ids {
				sets.image[id] = struct{}{}
			}
		}
	}
	s.webRoutePins = sets
}

func (s *Service) webLanePinAllowedLocked(lane WebLane, accountID uint64) bool {
	if lane != WebLaneImage || !s.webRoutePins.imageRestricted {
		return true
	}
	_, ok := s.webRoutePins.image[accountID]
	return ok
}

// pinNotInDispatch 返回已 pin 但不在对应 dispatch 索引中的账号 ID。
func (s *Service) pinNotInDispatchLocked() []uint64 {
	if !s.webRoutePins.imageRestricted || len(s.webRoutePins.image) == 0 {
		return nil
	}
	dispatch := make(map[uint64]struct{}, s.webImageLane.dispatchIndex.Len())
	for _, entry := range s.webImageLane.dispatchIndex.Ascend(0) {
		dispatch[entry.ID] = struct{}{}
	}
	out := make([]uint64, 0)
	for id := range s.webRoutePins.image {
		if _, ok := dispatch[id]; !ok {
			out = append(out, id)
		}
	}
	return out
}

// WebSelectionDiagnostics 暴露选号池诊断指标（Admin）。
type WebSelectionDiagnostics struct {
	DispatchImageLen      int `json:"dispatchImageLen"`
	DispatchChatLen       int `json:"dispatchChatLen"`
	PinBoundCount         int `json:"pinBoundCount"`
	PinNotInDispatchCount int `json:"pinNotInDispatchCount"`
}

// WebDispatchPinSyncResult 描述图轨 dispatch pin 与四池 dispatch 对齐结果。
type WebDispatchPinSyncResult struct {
	Changed      bool     `json:"changed"`
	TargetIDs    []uint64 `json:"targetIds"`
	PreviousIDs  []uint64 `json:"previousIds"`
	AddedIDs     []uint64 `json:"addedIds"`
	RemovedIDs   []uint64 `json:"removedIds"`
	Snapshot     WebPoolSnapshot `json:"snapshot"`
}

func (s *Service) webSelectionDiagnosticsLocked() WebSelectionDiagnostics {
	pinCount := 0
	if s.webRoutePins.imageRestricted {
		pinCount = len(s.webRoutePins.image)
	}
	pinNotIn := s.pinNotInDispatchLocked()
	return WebSelectionDiagnostics{
		DispatchImageLen:      s.webImageLane.dispatchIndex.Len(),
		DispatchChatLen:       s.webChatLane.dispatchIndex.Len(),
		PinBoundCount:         pinCount,
		PinNotInDispatchCount: len(pinNotIn),
	}
}

func (s *Service) imagePinIDsLocked() []uint64 {
	if !s.webRoutePins.imageRestricted || len(s.webRoutePins.image) == 0 {
		return nil
	}
	out := make([]uint64, 0, len(s.webRoutePins.image))
	for id := range s.webRoutePins.image {
		out = append(out, id)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

// SyncImageDispatchPins 将 grok-imagine-image pin 与「四池 dispatch ∩ 有 Imagine 生图次数」对齐。
func (s *Service) SyncImageDispatchPins(ctx context.Context) (WebDispatchPinSyncResult, error) {
	if s.accounts == nil {
		return WebDispatchPinSyncResult{}, ErrUnsupported
	}
	targetIDs, err := s.imageDispatchIDsWithGenerations(ctx)
	if err != nil {
		return WebDispatchPinSyncResult{}, err
	}
	targetIDs = s.imageDispatchPinTargetIDs(ctx, targetIDs)
	previousIDs, _, err := s.accounts.ListRouteBoundAccountIDs(ctx, accountdomain.ProviderWeb, imagineUpstream)
	if err != nil {
		return WebDispatchPinSyncResult{}, err
	}
	sort.Slice(previousIDs, func(i, j int) bool { return previousIDs[i] < previousIDs[j] })
	targetCopy := append([]uint64(nil), targetIDs...)
	changed := !equalUint64Slice(previousIDs, targetCopy)
	result := WebDispatchPinSyncResult{
		Changed:     changed,
		TargetIDs:   targetCopy,
		PreviousIDs: previousIDs,
		AddedIDs:    diffUint64Slices(targetCopy, previousIDs),
		RemovedIDs:  diffUint64Slices(previousIDs, targetCopy),
	}
	if changed {
		if err := s.accounts.ReplaceRouteBoundAccountIDs(ctx, accountdomain.ProviderWeb, imagineUpstream, targetCopy); err != nil {
			return WebDispatchPinSyncResult{}, err
		}
		if err := s.RebuildWebPoolIndex(ctx); err != nil {
			return WebDispatchPinSyncResult{}, err
		}
	}
	now := s.now()
	accounts, _, err := s.accounts.List(ctx, repository.AccountListQuery{
		Page:   repository.PageQuery{Limit: maxCredentialExportAccounts},
		Filter: repository.AccountListFilter{Provider: string(accountdomain.ProviderWeb), Now: now},
	})
	if err != nil {
		return WebDispatchPinSyncResult{}, err
	}
	enabled := make([]uint64, 0)
	for _, value := range accounts {
		if value.Enabled {
			enabled = append(enabled, value.ID)
		}
	}
	sort.Slice(enabled, func(i, j int) bool { return enabled[i] < enabled[j] })
	result.Snapshot = s.webPoolSnapshotFromIndex(ctx, now, enabled, 0)
	return result, nil
}

func equalUint64Slice(a, b []uint64) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func diffUint64Slices(from, subtract []uint64) []uint64 {
	sub := make(map[uint64]struct{}, len(subtract))
	for _, id := range subtract {
		sub[id] = struct{}{}
	}
	out := make([]uint64, 0)
	for _, id := range from {
		if _, ok := sub[id]; !ok {
			out = append(out, id)
		}
	}
	return out
}

func (s *Service) imageDispatchPinTargetIDs(ctx context.Context, dispatchIDs []uint64) []uint64 {
	if s.chromeTicketCounts == nil || len(dispatchIDs) == 0 {
		return dispatchIDs
	}
	counts := s.chromeTicketCounts.AvailableCounts(ctx)
	if len(counts) == 0 {
		return dispatchIDs
	}
	dispatchSet := make(map[uint64]struct{}, len(dispatchIDs))
	for _, id := range dispatchIDs {
		dispatchSet[id] = struct{}{}
	}
	ticketIDs := make([]uint64, 0, len(counts))
	for id, count := range counts {
		if count <= 0 {
			continue
		}
		if _, ok := dispatchSet[id]; !ok {
			continue
		}
		ticketIDs = append(ticketIDs, id)
	}
	if len(ticketIDs) == 0 {
		return dispatchIDs
	}
	sort.Slice(ticketIDs, func(i, j int) bool { return ticketIDs[i] < ticketIDs[j] })
	return ticketIDs
}
