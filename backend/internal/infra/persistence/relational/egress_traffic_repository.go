package relational

import (
	"context"
	"strings"
	"time"

	"github.com/chenyme/grok2api/backend/internal/domain/egress"
)

type EgressTrafficRepository struct {
	db *Database
}

func NewEgressTrafficRepository(db *Database) *EgressTrafficRepository {
	return &EgressTrafficRepository{db: db}
}

func (r *EgressTrafficRepository) InsertHop(ctx context.Context, hop egress.TrafficHop) error {
	if strings.TrimSpace(hop.RequestID) == "" {
		return nil
	}
	row := egressTrafficHopModel{
		RequestID: strings.TrimSpace(hop.RequestID), EgressNodeID: hop.EgressNodeID,
		EgressScope: string(hop.EgressScope), Provider: hop.Provider, Operation: hop.Operation,
		PipelineStage: hop.PipelineStage, AccountID: hop.AccountID,
		RequestBytes: hop.RequestBytes, ResponseBytes: hop.ResponseBytes, Transport: hop.Transport,
		CreatedAt: time.Now().UTC(),
	}
	return r.db.db.WithContext(ctx).Create(&row).Error
}

func (r *EgressTrafficRepository) ListByRequestID(ctx context.Context, requestID string) ([]egress.TrafficHop, error) {
	requestID = strings.TrimSpace(requestID)
	if requestID == "" {
		return nil, nil
	}
	var rows []egressTrafficHopModel
	if err := r.db.db.WithContext(ctx).Where("request_id = ?", requestID).Order("created_at ASC, id ASC").Find(&rows).Error; err != nil {
		return nil, err
	}
	out := make([]egress.TrafficHop, 0, len(rows))
	for _, row := range rows {
		out = append(out, egress.TrafficHop{
			ID: row.ID, RequestID: row.RequestID, EgressNodeID: row.EgressNodeID,
			EgressScope: egress.Scope(row.EgressScope), Provider: row.Provider, Operation: row.Operation,
			PipelineStage: row.PipelineStage, AccountID: row.AccountID,
			RequestBytes: row.RequestBytes, ResponseBytes: row.ResponseBytes, Transport: row.Transport,
			CreatedAt: row.CreatedAt.UTC(),
		})
	}
	return out, nil
}
