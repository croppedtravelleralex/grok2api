package account

import (
	"context"
	"testing"
)

type staticChromeTicketCounts map[uint64]int64

func (s staticChromeTicketCounts) AvailableCounts(context.Context) map[uint64]int64 {
	return map[uint64]int64(s)
}

// pin 默认与 dispatch 全量对齐；配置 imagineSlotAccountIds 时与 SlotRegistry 对齐。
func TestImageDispatchPinTargetIDsKeepsFullDispatchByDefault(t *testing.T) {
	service := NewService(nil, nil, nil, nil, nil, nil, nil)
	service.SetChromeTicketCountsSource(staticChromeTicketCounts{86: 2, 227: 1})
	dispatch := []uint64{86, 87, 227, 250}
	got := service.imageDispatchPinTargetIDs(context.Background(), dispatch)
	if len(got) != len(dispatch) {
		t.Fatalf("pin target = %#v, want full dispatch %#v", got, dispatch)
	}
}

func TestImageDispatchPinTargetIDsUsesSlotRegistryWhenConfigured(t *testing.T) {
	service := NewService(nil, nil, nil, nil, nil, nil, nil)
	service.SetImagineSlotAccountIDs([]uint64{87, 250, 999})
	dispatch := []uint64{86, 87, 227, 250}
	got := service.imageDispatchPinTargetIDs(context.Background(), dispatch)
	want := []uint64{87, 250}
	if len(got) != len(want) {
		t.Fatalf("pin target = %#v, want %#v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("pin target = %#v, want %#v", got, want)
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
