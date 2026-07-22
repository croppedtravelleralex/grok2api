package relational

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	domain "github.com/chenyme/grok2api/backend/internal/domain/imagepipeline"
)

func TestImagePipelineRepositoryUpdatesCompletedTrace(t *testing.T) {
	ctx := context.Background()
	database, err := OpenSQLite(ctx, filepath.Join(t.TempDir(), "image-pipeline.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = database.Close() })
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}

	repository := NewImagePipelineRepository(database)
	startedAt := time.Now().UTC().Add(-time.Second)
	trace := domain.Trace{
		ID: "ipt_0123456789abcdef", RequestID: "request-1", Lane: 0,
		Status: domain.StatusRunning, Model: "grok-imagine-image", StartedAt: startedAt,
	}
	if err := repository.CreateTrace(ctx, trace); err != nil {
		t.Fatal(err)
	}

	endedAt := time.Now().UTC()
	trace.Status = domain.StatusSucceeded
	trace.EndedAt = &endedAt
	trace.QueueMS = 12
	trace.ExpandMS = 34
	trace.SSEMS = 56
	trace.DownloadMS = 78
	trace.TotalMS = 180
	if err := repository.UpdateTrace(ctx, trace); err != nil {
		t.Fatalf("完成 trace 更新失败: %v", err)
	}

	traces, err := repository.ListTraces(ctx, startedAt.Add(-time.Second), endedAt.Add(time.Second), 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(traces) != 1 {
		t.Fatalf("trace 数量 = %d", len(traces))
	}
	got := traces[0]
	if got.Status != domain.StatusSucceeded || got.EndedAt == nil || got.SSEMS != 56 || got.TotalMS != 180 {
		t.Fatalf("完成 trace 未正确落库: %+v", got)
	}
}
