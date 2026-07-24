package account

import (
	"context"

	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
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
