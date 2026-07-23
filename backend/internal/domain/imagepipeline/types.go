package imagepipeline

import "time"

type Stage string

const (
	StageQueue         Stage = "queue"
	StageQueueUpload   Stage = "queue_upload"
	StageUpload        Stage = "upload"
	StageQueuePS       Stage = "queue_ps"
	StagePS            Stage = "ps"
	StageExpand        Stage = "expand" // legacy read alias for ps
	StageQueueSS       Stage = "queue_ss"
	StageSSE           Stage = "sse"
	StageQueueDownload Stage = "queue_download"
	StageDownload      Stage = "download"
)

func NormalizeStage(stage Stage) Stage {
	if stage == StageExpand {
		return StagePS
	}
	return stage
}

type PhaseCursor string

const (
	PhaseAdmitted      PhaseCursor = "admitted"
	PhaseUploadDone    PhaseCursor = "upload_done"
	PhasePSDone        PhaseCursor = "ps_done"
	PhaseSSDone        PhaseCursor = "ss_done"
	PhaseDownloadDone  PhaseCursor = "download_done"
)

type Status string

const (
	StatusQueued    Status = "queued"
	StatusRunning   Status = "running"
	StatusSucceeded Status = "succeeded"
	StatusFailed    Status = "failed"
	StatusCanceled  Status = "canceled"
)

type Trace struct {
	ID          string
	RequestID   string
	Lane        int
	Status      Status
	Model       string
	AccountID   *uint64
	AccountName string
	ErrorCode   string
	StartedAt   time.Time
	EndedAt     *time.Time
	QueueMS     int64
	UploadQueueMS   int64
	PSQueueMS       int64
	SSQueueMS       int64
	DownloadQueueMS int64
	ExpandMS    int64
	SSEMS       int64
	DownloadMS  int64
	TotalMS     int64
	SoftStop    bool
	Segments    []Segment
}

type Segment struct {
	ID        uint64
	TraceID   string
	Stage     Stage
	Slot      int
	Sequence  int
	StartedAt time.Time
	EndedAt   *time.Time
	Outcome   string
}

type Snapshot struct {
	PromptSlots    int
	PromptActive   int
	PromptQueued   int
	SSESlots       int
	SSEActive      int
	SSEQueued      int
	UploadActive   int
	UploadLimit    int
	UploadQueued   int
	DownloadActive int
	DownloadLimit  int
	DownloadQueued int
	InFlight       int
	QueueCapacity  int
	PSSlots        []SlotSnapshot
	SSSlots        []SlotSnapshot
	SuccessRate    float64
	SampleCount    int
	P50TotalMS     int64
	P90TotalMS     int64
	P95TotalMS     int64
	P50ExpandMS    int64
	P90ExpandMS    int64
	P50SSEMS       int64
	P90SSEMS       int64
	P50DownloadMS  int64
	P90DownloadMS  int64
	OldestQueueMS  int64
	UpdatedAt      time.Time

	// Legacy fields for gradual API migration.
	PipelineSlots  int
	ActiveSlots    int
	QueueDepth     int
	ExpandActive   int
	ExpandLimit    int
	SSELimit       int
	SSETarget      int
	ExpandQueued   int
	Slots          []SlotSnapshot
	Queue          []QueueSnapshot
}

type SlotSnapshot struct {
	Lane        int
	Pool        string
	Occupied    bool
	TraceID     string
	RequestID   string
	Model       string
	AccountName string
	Stage       Stage
	WaitingFor  Stage
	Status      Status
	StartedAt   time.Time
	ActiveMS    int64
}

type QueueSnapshot struct {
	Position   int
	Pool       string
	TraceID    string
	RequestID  string
	Model      string
	EnqueuedAt time.Time
	WaitMS     int64
}

type LaneLayout struct {
	PS int
	SS int
}

type Timeline struct {
	From     time.Time
	To       time.Time
	Snapshot Snapshot
	Lanes    LaneLayout
	Traces   []Trace
}
