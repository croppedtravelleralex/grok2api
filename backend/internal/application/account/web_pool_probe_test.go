package account

import (
	"testing"
	"time"

	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
)

func TestWebPoolAtDeadAndDualLane(t *testing.T) {
	now := time.Now().UTC()
	dead := accountdomain.Credential{ID: 1, Enabled: true, AuthStatus: accountdomain.AuthStatusActive, LastError: "web_dead: expired"}
	ctx := WebPoolContext{Credential: dead, FastRem: 10, AutoRem: 10, ImagineWindow: &accountdomain.QuotaWindow{Total: 10, Remaining: 5}}
	if got := WebPoolAt(WebLaneImage, ctx, now); got != WebPoolDead {
		t.Fatalf("image dead = %q", got)
	}
	if got := WebPoolAt(WebLaneChat, ctx, now); got != WebPoolDead {
		t.Fatalf("chat dead = %q", got)
	}

	dispatchChat := accountdomain.Credential{ID: 2, Enabled: true, AuthStatus: accountdomain.AuthStatusActive}
	chatCtx := WebPoolContext{
		Credential: dispatchChat, FastRem: 5, AutoRem: 0,
		ImagineWindow: &accountdomain.QuotaWindow{Total: 10, Remaining: 0},
		ModelState:    &accountdomain.ModelState{Status: accountdomain.ModelStatusQuotaExhausted},
	}
	if got := WebPoolAt(WebLaneImage, chatCtx, now); got != WebPoolRecovery {
		t.Fatalf("image recovery = %q", got)
	}
	if got := WebPoolAt(WebLaneChat, chatCtx, now); got != WebPoolDispatch {
		t.Fatalf("chat dispatch = %q", got)
	}
}

func TestResolveWebAcquireLane(t *testing.T) {
	if got := ResolveWebAcquireLane("grok-imagine-image", "imagine"); got != WebLaneImage {
		t.Fatalf("imagine lane = %q", got)
	}
	if got := ResolveWebAcquireLane("grok-3-fast", "fast"); got != WebLaneChat {
		t.Fatalf("chat lane = %q", got)
	}
}

func TestQueueQuotaRefreshImagine(t *testing.T) {
	service := NewService(nil, nil, nil, nil, nil, nil, nil)
	service.QueueQuotaRefresh(42, "imagine")
	service.quotaRefreshMu.Lock()
	_, ok := service.quotaRefreshes["42:imagine"]
	service.quotaRefreshMu.Unlock()
	if !ok {
		t.Fatal("imagine mode should be queued")
	}
}
