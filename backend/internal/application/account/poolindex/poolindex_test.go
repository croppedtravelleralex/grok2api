package poolindex

import (
	"testing"
	"time"
)

func TestDispatchIndexOrdersByPriorityQuotaAndFairness(t *testing.T) {
	idx := NewDispatchIndex()
	now := time.Unix(1000, 0).UTC()
	idx.Upsert(DispatchEntry{ID: 1, Priority: 10, QuotaKnown: true, QuotaRemaining: 5, LastSelectedAt: now})
	idx.Upsert(DispatchEntry{ID: 2, Priority: 20, QuotaKnown: true, QuotaRemaining: 9, LastSelectedAt: now})
	idx.Upsert(DispatchEntry{ID: 3, Priority: 20, QuotaKnown: true, QuotaRemaining: 9, LastSelectedAt: now.Add(-time.Hour)})
	got := idx.Ascend(10)
	if len(got) != 3 || got[0].ID != 3 || got[1].ID != 2 || got[2].ID != 1 {
		t.Fatalf("unexpected order: %#v", got)
	}
	idx.TouchSelected(3, now.Add(time.Hour))
	got = idx.Ascend(1)
	if got[0].ID != 2 {
		t.Fatalf("after touch expected id=2, got %#v", got)
	}
	idx.Remove(2)
	if idx.Contains(2) || idx.Len() != 2 {
		t.Fatalf("remove failed len=%d", idx.Len())
	}
}

func TestDueHeapPeekAndPopRespectDueAt(t *testing.T) {
	h := NewDueHeap()
	now := time.Unix(2000, 0).UTC()
	h.Upsert(1, now.Add(time.Minute))
	h.Upsert(2, now.Add(-time.Second))
	if id, ok := h.PeekDue(now); !ok || id != 2 {
		t.Fatalf("peek due got %d %v", id, ok)
	}
	if id, ok := h.PopDue(now); !ok || id != 2 {
		t.Fatalf("pop due got %d %v", id, ok)
	}
	if _, ok := h.PopDue(now); ok {
		t.Fatal("future item should not pop")
	}
	id, ok := h.PopAny()
	if !ok || id != 1 {
		t.Fatalf("pop any expected 1 got %d %v", id, ok)
	}
}

func TestDRRApproximatesWeights(t *testing.T) {
	d := NewDRRScheduler()
	counts := [3]int{}
	has := [3]bool{true, true, true}
	for i := 0; i < 1000; i++ {
		lane, ok := d.Next(has)
		if !ok {
			t.Fatal("expected lane")
		}
		counts[lane]++
	}
	// 5:3:2 over 1000 ≈ 500:300:200
	if counts[0] < 450 || counts[0] > 550 || counts[1] < 250 || counts[1] > 350 || counts[2] < 150 || counts[2] > 250 {
		t.Fatalf("unexpected drr distribution %#v", counts)
	}
	d2 := NewDRRScheduler()
	counts = [3]int{}
	has = [3]bool{false, true, true}
	for i := 0; i < 1000; i++ {
		lane, ok := d2.Next(has)
		if !ok {
			t.Fatal("expected lane")
		}
		counts[lane]++
	}
	if counts[0] != 0 || counts[1] < 650 || counts[1] > 750 || counts[2] < 250 || counts[2] > 350 {
		t.Fatalf("unexpected fallback distribution %#v", counts)
	}
}
