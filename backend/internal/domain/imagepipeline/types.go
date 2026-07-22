package imagepipeline

import "time"

type Stage string

const (
	StageQueue    Stage = "queue"
	StageExpand   Stage = "expand"
	StageSSE      Stage = "sse"
	StageDownload Stage = "download"
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
	Sequence  int
	StartedAt time.Time
	EndedAt   *time.Time
	Outcome   string
}

type Snapshot struct {
	PipelineSlots  int
	ActiveSlots    int
	QueueDepth     int
	QueueCapacity  int
	ExpandActive   int
	ExpandLimit    int
	SSEActive      int
	SSELimit       int
	SSETarget      int
	DownloadActive int
	DownloadLimit  int
	ExpandQueued   int
	SSEQueued      int
	DownloadQueued int
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
	Slots          []SlotSnapshot
	Queue          []QueueSnapshot
	UpdatedAt      time.Time
}

type SlotSnapshot struct {
	Lane        int
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
	TraceID    string
	RequestID  string
	Model      string
	EnqueuedAt time.Time
	WaitMS     int64
}

type Timeline struct {
	From     time.Time
	To       time.Time
	Snapshot Snapshot
	Lanes    int
	Traces   []Trace
}
