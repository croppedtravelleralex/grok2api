package chrometicket

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"

	domain "github.com/chenyme/grok2api/backend/internal/domain/chrometicket"
	"github.com/chenyme/grok2api/backend/internal/repository"
)

const defaultTicketTTL = 12 * time.Hour

// Pool 管理 Chrome 长效票入池、取票与清扫。
type Pool struct {
	repo   repository.ChromeTicketRepository
	logger *slog.Logger
	ttl    time.Duration
}

func NewPool(repo repository.ChromeTicketRepository, logger *slog.Logger) *Pool {
	if logger == nil {
		logger = slog.Default()
	}
	return &Pool{repo: repo, logger: logger, ttl: defaultTicketTTL}
}

func (p *Pool) Push(ctx context.Context, input domain.PushInput) (domain.Ticket, error) {
	if p == nil || p.repo == nil {
		return domain.Ticket{}, fmt.Errorf("Chrome 票池未启用")
	}
	if input.TTL <= 0 {
		input.TTL = p.ttl
	}
	return p.repo.Push(ctx, input)
}

// PopForAccount 为指定账号取出一张可用票并立即标记 consumed。
func (p *Pool) PopForAccount(ctx context.Context, accountID uint64) (domain.Ticket, error) {
	if p == nil || p.repo == nil {
		return domain.Ticket{}, repository.ErrNotFound
	}
	ticket, err := p.repo.PopForAccount(ctx, accountID)
	if err != nil {
		if errors.Is(err, repository.ErrNotFound) {
			return domain.Ticket{}, err
		}
		return domain.Ticket{}, err
	}
	p.logger.Debug("chrome_ticket_popped", "ticket_id", ticket.ID, "account_id", accountID, "sign_source", ticket.SignSource)
	return ticket, nil
}

func (p *Pool) Sweep(ctx context.Context) (int64, error) {
	if p == nil || p.repo == nil {
		return 0, nil
	}
	return p.repo.SweepExpired(ctx, time.Now().UTC())
}

func (p *Pool) Stats(ctx context.Context) (domain.Stats, error) {
	if p == nil || p.repo == nil {
		return domain.Stats{}, fmt.Errorf("Chrome 票池未启用")
	}
	return p.repo.Stats(ctx, time.Now().UTC())
}

// NormalizePushInputFromFields 从结构化字段构建入池请求。
func NormalizePushInputFromFields(accountID uint64, meta, deviceCookie, userAgent, signSource string, ttl time.Duration) domain.PushInput {
	return domain.PushInput{
		AccountID: accountID, StatsigMeta: meta, DeviceCookie: deviceCookie,
		UserAgent: userAgent, SignSource: signSource, TTL: ttl,
	}
}

// NormalizePushInput 兼容 Python minter 字段命名。
func NormalizePushInput(raw map[string]any, ttl time.Duration) (domain.PushInput, error) {
	accountID, ok := uint64Value(raw["account_id"])
	if !ok {
		if alt, altOK := uint64Value(raw["accountId"]); altOK {
			accountID, ok = alt, true
		}
	}
	if !ok || accountID == 0 {
		return domain.PushInput{}, fmt.Errorf("account_id 无效")
	}
	meta := strings.TrimSpace(stringValue(raw["statsig_meta"]))
	if meta == "" {
		meta = strings.TrimSpace(stringValue(raw["statsigMeta"]))
	}
	if meta == "" {
		return domain.PushInput{}, fmt.Errorf("statsig_meta 不能为空")
	}
	deviceCookie := strings.TrimSpace(stringValue(raw["cookie"]))
	if deviceCookie == "" {
		deviceCookie = strings.TrimSpace(stringValue(raw["device_cookie"]))
	}
	return domain.PushInput{
		AccountID: accountID, StatsigMeta: meta, DeviceCookie: deviceCookie,
		UserAgent: strings.TrimSpace(stringValue(raw["user_agent"])),
		SignSource: strings.TrimSpace(stringValue(raw["sign_source"])),
		TTL: ttl,
	}, nil
}

func stringValue(value any) string {
	if value == nil {
		return ""
	}
	switch typed := value.(type) {
	case string:
		return typed
	case fmt.Stringer:
		return typed.String()
	default:
		return fmt.Sprint(value)
	}
}

func uint64Value(value any) (uint64, bool) {
	switch typed := value.(type) {
	case float64:
		if typed <= 0 {
			return 0, false
		}
		return uint64(typed), true
	case int:
		if typed <= 0 {
			return 0, false
		}
		return uint64(typed), true
	case int64:
		if typed <= 0 {
			return 0, false
		}
		return uint64(typed), true
	case uint64:
		return typed, typed > 0
	default:
		return 0, false
	}
}
