package repository

import (
	"context"
	"time"

	"github.com/chenyme/grok2api/backend/internal/domain/imagepipeline"
)

// ImagePipelineRepository 持久化生图流水线 trace 与阶段片段。
type ImagePipelineRepository interface {
	CreateTrace(ctx context.Context, value imagepipeline.Trace) error
	UpdateTrace(ctx context.Context, value imagepipeline.Trace) error
	AppendSegment(ctx context.Context, value imagepipeline.Segment) (imagepipeline.Segment, error)
	CloseSegment(ctx context.Context, id uint64, endedAt time.Time, outcome string) error
	ListTraces(ctx context.Context, from, to time.Time, limit int) ([]imagepipeline.Trace, error)
	DeleteOlderThan(ctx context.Context, before time.Time) (int64, error)
}
