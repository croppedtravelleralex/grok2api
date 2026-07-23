package repository

import (
	"context"
	"time"

	"github.com/chenyme/grok2api/backend/internal/domain/chrometicket"
)

// ChromeTicketRepository 持久化 Chrome 长效票池。
type ChromeTicketRepository interface {
	Push(ctx context.Context, input chrometicket.PushInput) (chrometicket.Ticket, error)
	PopForAccount(ctx context.Context, accountID uint64) (chrometicket.Ticket, error)
	SweepExpired(ctx context.Context, now time.Time) (int64, error)
	Stats(ctx context.Context, now time.Time) (chrometicket.Stats, error)
}
