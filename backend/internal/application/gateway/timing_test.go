package gateway

import (
	"bytes"
	"io"
	"log/slog"
	"strings"
	"testing"
	"time"

	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
)

func TestGenerationTimingLogsOnlyPhaseMetadata(t *testing.T) {
	var output bytes.Buffer
	logger := slog.New(slog.NewTextHandler(&output, &slog.HandlerOptions{Level: slog.LevelDebug}))
	timing := newGenerationTiming("public-model", accountdomain.ProviderBuild)
	timing.markSelection(10 * time.Millisecond)
	timing.markCredential(20 * time.Millisecond)
	timing.markUpstream(30 * time.Millisecond)
	timing.markUpstream(40 * time.Millisecond)
	timing.markImagePipeline(11*time.Millisecond, 22*time.Millisecond, 33*time.Millisecond, 44*time.Millisecond)
	body := &firstByteReadCloser{ReadCloser: io.NopCloser(strings.NewReader("ok")), mark: timing.markFirstBody}
	if _, err := io.ReadAll(body); err != nil {
		t.Fatal(err)
	}
	timing.finish(logger, "success")
	logged := output.String()
	for _, expected := range []string{"generation_timing", "route=public-model", "provider=grok_build", "selection_wait_ms=10", "credential_wait_ms=20", "upstream_wait_ms=70", "pipeline_queue_ms=11", "pipeline_expand_ms=22", "pipeline_sse_ms=33", "pipeline_download_ms=44", "attempts=2", "retries=1"} {
		if !strings.Contains(logged, expected) {
			t.Fatalf("log missing %q: %s", expected, logged)
		}
	}
}
