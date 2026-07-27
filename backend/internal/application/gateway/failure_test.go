package gateway

import (
	"net/http"
	"testing"
)

func TestClassifyImagine429(t *testing.T) {
	heavy := []byte(`{"error":{"code":8,"message":"Grok is under heavy usage right now"}}`)
	policy := classifyImagine429(heavy, newHTTPUpstreamFailure(http.StatusTooManyRequests, heavy, 1, "a"))
	if policy.QuotaExhausted || !policy.TransientHeavy {
		t.Fatalf("heavy usage policy = %+v, want transient heavy", policy)
	}

	exhausted := []byte(`{"error":{"code":"usage_limit_reached","message":"usage limit"}}`)
	policy = classifyImagine429(exhausted, newHTTPUpstreamFailure(http.StatusTooManyRequests, exhausted, 1, "a"))
	if !policy.QuotaExhausted || policy.TransientHeavy {
		t.Fatalf("usage limit policy = %+v, want quota exhausted", policy)
	}
}
