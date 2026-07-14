package account

import (
	"context"
	"sort"
	"strings"
	"time"

	"github.com/chenyme/grok2api/backend/internal/repository"
)

const accountAnalyticsRetention = 90 * 24 * time.Hour

type Analytics struct {
	From            time.Time
	To              time.Time
	IntervalMinutes int
	Points          []repository.AccountPoolSnapshot
}

// CaptureAnalytics 记录当前号池聚合快照；不支持历史仓储时安静跳过。
func (s *Service) CaptureAnalytics(ctx context.Context) error {
	analytics, ok := s.accounts.(repository.AccountAnalyticsRepository)
	if !ok {
		return nil
	}
	now := s.now()
	if _, err := analytics.CaptureAccountPoolSnapshots(ctx, now); err != nil {
		return err
	}
	_, err := analytics.PruneAccountPoolSnapshots(ctx, now.Add(-accountAnalyticsRetention))
	return err
}

// Analytics 返回适合前端绘图的降采样号池历史。
func (s *Service) Analytics(ctx context.Context, period string) (Analytics, error) {
	analytics, ok := s.accounts.(repository.AccountAnalyticsRepository)
	if !ok {
		return Analytics{}, ErrUnsupported
	}
	duration, interval, err := parseAnalyticsPeriod(period)
	if err != nil {
		return Analytics{}, err
	}
	to := s.now()
	from := to.Add(-duration)
	if _, err := analytics.CaptureAccountPoolSnapshots(ctx, to); err != nil {
		return Analytics{}, err
	}
	rows, err := analytics.ListAccountPoolSnapshots(ctx, from, to)
	if err != nil {
		return Analytics{}, err
	}
	return Analytics{From: from, To: to, IntervalMinutes: int(interval / time.Minute), Points: downsampleAnalytics(rows, interval)}, nil
}

func parseAnalyticsPeriod(period string) (time.Duration, time.Duration, error) {
	switch strings.ToLower(strings.TrimSpace(period)) {
	case "", "24h":
		return 24 * time.Hour, 15 * time.Minute, nil
	case "7d":
		return 7 * 24 * time.Hour, time.Hour, nil
	case "30d":
		return 30 * 24 * time.Hour, 6 * time.Hour, nil
	default:
		return 0, 0, invalidInput("period 必须是 24h、7d 或 30d")
	}
}

func downsampleAnalytics(rows []repository.AccountPoolSnapshot, interval time.Duration) []repository.AccountPoolSnapshot {
	type key struct {
		bucket   time.Time
		provider string
	}
	latest := make(map[key]repository.AccountPoolSnapshot, len(rows))
	for _, row := range rows {
		latest[key{bucket: row.BucketAt.UTC().Truncate(interval), provider: string(row.Provider)}] = row
	}
	result := make([]repository.AccountPoolSnapshot, 0, len(latest))
	for _, row := range latest {
		result = append(result, row)
	}
	sort.Slice(result, func(i, j int) bool {
		if result[i].BucketAt.Equal(result[j].BucketAt) {
			return result[i].Provider < result[j].Provider
		}
		return result[i].BucketAt.Before(result[j].BucketAt)
	})
	return result
}
