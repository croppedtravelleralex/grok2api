package relational

import (
	"context"
	"fmt"
	"time"

	"github.com/chenyme/grok2api/backend/internal/domain/imagepipeline"
	"github.com/chenyme/grok2api/backend/internal/repository"
)

type ImagePipelineRepository struct {
	db *Database
}

func NewImagePipelineRepository(db *Database) *ImagePipelineRepository {
	return &ImagePipelineRepository{db: db}
}

var _ repository.ImagePipelineRepository = (*ImagePipelineRepository)(nil)

func (r *ImagePipelineRepository) CreateTrace(ctx context.Context, value imagepipeline.Trace) error {
	model := toTraceModel(value)
	if err := r.db.db.WithContext(ctx).Create(&model).Error; err != nil {
		return fmt.Errorf("创建生图流水线 trace: %w", err)
	}
	return nil
}

func (r *ImagePipelineRepository) UpdateTrace(ctx context.Context, value imagepipeline.Trace) error {
	model := toTraceModel(value)
	result := r.db.db.WithContext(ctx).Model(&imagePipelineTraceModel{}).Where("id = ?", model.ID).Updates(map[string]any{
		"lane": model.Lane, "status": model.Status, "model": model.Model, "account_id": model.AccountID,
		"account_name": model.AccountName, "error_code": model.ErrorCode, "ended_at": model.EndedAt,
		"queue_ms": model.QueueMS, "upload_queue_ms": model.UploadQueueMS, "ps_queue_ms": model.PSQueueMS,
		"ss_queue_ms": model.SSQueueMS, "download_queue_ms": model.DownloadQueueMS,
		"expand_ms": model.ExpandMS, "ssems": model.SSEMS,
		"download_ms": model.DownloadMS, "total_ms": model.TotalMS, "soft_stop": model.SoftStop,
	})
	if result.Error != nil {
		return fmt.Errorf("更新生图流水线 trace: %w", result.Error)
	}
	if result.RowsAffected == 0 {
		return repository.ErrNotFound
	}
	return nil
}

func (r *ImagePipelineRepository) AppendSegment(ctx context.Context, value imagepipeline.Segment) (imagepipeline.Segment, error) {
	model := imagePipelineSegmentModel{
		TraceID: value.TraceID, Stage: string(value.Stage), Slot: value.Slot, Sequence: value.Sequence,
		StartedAt: value.StartedAt.UTC(), EndedAt: value.EndedAt, Outcome: value.Outcome,
	}
	if err := r.db.db.WithContext(ctx).Create(&model).Error; err != nil {
		return imagepipeline.Segment{}, fmt.Errorf("追加生图流水线段: %w", err)
	}
	value.ID = model.ID
	return value, nil
}

func (r *ImagePipelineRepository) CloseSegment(ctx context.Context, id uint64, endedAt time.Time, outcome string) error {
	result := r.db.db.WithContext(ctx).Model(&imagePipelineSegmentModel{}).Where("id = ?", id).Updates(map[string]any{
		"ended_at": endedAt.UTC(), "outcome": outcome,
	})
	if result.Error != nil {
		return fmt.Errorf("关闭生图流水线段: %w", result.Error)
	}
	if result.RowsAffected == 0 {
		return repository.ErrNotFound
	}
	return nil
}

func (r *ImagePipelineRepository) ListTraces(ctx context.Context, from, to time.Time, limit int) ([]imagepipeline.Trace, error) {
	if limit <= 0 || limit > 2000 {
		limit = 500
	}
	var models []imagePipelineTraceModel
	if err := r.db.db.WithContext(ctx).Where("started_at < ? AND (ended_at IS NULL OR ended_at >= ?)", to.UTC(), from.UTC()).
		Order("started_at ASC").Limit(limit).Find(&models).Error; err != nil {
		return nil, fmt.Errorf("列出生图流水线 trace: %w", err)
	}
	if len(models) == 0 {
		return nil, nil
	}
	ids := make([]string, 0, len(models))
	for _, model := range models {
		ids = append(ids, model.ID)
	}
	var segments []imagePipelineSegmentModel
	if err := r.db.db.WithContext(ctx).Where("trace_id IN ?", ids).Order("trace_id ASC, sequence ASC, id ASC").Find(&segments).Error; err != nil {
		return nil, fmt.Errorf("列出生图流水线段: %w", err)
	}
	byTrace := make(map[string][]imagepipeline.Segment, len(ids))
	for _, segment := range segments {
		byTrace[segment.TraceID] = append(byTrace[segment.TraceID], fromSegmentModel(segment))
	}
	result := make([]imagepipeline.Trace, 0, len(models))
	for _, model := range models {
		trace := fromTraceModel(model)
		trace.Segments = byTrace[model.ID]
		result = append(result, trace)
	}
	return result, nil
}

func (r *ImagePipelineRepository) CloseStaleRunning(ctx context.Context, olderThan time.Time) (int64, error) {
	now := time.Now().UTC()
	result := r.db.db.WithContext(ctx).Model(&imagePipelineTraceModel{}).
		Where("status = ? AND ended_at IS NULL AND started_at < ?", string(imagepipeline.StatusRunning), olderThan.UTC()).
		Updates(map[string]any{
			"status":     string(imagepipeline.StatusFailed),
			"error_code": "stale_abandoned",
			"ended_at":   now,
		})
	if result.Error != nil {
		return 0, fmt.Errorf("关闭陈旧 running trace: %w", result.Error)
	}
	if result.RowsAffected == 0 {
		return 0, nil
	}
	// SQLite / Postgres 均支持 julianday 差异近似；仅用于陈旧 trace 收尾。
	if err := r.db.db.WithContext(ctx).Exec(
		`UPDATE image_pipeline_traces SET total_ms = CAST((julianday(ended_at) - julianday(started_at)) * 86400000 AS INTEGER) WHERE status = ? AND error_code = ? AND ended_at = ?`,
		string(imagepipeline.StatusFailed), "stale_abandoned", now,
	).Error; err != nil {
		return result.RowsAffected, fmt.Errorf("更新陈旧 trace total_ms: %w", err)
	}
	return result.RowsAffected, nil
}

func (r *ImagePipelineRepository) DeleteOlderThan(ctx context.Context, before time.Time) (int64, error) {
	tx := r.db.db.WithContext(ctx).Begin()
	if tx.Error != nil {
		return 0, tx.Error
	}
	var ids []string
	if err := tx.Model(&imagePipelineTraceModel{}).Where("started_at < ? AND ended_at IS NOT NULL", before.UTC()).Limit(5000).Pluck("id", &ids).Error; err != nil {
		_ = tx.Rollback()
		return 0, fmt.Errorf("查找过期生图流水线: %w", err)
	}
	if len(ids) == 0 {
		_ = tx.Rollback()
		return 0, nil
	}
	if err := tx.Where("trace_id IN ?", ids).Delete(&imagePipelineSegmentModel{}).Error; err != nil {
		_ = tx.Rollback()
		return 0, fmt.Errorf("删除过期生图流水线段: %w", err)
	}
	result := tx.Where("id IN ?", ids).Delete(&imagePipelineTraceModel{})
	if result.Error != nil {
		_ = tx.Rollback()
		return 0, fmt.Errorf("删除过期生图流水线: %w", result.Error)
	}
	if err := tx.Commit().Error; err != nil {
		return 0, err
	}
	return result.RowsAffected, nil
}

func toTraceModel(value imagepipeline.Trace) imagePipelineTraceModel {
	return imagePipelineTraceModel{
		ID: value.ID, RequestID: value.RequestID, Lane: value.Lane, Status: string(value.Status),
		Model: value.Model, AccountID: value.AccountID, AccountName: value.AccountName, ErrorCode: value.ErrorCode,
		StartedAt: value.StartedAt.UTC(), EndedAt: value.EndedAt,
		QueueMS: value.QueueMS, UploadQueueMS: value.UploadQueueMS, PSQueueMS: value.PSQueueMS,
		SSQueueMS: value.SSQueueMS, DownloadQueueMS: value.DownloadQueueMS,
		ExpandMS: value.ExpandMS, SSEMS: value.SSEMS, DownloadMS: value.DownloadMS, TotalMS: value.TotalMS, SoftStop: value.SoftStop,
	}
}

func fromTraceModel(model imagePipelineTraceModel) imagepipeline.Trace {
	return imagepipeline.Trace{
		ID: model.ID, RequestID: model.RequestID, Lane: model.Lane, Status: imagepipeline.Status(model.Status),
		Model: model.Model, AccountID: model.AccountID, AccountName: model.AccountName, ErrorCode: model.ErrorCode,
		StartedAt: model.StartedAt, EndedAt: model.EndedAt,
		QueueMS: model.QueueMS, UploadQueueMS: model.UploadQueueMS, PSQueueMS: model.PSQueueMS,
		SSQueueMS: model.SSQueueMS, DownloadQueueMS: model.DownloadQueueMS,
		ExpandMS: model.ExpandMS, SSEMS: model.SSEMS, DownloadMS: model.DownloadMS, TotalMS: model.TotalMS, SoftStop: model.SoftStop,
	}
}

func fromSegmentModel(model imagePipelineSegmentModel) imagepipeline.Segment {
	return imagepipeline.Segment{
		ID: model.ID, TraceID: model.TraceID, Stage: imagepipeline.Stage(model.Stage), Slot: model.Slot, Sequence: model.Sequence,
		StartedAt: model.StartedAt, EndedAt: model.EndedAt, Outcome: model.Outcome,
	}
}
