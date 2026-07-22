package poolindex

import "testing"

func TestWebDRRSchedulerRatio(t *testing.T) {
	scheduler := NewWebDRRScheduler()
	counts := map[WebMaintenanceLane]int{}
	hasWork := [3]bool{true, true, true}
	for i := 0; i < 100; i++ {
		lane, ok := scheduler.Next(hasWork)
		if !ok {
			t.Fatal("expected lane")
		}
		counts[lane]++
	}
	if counts[WebLaneRecoveryVerify] < 40 || counts[WebLaneRecoveryVerify] > 60 {
		t.Fatalf("verify ratio unexpected: %#v", counts)
	}
}
