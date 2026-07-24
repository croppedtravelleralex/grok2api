package relational

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"strings"
	"time"

	"github.com/chenyme/grok2api/backend/internal/domain/chrometicket"
	"github.com/chenyme/grok2api/backend/internal/repository"
	"gorm.io/gorm"
)

type ChromeTicketRepository struct {
	db *Database
}

func NewChromeTicketRepository(db *Database) *ChromeTicketRepository {
	return &ChromeTicketRepository{db: db}
}

var _ repository.ChromeTicketRepository = (*ChromeTicketRepository)(nil)

func (r *ChromeTicketRepository) Push(ctx context.Context, input chrometicket.PushInput) (chrometicket.Ticket, error) {
	meta := strings.TrimSpace(input.StatsigMeta)
	if meta == "" {
		return chrometicket.Ticket{}, fmt.Errorf("statsig_meta 不能为空")
	}
	if input.AccountID == 0 {
		return chrometicket.Ticket{}, fmt.Errorf("account_id 无效")
	}
	ttl := input.TTL
	if ttl <= 0 {
		ttl = 12 * time.Hour
	}
	now := time.Now().UTC()
	model := chromeTicketModel{
		ID: newChromeTicketID(), AccountID: input.AccountID, StatsigMeta: meta,
		DeviceCookie: strings.TrimSpace(input.DeviceCookie), UserAgent: strings.TrimSpace(input.UserAgent),
		SignSource: strings.TrimSpace(input.SignSource), CreatedAt: now, ExpiresAt: now.Add(ttl),
		Status: chrometicket.StatusAvailable,
	}
	if err := r.db.db.WithContext(ctx).Create(&model).Error; err != nil {
		return chrometicket.Ticket{}, fmt.Errorf("写入 Chrome 票: %w", err)
	}
	return toChromeTicketDomain(model), nil
}

func (r *ChromeTicketRepository) PopForAccount(ctx context.Context, accountID uint64) (chrometicket.Ticket, error) {
	if accountID == 0 {
		return chrometicket.Ticket{}, fmt.Errorf("account_id 无效")
	}
	var ticket chrometicket.Ticket
	err := r.db.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		now := time.Now().UTC()
		if err := sweepExpiredTickets(tx, now); err != nil {
			return err
		}
		var model chromeTicketModel
		query := tx.Where("status = ? AND expires_at >= ? AND account_id = ?", chrometicket.StatusAvailable, now, accountID).
			Order("created_at ASC").Limit(1)
		if err := query.First(&model).Error; err != nil {
			return mapError(err)
		}
		consumedAt := now
		result := tx.Model(&chromeTicketModel{}).Where("id = ? AND status = ?", model.ID, chrometicket.StatusAvailable).
			Updates(map[string]any{"status": chrometicket.StatusConsumed, "consumed_at": consumedAt})
		if result.Error != nil {
			return fmt.Errorf("消费 Chrome 票: %w", result.Error)
		}
		if result.RowsAffected == 0 {
			return repository.ErrNotFound
		}
		model.Status = chrometicket.StatusConsumed
		model.ConsumedAt = &consumedAt
		ticket = toChromeTicketDomain(model)
		return nil
	})
	if err != nil {
		return chrometicket.Ticket{}, err
	}
	return ticket, nil
}

func (r *ChromeTicketRepository) SweepExpired(ctx context.Context, now time.Time) (int64, error) {
	result := r.db.db.WithContext(ctx).Model(&chromeTicketModel{}).
		Where("status = ? AND expires_at < ?", chrometicket.StatusAvailable, now.UTC()).
		Update("status", chrometicket.StatusExpired)
	if result.Error != nil {
		return 0, fmt.Errorf("清理过期 Chrome 票: %w", result.Error)
	}
	return result.RowsAffected, nil
}

func (r *ChromeTicketRepository) Stats(ctx context.Context, now time.Time) (chrometicket.Stats, error) {
	now = now.UTC()
	if _, err := r.SweepExpired(ctx, now); err != nil {
		return chrometicket.Stats{}, err
	}
	type statusRow struct {
		Status string
		Count  int64
	}
	var statusRows []statusRow
	if err := r.db.db.WithContext(ctx).Model(&chromeTicketModel{}).
		Select("status, COUNT(*) AS count").Group("status").Scan(&statusRows).Error; err != nil {
		return chrometicket.Stats{}, fmt.Errorf("统计 Chrome 票状态: %w", err)
	}
	type accountRow struct {
		AccountID uint64
		Count     int64
	}
	var accountRows []accountRow
	if err := r.db.db.WithContext(ctx).Model(&chromeTicketModel{}).
		Select("account_id, COUNT(*) AS count").
		Where("status = ?", chrometicket.StatusAvailable).
		Group("account_id").Order("count DESC").Limit(20).Scan(&accountRows).Error; err != nil {
		return chrometicket.Stats{}, fmt.Errorf("统计 Chrome 票账号分布: %w", err)
	}
	stats := chrometicket.Stats{
		ByStatus:          make(map[string]int64, len(statusRows)),
		TTLDistribution:   make(map[string]int64),
	}
	for _, row := range statusRows {
		stats.ByStatus[row.Status] = row.Count
	}
	for _, row := range accountRows {
		stats.AvailableByAccount = append(stats.AvailableByAccount, chrometicket.AccountCount{
			AccountID: row.AccountID, Count: row.Count,
		})
	}
	available, err := r.ListAvailable(ctx, now, 500)
	if err != nil {
		return chrometicket.Stats{}, err
	}
	for _, ticket := range available {
		remaining := int64(ticket.ExpiresAt.Sub(now).Seconds())
		if remaining < 0 {
			remaining = 0
		}
		stats.AvailableTickets = append(stats.AvailableTickets, chrometicket.TicketSummary{
			ID: ticket.ID, AccountID: ticket.AccountID, CreatedAt: ticket.CreatedAt,
			ExpiresAt: ticket.ExpiresAt, TTLRemainingSeconds: remaining, SignSource: ticket.SignSource,
		})
		bucket := ttlBucket(remaining)
		stats.TTLDistribution[bucket]++
		if stats.EarliestExpiresAt == nil || ticket.ExpiresAt.Before(*stats.EarliestExpiresAt) {
			expiresAt := ticket.ExpiresAt
			stats.EarliestExpiresAt = &expiresAt
		}
	}
	if stats.EarliestExpiresAt != nil {
		stats.EarliestExpiresInSec = int64(stats.EarliestExpiresAt.Sub(now).Seconds())
		if stats.EarliestExpiresInSec < 0 {
			stats.EarliestExpiresInSec = 0
		}
	}
	return stats, nil
}

func (r *ChromeTicketRepository) ListAvailable(ctx context.Context, now time.Time, limit int) ([]chrometicket.Ticket, error) {
	if limit <= 0 {
		limit = 200
	}
	now = now.UTC()
	var models []chromeTicketModel
	if err := r.db.db.WithContext(ctx).
		Where("status = ? AND expires_at >= ?", chrometicket.StatusAvailable, now).
		Order("expires_at ASC, created_at ASC").
		Limit(limit).
		Find(&models).Error; err != nil {
		return nil, fmt.Errorf("列出可用 Chrome 票: %w", err)
	}
	out := make([]chrometicket.Ticket, 0, len(models))
	for _, model := range models {
		out = append(out, toChromeTicketDomain(model))
	}
	return out, nil
}

func ttlBucket(remainingSeconds int64) string {
	switch {
	case remainingSeconds < 3600:
		return "<1h"
	case remainingSeconds < 3*3600:
		return "1-3h"
	case remainingSeconds < 6*3600:
		return "3-6h"
	case remainingSeconds < 12*3600:
		return "6-12h"
	default:
		return ">12h"
	}
}

func sweepExpiredTickets(tx *gorm.DB, now time.Time) error {
	result := tx.Model(&chromeTicketModel{}).
		Where("status = ? AND expires_at < ?", chrometicket.StatusAvailable, now.UTC()).
		Update("status", chrometicket.StatusExpired)
	if result.Error != nil {
		return fmt.Errorf("清理过期 Chrome 票: %w", result.Error)
	}
	return nil
}

func toChromeTicketDomain(model chromeTicketModel) chrometicket.Ticket {
	return chrometicket.Ticket{
		ID: model.ID, AccountID: model.AccountID, StatsigMeta: model.StatsigMeta,
		DeviceCookie: model.DeviceCookie, UserAgent: model.UserAgent, SignSource: model.SignSource,
		CreatedAt: model.CreatedAt, ExpiresAt: model.ExpiresAt, ConsumedAt: model.ConsumedAt, Status: model.Status,
	}
}

func newChromeTicketID() string {
	value := make([]byte, 16)
	if _, err := rand.Read(value); err != nil {
		return fmt.Sprintf("%032x", time.Now().UTC().UnixNano())
	}
	return hex.EncodeToString(value)
}
