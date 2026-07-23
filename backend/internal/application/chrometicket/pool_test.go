package chrometicket

import (
	"context"
	"errors"
	"testing"
	"time"

	domain "github.com/chenyme/grok2api/backend/internal/domain/chrometicket"
	"github.com/chenyme/grok2api/backend/internal/infra/persistence/relational"
	"github.com/chenyme/grok2api/backend/internal/repository"
)

func TestPoolPushPopSweep(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	db, err := relational.OpenSQLite(ctx, t.TempDir()+"/pool.db")
	if err != nil {
		t.Fatalf("open sqlite: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })
	if err := db.InitializeSchema(ctx); err != nil {
		t.Fatalf("schema: %v", err)
	}
	repo := relational.NewChromeTicketRepository(db)
	pool := NewPool(repo, nil)

	pushed, err := pool.Push(ctx, domain.PushInput{
		AccountID: 1467, StatsigMeta: "meta-abc", DeviceCookie: "grok_device_id=dev1", SignSource: "chrome",
	})
	if err != nil {
		t.Fatalf("push: %v", err)
	}
	if pushed.ID == "" {
		t.Fatal("expected ticket id")
	}

	popped, err := pool.PopForAccount(ctx, 1467)
	if err != nil {
		t.Fatalf("pop: %v", err)
	}
	if popped.StatsigMeta != "meta-abc" || popped.DeviceCookie != "grok_device_id=dev1" {
		t.Fatalf("unexpected ticket: %+v", popped)
	}

	_, err = pool.PopForAccount(ctx, 1467)
	if err == nil || !errors.Is(err, repository.ErrNotFound) {
		t.Fatalf("expected not found, got %v", err)
	}

	_, err = pool.Push(ctx, domain.PushInput{
		AccountID: 1467, StatsigMeta: "meta-old", TTL: time.Millisecond,
	})
	if err != nil {
		t.Fatalf("push expired: %v", err)
	}
	time.Sleep(5 * time.Millisecond)
	n, err := pool.Sweep(ctx)
	if err != nil {
		t.Fatalf("sweep: %v", err)
	}
	if n < 1 {
		t.Fatalf("expected sweep count >= 1, got %d", n)
	}
	stats, err := pool.Stats(ctx)
	if err != nil {
		t.Fatalf("stats: %v", err)
	}
	if stats.ByStatus[domain.StatusConsumed] != 1 {
		t.Fatalf("expected 1 consumed, got %+v", stats.ByStatus)
	}
}

func TestNormalizePushInput(t *testing.T) {
	t.Parallel()
	input, err := NormalizePushInput(map[string]any{
		"account_id": float64(42),
		"statsigMeta": "meta",
		"cookie": "grok_device_id=x",
		"sign_source": "chrome",
	}, 2*time.Hour)
	if err != nil {
		t.Fatalf("normalize: %v", err)
	}
	if input.AccountID != 42 || input.StatsigMeta != "meta" || input.DeviceCookie != "grok_device_id=x" {
		t.Fatalf("unexpected input: %+v", input)
	}
}
