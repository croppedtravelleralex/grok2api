package account

import "testing"

func TestImagineGenerationsSmallValues(t *testing.T) {
	got, known := ImagineGenerations(7, 12)
	if !known || got != 7 {
		t.Fatalf("ImagineGenerations() = (%d, %v), want (7, true)", got, known)
	}
	total, totalKnown := ImagineGenerationsTotal(12)
	if !totalKnown || total != 12 {
		t.Fatalf("ImagineGenerationsTotal() = (%d, %v), want (12, true)", total, totalKnown)
	}
}

func TestImagineGenerationsMicroCredits(t *testing.T) {
	got, known := ImagineGenerations(3_450_000_000, 3_850_000_000)
	if !known || got != 9 {
		t.Fatalf("ImagineGenerations() = (%d, %v), want (9, true)", got, known)
	}
	total, totalKnown := ImagineGenerationsTotal(3_850_000_000)
	if !totalKnown || total != 10 {
		t.Fatalf("ImagineGenerationsTotal() = (%d, %v), want (10, true)", total, totalKnown)
	}
}
