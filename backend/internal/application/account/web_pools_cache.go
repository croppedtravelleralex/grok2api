package account

import (
	"context"
	"sort"
	"time"
)

const webPoolsSummaryCacheTTL = 15 * time.Second

type webPoolsSummarySnapshot struct {
	summary        WebThreePoolSummary
	four           WebFourPoolsPublic
	dispatchIDs    []uint64
	schedulableIDs []uint64
}

func (s *Service) invalidateWebPoolsSummaryCache() {
	s.webPoolsSummaryMu.Lock()
	s.webPoolsSummaryAt = time.Time{}
	s.webPoolsSummaryMu.Unlock()
}

func (s *Service) summarizeWebPoolsCached(ctx context.Context) (WebThreePoolSummary, WebFourPoolsPublic, []uint64, []uint64, error) {
	now := s.now()
	s.webPoolsSummaryMu.Lock()
	if !s.webPoolsSummaryAt.IsZero() && now.Sub(s.webPoolsSummaryAt) < webPoolsSummaryCacheTTL {
		snap := s.webPoolsSummarySnap
		s.webPoolsSummaryMu.Unlock()
		return snap.summary, snap.four, append([]uint64(nil), snap.dispatchIDs...), append([]uint64(nil), snap.schedulableIDs...), nil
	}
	s.webPoolsSummaryMu.Unlock()

	summary, four, dispatchIDs, schedulableIDs, err := s.summarizeWebPools(ctx)
	if err != nil {
		return WebThreePoolSummary{}, WebFourPoolsPublic{}, nil, nil, err
	}
	s.webPoolsSummaryMu.Lock()
	s.webPoolsSummarySnap = webPoolsSummarySnapshot{
		summary:        summary,
		four:           four,
		dispatchIDs:    append([]uint64(nil), dispatchIDs...),
		schedulableIDs: append([]uint64(nil), schedulableIDs...),
	}
	s.webPoolsSummaryAt = now
	s.webPoolsSummaryMu.Unlock()
	return summary, four, dispatchIDs, schedulableIDs, nil
}

// summarizeWebThreePoolsFromIndex 从内存索引读取三池计数，避免探针状态轮询触发全库扫描。
func (s *Service) summarizeWebThreePoolsFromIndex() WebThreePoolSummary {
	s.initWebProbe()
	s.webProbeMu.Lock()
	defer s.webProbeMu.Unlock()
	return WebThreePoolSummary{
		Image: WebLanePoolCounts{
			Dispatch: int64(s.webImageLane.dispatchIndex.Len()),
			Recovery: int64(s.webImageLane.recoveryVerify.Len() + s.webImageLane.recoveryCooldown.Len()),
			Dead:     int64(s.webImageLane.deadHeap.Len()),
		},
		Chat: WebLanePoolCounts{
			Dispatch: int64(s.webChatLane.dispatchIndex.Len()),
			Recovery: int64(s.webChatLane.recoveryVerify.Len() + s.webChatLane.recoveryCooldown.Len()),
			Dead:     int64(s.webChatLane.deadHeap.Len()),
		},
	}
}

func (s *Service) imageDispatchIDsFromIndex() []uint64 {
	s.initWebProbe()
	s.webProbeMu.Lock()
	defer s.webProbeMu.Unlock()
	idSet := s.webImageLane.dispatchIndex.IDs()
	ids := make([]uint64, 0, len(idSet))
	for id := range idSet {
		ids = append(ids, id)
	}
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
	return ids
}
