package repository

import (
	"context"

	"github.com/chenyme/grok2api/backend/internal/domain/egress"
)

type EgressTrafficRepository interface {
	InsertHop(ctx context.Context, hop egress.TrafficHop) error
	ListByRequestID(ctx context.Context, requestID string) ([]egress.TrafficHop, error)
}
