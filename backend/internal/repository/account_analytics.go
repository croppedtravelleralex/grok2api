package repository

import (
	"context"
	"time"

	"github.com/chenyme/grok2api/backend/internal/domain/account"
)

// AccountPoolSnapshot 保存号池在固定时间桶内的聚合状态，不记录任何账号凭据。
type AccountPoolSnapshot struct {
	BucketAt       time.Time
	Provider       account.Provider
	Total          int64
	Available      int64
	Cooldown       int64
	WaitingReset   int64
	Probing        int64
	Disabled       int64
	ReauthRequired int64
	Free           int64
	Paid           int64
	Unknown        int64
	TierAuto       int64
	TierBasic      int64
	TierSuper      int64
	TierHeavy      int64
	QuotaRemaining float64
	QuotaTotal     float64
	QuotaKnown     int64
}

// AccountAnalyticsRepository 是可选的号池历史能力，避免扩大核心账号仓储接口。
type AccountAnalyticsRepository interface {
	CaptureAccountPoolSnapshots(ctx context.Context, at time.Time) ([]AccountPoolSnapshot, error)
	ListAccountPoolSnapshots(ctx context.Context, from, to time.Time) ([]AccountPoolSnapshot, error)
	PruneAccountPoolSnapshots(ctx context.Context, before time.Time) (int64, error)
}
