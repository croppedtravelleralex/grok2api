package relational

import "time"

type imagePipelineTraceModel struct {
	ID          string `gorm:"size:64;primaryKey;check:chk_image_pipeline_traces_id,length(trim(id)) BETWEEN 16 AND 64"`
	RequestID   string `gorm:"size:64;not null;index:idx_image_pipeline_traces_request;check:chk_image_pipeline_traces_request_id,length(request_id) BETWEEN 1 AND 64"`
	Lane        int    `gorm:"not null;check:chk_image_pipeline_traces_lane,lane >= 0 AND lane <= 128"`
	Status      string `gorm:"size:32;not null;check:chk_image_pipeline_traces_status,status IN ('queued','running','succeeded','failed','canceled')"`
	Model       string `gorm:"size:255;not null;default:'';check:chk_image_pipeline_traces_model,length(model) <= 255"`
	AccountID   *uint64
	AccountName string `gorm:"size:160;not null;default:'';check:chk_image_pipeline_traces_account_name,length(account_name) <= 160"`
	ErrorCode   string `gorm:"size:100;not null;default:'';check:chk_image_pipeline_traces_error_code,length(error_code) <= 100"`
	StartedAt   time.Time `gorm:"not null;index:idx_image_pipeline_traces_started"`
	EndedAt     *time.Time
	QueueMS     int64 `gorm:"not null;default:0"`
	ExpandMS    int64 `gorm:"not null;default:0"`
	SSEMS       int64 `gorm:"not null;default:0"`
	DownloadMS  int64 `gorm:"not null;default:0"`
	TotalMS     int64 `gorm:"not null;default:0"`
	SoftStop    bool  `gorm:"not null;default:false"`
}

func (imagePipelineTraceModel) TableName() string { return "image_pipeline_traces" }

type imagePipelineSegmentModel struct {
	ID        uint64 `gorm:"primaryKey;autoIncrement"`
	TraceID   string `gorm:"size:64;not null;index:idx_image_pipeline_segments_trace;check:chk_image_pipeline_segments_trace_id,length(trim(trace_id)) BETWEEN 16 AND 64"`
	Stage     string `gorm:"size:32;not null;check:chk_image_pipeline_segments_stage,stage IN ('queue','expand','sse','download')"`
	Sequence  int    `gorm:"not null;check:chk_image_pipeline_segments_sequence,sequence >= 0 AND sequence <= 1000"`
	StartedAt time.Time `gorm:"not null"`
	EndedAt   *time.Time
	Outcome   string `gorm:"size:64;not null;default:'';check:chk_image_pipeline_segments_outcome,length(outcome) <= 64"`
}

func (imagePipelineSegmentModel) TableName() string { return "image_pipeline_segments" }
