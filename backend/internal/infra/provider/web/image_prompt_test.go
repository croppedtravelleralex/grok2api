package web

import "testing"

func TestShouldExpandImagePrompt(t *testing.T) {
	cases := []struct {
		prompt string
		want   bool
	}{
		{"一台iPhone", true},
		{"a red mug", true},
		{"product photo of one Apple iPhone 16 Pro smartphone standing upright on white background with studio lighting", false},
		{"", false},
	}
	for _, tc := range cases {
		if got := shouldExpandImagePrompt(tc.prompt); got != tc.want {
			t.Fatalf("shouldExpandImagePrompt(%q)=%v want %v", tc.prompt, got, tc.want)
		}
	}
}

func TestSanitizeExpandedImagePrompt(t *testing.T) {
	got := sanitizeExpandedImagePrompt("\"A black iPhone on white table\"\n\nNote: done")
	if got != "A black iPhone on white table" {
		t.Fatalf("sanitize = %q", got)
	}
}
