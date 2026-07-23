package web

import (
	"context"
	"errors"
	"strings"

	chrometicketdomain "github.com/chenyme/grok2api/backend/internal/domain/chrometicket"
	imagepipelineapp "github.com/chenyme/grok2api/backend/internal/application/imagepipeline"
	"github.com/chenyme/grok2api/backend/internal/repository"
)

func (a *Adapter) attachChromeTicket(ctx context.Context, accountID uint64) context.Context {
	pool := a.chromeTicketPool()
	if pool == nil || accountID == 0 {
		return ctx
	}
	ticket, err := pool.PopForAccount(ctx, accountID)
	if err != nil {
		if !errors.Is(err, repository.ErrNotFound) {
			a.log().Warn("chrome_ticket_pop_failed", "account_id", accountID, "error", err)
		}
		return ctx
	}
	a.log().Info("chrome_ticket_pool_hit", "ticket_id", ticket.ID, "account_id", accountID, "sign_source", ticket.SignSource)
	if run := imagepipelineapp.RunFromContext(ctx); run != nil {
		run.SetChromeDeviceCookie(ticket.DeviceCookie)
	}
	return chrometicketdomain.WithLease(ctx, ticket)
}

func chromeTicketCookieFromContext(ctx context.Context) string {
	if ticket, ok := chrometicketdomain.LeaseFromContext(ctx); ok {
		return strings.TrimSpace(ticket.DeviceCookie)
	}
	if run := imagepipelineapp.RunFromContext(ctx); run != nil {
		return strings.TrimSpace(run.Artifacts().ChromeDeviceCookie)
	}
	return ""
}

func mergeChromeTicketCookie(baseCookie, deviceCookie string) string {
	deviceCookie = strings.TrimSpace(deviceCookie)
	if deviceCookie == "" {
		return baseCookie
	}
	baseCookie = strings.TrimSpace(baseCookie)
	if baseCookie == "" {
		return deviceCookie
	}
	return baseCookie + "; " + deviceCookie
}
