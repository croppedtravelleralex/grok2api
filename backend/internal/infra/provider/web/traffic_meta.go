package web

import (
	"context"

	"github.com/chenyme/grok2api/backend/internal/infra/egress"
	"github.com/chenyme/grok2api/backend/internal/infra/provider"
)

func withEgressTrafficMeta(ctx context.Context, operation, stage string, accountID uint64) context.Context {
	meta := egress.TrafficMeta{
		Provider: "grok_web", Operation: operation, PipelineStage: stage, AccountID: accountID,
	}
	if asset, ok := provider.ImageAssetMetadataFromContext(ctx); ok {
		meta.RequestID = asset.RequestID
	}
	return egress.WithTrafficMeta(ctx, meta)
}
