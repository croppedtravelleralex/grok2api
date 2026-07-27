package account

import "testing"

func TestImagineSlotRegistryIDsDefaultsToDispatch(t *testing.T) {
	dispatch := []uint64{10, 20, 30}
	got := ImagineSlotRegistryIDs(nil, dispatch)
	if len(got) != 3 || got[0] != 10 || got[1] != 20 || got[2] != 30 {
		t.Fatalf("registry = %v, want %v", got, dispatch)
	}
}

func TestImagineSlotRegistryIDsIntersectsConfigured(t *testing.T) {
	got := ImagineSlotRegistryIDs([]uint64{20, 40}, []uint64{10, 20, 30})
	if len(got) != 1 || got[0] != 20 {
		t.Fatalf("registry = %v, want [20]", got)
	}
}

func TestTicketReadyAccountIDs(t *testing.T) {
	slot := []uint64{10, 20, 30}
	dispatch := []uint64{10, 20, 40}
	pin := []uint64{10, 20}
	tickets := map[uint64]int64{10: 2, 20: 0, 30: 1}
	got := TicketReadyAccountIDs(slot, dispatch, pin, tickets)
	if len(got) != 1 || got[0] != 10 {
		t.Fatalf("ticket ready = %v, want [10]", got)
	}
}

func TestTicketReadyAccountIDsWithoutTicketFilter(t *testing.T) {
	got := TicketReadyAccountIDs([]uint64{1, 2}, []uint64{1, 2, 3}, []uint64{1}, nil)
	if len(got) != 1 || got[0] != 1 {
		t.Fatalf("ticket ready = %v, want [1]", got)
	}
}
