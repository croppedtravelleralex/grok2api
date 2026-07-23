package account

import (
	"encoding/json"
	"fmt"
	"net/http"
	"testing"

	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
)

func TestProbeWebLiteL2PayloadUsesMessages(t *testing.T) {
	payload := []byte(`{"model":"grok-imagine-image","messages":[{"role":"user","content":"minimal probe"}],"image_config":{"n":1},"stream":false}`)
	var body map[string]any
	if err := json.Unmarshal(payload, &body); err != nil {
		t.Fatal(err)
	}
	if _, ok := body["input"]; ok {
		t.Fatal("lite probe payload must not use input field")
	}
	messages, ok := body["messages"].([]any)
	if !ok || len(messages) != 1 {
		t.Fatalf("messages = %#v", body["messages"])
	}
	imageConfig, ok := body["image_config"].(map[string]any)
	if !ok || imageConfig["n"] != float64(1) {
		t.Fatalf("image_config = %#v", body["image_config"])
	}
}

func TestImagineNeedsL2Probe(t *testing.T) {
	tests := []struct {
		name  string
		input WebPoolContext
		want  bool
	}{
		{name: "nil state", input: WebPoolContext{}, want: true},
		{name: "unknown", input: WebPoolContext{ModelState: modelStatePtr("unknown")}, want: true},
		{name: "quota available", input: WebPoolContext{ModelState: modelStatePtr("quota_available")}, want: true},
		{name: "available", input: WebPoolContext{ModelState: modelStatePtr("available")}, want: false},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := imagineNeedsL2Probe(test.input); got != test.want {
				t.Fatalf("imagineNeedsL2Probe() = %v, want %v", got, test.want)
			}
		})
	}
}

func modelStatePtr(status string) *accountdomain.ModelState {
	value := accountdomain.ModelState{Status: accountdomain.ModelStatus(status)}
	return &value
}

func TestModelStatusFromProbeError(t *testing.T) {
	tests := []struct {
		err  error
		want accountdomain.ModelStatus
	}{
		{err: fmt.Errorf("status %d: quota", http.StatusTooManyRequests), want: accountdomain.ModelStatusQuotaExhausted},
		{err: fmt.Errorf("status %d: denied", http.StatusForbidden), want: accountdomain.ModelStatusAuthFailed},
		{err: fmt.Errorf("upstream credential unauthorized"), want: accountdomain.ModelStatusAuthFailed},
		{err: fmt.Errorf("status %d: bad request", http.StatusBadRequest), want: accountdomain.ModelStatusSignatureFailed},
	}
	for _, test := range tests {
		if got := modelStatusFromProbeError(test.err); got != test.want {
			t.Fatalf("modelStatusFromProbeError(%v) = %q, want %q", test.err, got, test.want)
		}
	}
}
