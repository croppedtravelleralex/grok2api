package account

import (
	"testing"
	"time"
)

func TestImagineDispatchQuotaAdmissible(t *testing.T) {
	now := time.Now().UTC()
	synced := now.Add(-5 * time.Minute)
	fresh := func(total, remaining int) *QuotaWindow {
		return &QuotaWindow{
			Mode: "imagine", Total: total, Remaining: remaining,
			SyncedAt: &synced, Source: QuotaSourceUpstream, UpdatedAt: synced,
		}
	}
	success := now.Add(-10 * time.Minute)
	tests := []struct {
		name   string
		window *QuotaWindow
		state  *ModelState
		want   bool
	}{
		{
			name:   "known positive",
			window: fresh(10, 3),
			state:  &ModelState{Status: ModelStatusAvailable, LastSuccessAt: &success},
			want:   true,
		},
		{
			name:   "unknown zero with recent lite success",
			window: fresh(0, 0),
			state:  &ModelState{Status: ModelStatusAvailable, LastSuccessAt: &success},
			want:   true,
		},
		{
			name:   "unknown zero without success evidence",
			window: fresh(0, 0),
			state:  &ModelState{Status: ModelStatusAvailable},
			want:   true,
		},
		{
			name:   "unknown zero available trusts model state",
			window: fresh(0, 0),
			state:  &ModelState{Status: ModelStatusAvailable, LastSuccessAt: func() *time.Time { t := now.Add(-10 * time.Minute); return &t }()},
			want:   true,
		},
		{
			name:   "unknown zero quota available awaits probe",
			window: fresh(0, 0),
			state:  &ModelState{Status: ModelStatusQuotaAvailable},
			want:   true,
		},
		{
			name:   "known exhausted",
			window: fresh(10, 0),
			state:  &ModelState{Status: ModelStatusAvailable, LastSuccessAt: &success},
			want:   false,
		},
		{
			name:   "quota exhausted state blocked",
			window: fresh(0, 0),
			state:  &ModelState{Status: ModelStatusQuotaExhausted},
			want:   false,
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := ImagineDispatchQuotaAdmissible(test.window, test.state, now); got != test.want {
				t.Fatalf("ImagineDispatchQuotaAdmissible() = %v, want %v", got, test.want)
			}
		})
	}
}
