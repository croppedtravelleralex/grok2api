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
		"lanes":    gin.H{"ps": timeline.Lanes.PS, "ss": timeline.Lanes.SS},
		"snapshot": snapshotDTO(timeline.Snapshot),
		"traces":   tracesDTO(timeline.Traces),
	})
}

func snapshotDTO(value domain.Snapshot) gin.H {
	return gin.H{
		"promptSlots": value.PromptSlots, "promptActive": value.PromptActive, "promptQueued": value.PromptQueued,
		"sseSlots": value.SSESlots, "sseActive": value.SSEActive, "sseQueued": value.SSEQueued,
		"uploadActive": value.UploadActive, "uploadLimit": value.UploadLimit, "uploadQueued": value.UploadQueued,
		"downloadActive": value.DownloadActive, "downloadLimit": value.DownloadLimit, "downloadQueued": value.DownloadQueued,
		"inFlight": value.InFlight, "queueCapacity": value.QueueCapacity,
		"psSlots": slotsDTO(value.PSSlots), "ssSlots": slotsDTO(value.SSSlots),
		"pipelineSlots": value.PipelineSlots, "activeSlots": value.ActiveSlots,
		"queueDepth": value.QueueDepth,
		"expandActive": value.ExpandActive, "expandLimit": value.ExpandLimit,
		"sseLimit": value.SSELimit, "sseTarget": value.SSETarget,
		"expandQueued": value.ExpandQueued,
		"oldestQueueMs": value.OldestQueueMS, "slots": slotsDTO(value.Slots), "queue": queueDTO(value.Queue),
		"successRate": value.SuccessRate, "sampleCount": value.SampleCount,
		"p50TotalMs": value.P50TotalMS, "p90TotalMs": value.P90TotalMS, "p95TotalMs": value.P95TotalMS,
		"p50ExpandMs": value.P50ExpandMS, "p90ExpandMs": value.P90ExpandMS,
		"p50SseMs": value.P50SSEMS, "p90SseMs": value.P90SSEMS,
		"p50DownloadMs": value.P50DownloadMS, "p90DownloadMs": value.P90DownloadMS,
		"updatedAt": value.UpdatedAt.Format(time.RFC3339Nano),
	}
}

func slotsDTO(values []domain.SlotSnapshot) []gin.H {
	items := make([]gin.H, 0, len(values))
	for _, slot := range values {
		item := gin.H{"lane": slot.Lane, "pool": slot.Pool, "occupied": slot.Occupied}
		if slot.Occupied {
			item["traceId"] = slot.TraceID
			item["requestId"] = slot.RequestID
			item["model"] = slot.Model
			item["accountName"] = slot.AccountName
			item["stage"] = string(slot.Stage)
			item["waitingFor"] = string(slot.WaitingFor)
			item["status"] = string(slot.Status)
			item["startedAt"] = slot.StartedAt.Format(time.RFC3339Nano)
			item["activeMs"] = slot.ActiveMS
		}
		items = append(items, item)
	}
	return items
}

func queueDTO(values []domain.QueueSnapshot) []gin.H {
	items := make([]gin.H, 0, len(values))
	for _, queued := range values {
		items = append(items, gin.H{
			"position": queued.Position, "pool": queued.Pool, "traceId": queued.TraceID, "requestId": queued.RequestID,
			"model": queued.Model, "enqueuedAt": queued.EnqueuedAt.Format(time.RFC3339Nano), "waitMs": queued.WaitMS,
		})
	}
	return items
}

func tracesDTO(values []domain.Trace) []gin.H {
	items := make([]gin.H, 0, len(values))
	for _, trace := range values {
		item := gin.H{
			"id": trace.ID, "requestId": trace.RequestID, "lane": trace.Lane, "status": string(trace.Status),
			"model": trace.Model, "accountName": trace.AccountName, "errorCode": trace.ErrorCode,
			"startedAt": trace.StartedAt.Format(time.RFC3339Nano),
			"queueMs": trace.QueueMS, "uploadQueueMs": trace.UploadQueueMS, "psQueueMs": trace.PSQueueMS,
			"ssQueueMs": trace.SSQueueMS, "downloadQueueMs": trace.DownloadQueueMS,
			"expandMs": trace.ExpandMS, "sseMs": trace.SSEMS,
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
			"id": segment.ID, "stage": string(segment.Stage), "slot": segment.Slot, "sequence": segment.Sequence,
			"startedAt": segment.StartedAt.Format(time.RFC3339Nano), "outcome": segment.Outcome,
		}
		if segment.EndedAt != nil {
			item["endedAt"] = segment.EndedAt.Format(time.RFC3339Nano)
		}
		items = append(items, item)
	}
	return items
}
