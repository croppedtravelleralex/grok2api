package account

import (
	"context"
	"testing"
)

type staticChromeTicketCounts map[uint64]int64

func (s staticChromeTicketCounts) AvailableCounts(context.Context) map[uint64]int64 {
	return map[uint64]int64(s)
}

func TestImageDispatchPinTargetIDsPrefersTicketHolders(t *testing.T) {
	service := NewService(nil, nil, nil, nil, nil, nil, nil)
	service.SetChromeTicketCountsSource(staticChromeTicketCounts{86: 2, 227: 1})
	got := service.imageDispatchPinTargetIDs(context.Background(), []uint64{86, 87, 227, 250})
	if len(got) != 2 || got[0] != 86 || got[1] != 227 {
		t.Fatalf("pin target = %#v, want ticket holders only", got)
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
