package relational

import "time"

type egressTrafficHopModel struct {
	ID            uint64    `gorm:"primaryKey;autoIncrement"`
	RequestID     string    `gorm:"size:128;not null;index:idx_egress_traffic_request"`
	EgressNodeID  uint64    `gorm:"not null;index:idx_egress_traffic_node_created"`
	EgressScope   string    `gorm:"size:32;not null;index:idx_egress_traffic_scope_created"`
	Provider      string    `gorm:"size:32;not null;default:''"`
	Operation     string    `gorm:"size:64;not null;default:''"`
	PipelineStage string    `gorm:"size:32;not null;default:''"`
	AccountID     uint64    `gorm:"not null;default:0"`
	RequestBytes  int64     `gorm:"not null;default:0"`
	ResponseBytes int64     `gorm:"not null;default:0"`
	Transport     string    `gorm:"size:32;not null;default:'tls_client'"`
	CreatedAt     time.Time `gorm:"not null;index:idx_egress_traffic_scope_created;index:idx_egress_traffic_node_created"`
}

func (egressTrafficHopModel) TableName() string { return "egress_traffic_hops" }
