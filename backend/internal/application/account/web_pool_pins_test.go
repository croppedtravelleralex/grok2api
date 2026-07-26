package account

import (
	"context"
	"testing"
)

type staticChromeTicketCounts map[uint64]int64

func (s staticChromeTicketCounts) AvailableCounts(context.Context) map[uint64]int64 {
	return map[uint64]int64(s)
}

// pin 不再按票收窄：票只影响选号排序，不该缩小可选账号集合，
// 否则运行时可选账号会被票的分布绑架（详见 imageDispatchPinTargetIDs 注释）。
func TestImageDispatchPinTargetIDsKeepsFullDispatchWithTickets(t *testing.T) {
	service := NewService(nil, nil, nil, nil, nil, nil, nil)
	service.SetChromeTicketCountsSource(staticChromeTicketCounts{86: 2, 227: 1})
	dispatch := []uint64{86, 87, 227, 250}
	got := service.imageDispatchPinTargetIDs(context.Background(), dispatch)
	if len(got) != len(dispatch) {
		t.Fatalf("pin target = %#v, want full dispatch %#v", got, dispatch)
	}
	for i := range dispatch {
		if got[i] != dispatch[i] {
			t.Fatalf("pin target = %#v, want full dispatch %#v", got, dispatch)
		}
	}
}

func TestImageDispatchPinTargetIDsFallsBackWithoutTickets(t *testing.T) {
	service := NewService(nil, nil, nil, nil, nil, nil, nil)
	dispatch := []uint64{86, 87, 227}
	got := service.imageDispatchPinTargetIDs(context.Background(), dispatch)
	if len(got) != len(dispatch) || got[0] != dispatch[0] || got[1] != dispatch[1] || got[2] != dispatch[2] {
		t.Fatalf("pin target = %#v, want original dispatch", got)
	}
}
