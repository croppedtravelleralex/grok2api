package egress

import "time"

// TrafficHop 记录单次 egress HTTP hop 的字节计量。
type TrafficHop struct {
	ID            uint64
	RequestID     string
	EgressNodeID  uint64
	EgressScope   Scope
	Provider      string
	Operation     string
	PipelineStage string
	AccountID     uint64
	RequestBytes  int64
	ResponseBytes int64
	Transport     string
	CreatedAt     time.Time
}
