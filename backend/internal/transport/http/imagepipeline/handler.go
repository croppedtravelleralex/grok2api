package imagepipeline

import (
	"net/http"
	"time"

	imagepipelineapp "github.com/chenyme/grok2api/backend/internal/application/imagepipeline"
	domain "github.com/chenyme/grok2api/backend/internal/domain/imagepipeline"
	"github.com/chenyme/grok2api/backend/internal/shared/response"
	"github.com/gin-gonic/gin"
)

type Handler struct {
	scheduler *imagepipelineapp.Scheduler
}

func NewHandler(scheduler *imagepipelineapp.Scheduler) *Handler {
	return &Handler{scheduler: scheduler}
}

func (h *Handler) Register(router gin.IRoutes) {
	router.GET("/image-timeline", h.timeline)
}

func (h *Handler) timeline(c *gin.Context) {
	if h.scheduler == nil {
		response.Error(c, http.StatusServiceUnavailable, "imagePipelineUnavailable", "生图流水线未启用")
		return
	}
	now := time.Now().UTC()
	to := now
	from := now.Add(-30 * time.Minute)
	if raw := c.Query("to"); raw != "" {
		parsed, err := time.Parse(time.RFC3339, raw)
		if err != nil {
			response.Error(c, http.StatusBadRequest, "invalidTimeRange", "to 必须是 RFC3339 时间")
			return
		}
		to = parsed.UTC()
	}
	if raw := c.Query("from"); raw != "" {
		parsed, err := time.Parse(time.RFC3339, raw)
		if err != nil {
			response.Error(c, http.StatusBadRequest, "invalidTimeRange", "from 必须是 RFC3339 时间")
			return
		}
		from = parsed.UTC()
	}
	if window := c.Query("window"); window != "" {
		switch window {
		case "30m":
			from = to.Add(-30 * time.Minute)
		case "1h":
			from = to.Add(-time.Hour)
		case "6h":
			from = to.Add(-6 * time.Hour)
		case "12h":
			from = to.Add(-12 * time.Hour)
		default:
			response.Error(c, http.StatusBadRequest, "invalidWindow", "window 必须是 30m、1h、6h 或 12h")
			return
		}
	}
	if !from.Before(to) {
		response.Error(c, http.StatusBadRequest, "invalidTimeRange", "from 必须早于 to")
		return
	}
	if to.Sub(from) > 12*time.Hour {
		response.Error(c, http.StatusBadRequest, "invalidTimeRange", "时间范围不能超过 12 小时")
		return
	}
	timeline, err := h.scheduler.Timeline(c.Request.Context(), from, to)
	if err != nil {
		response.Error(c, http.StatusInternalServerError, "imageTimelineFailed", "读取生图时序图失败")
		return
	}
	response.Success(c, http.StatusOK, gin.H{
		"from":     timeline.From.Format(time.RFC3339Nano),
		"to":       timeline.To.Format(time.RFC3339Nano),
		"lanes":    timeline.Lanes,
		"snapshot": snapshotDTO(timeline.Snapshot),
		"traces":   tracesDTO(timeline.Traces),
	})
}

func snapshotDTO(value domain.Snapshot) gin.H {
	return gin.H{
		"pipelineSlots": value.PipelineSlots, "activeSlots": value.ActiveSlots,
		"queueDepth": value.QueueDepth, "queueCapacity": value.QueueCapacity,
		"expandActive": value.ExpandActive, "expandLimit": value.ExpandLimit,
		"sseActive": value.SSEActive, "sseLimit": value.SSELimit, "sseTarget": value.SSETarget,
		"downloadActive": value.DownloadActive, "downloadLimit": value.DownloadLimit,
		"successRate": value.SuccessRate, "sampleCount": value.SampleCount,
		"p50TotalMs": value.P50TotalMS, "p90TotalMs": value.P90TotalMS, "p95TotalMs": value.P95TotalMS,
		"p50ExpandMs": value.P50ExpandMS, "p90ExpandMs": value.P90ExpandMS,
		"p50SseMs": value.P50SSEMS, "p90SseMs": value.P90SSEMS,
		"p50DownloadMs": value.P50DownloadMS, "p90DownloadMs": value.P90DownloadMS,
		"updatedAt": value.UpdatedAt.Format(time.RFC3339Nano),
	}
}

func tracesDTO(values []domain.Trace) []gin.H {
	items := make([]gin.H, 0, len(values))
	for _, trace := range values {
		item := gin.H{
			"id": trace.ID, "requestId": trace.RequestID, "lane": trace.Lane, "status": string(trace.Status),
			"model": trace.Model, "accountName": trace.AccountName, "errorCode": trace.ErrorCode,
			"startedAt": trace.StartedAt.Format(time.RFC3339Nano),
			"queueMs":   trace.QueueMS, "expandMs": trace.ExpandMS, "sseMs": trace.SSEMS,
			"downloadMs": trace.DownloadMS, "totalMs": trace.TotalMS, "softStop": trace.SoftStop,
			"segments": segmentsDTO(trace.Segments),
		}
		if trace.AccountID != nil {
			item["accountId"] = *trace.AccountID
		}
		if trace.EndedAt != nil {
			item["endedAt"] = trace.EndedAt.Format(time.RFC3339Nano)
		}
		items = append(items, item)
	}
	return items
}

func segmentsDTO(values []domain.Segment) []gin.H {
	items := make([]gin.H, 0, len(values))
	for _, segment := range values {
		item := gin.H{
			"id": segment.ID, "stage": string(segment.Stage), "sequence": segment.Sequence,
			"startedAt": segment.StartedAt.Format(time.RFC3339Nano), "outcome": segment.Outcome,
		}
		if segment.EndedAt != nil {
			item["endedAt"] = segment.EndedAt.Format(time.RFC3339Nano)
		}
		items = append(items, item)
	}
	return items
}
