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
	if got := WebPoolAt(WebLaneImage, ctx, now); got != WebPoolDelete {
		t.Fatalf("image delete = %q", got)
	}
	if got := WebPoolAt(WebLaneChat, ctx, now); got != WebPoolDead {
		t.Fatalf("chat dead = %q", got)
	}

	synced := now.Add(-5 * time.Minute)
	exhaustedCtx := WebPoolContext{
		Credential: accountdomain.Credential{ID: 2, Enabled: true, AuthStatus: accountdomain.AuthStatusActive},
		FastRem: 5, AutoRem: 0,
		ImagineWindow: &accountdomain.QuotaWindow{
			Mode: "imagine", Total: 10, Remaining: 0,
			SyncedAt: &synced, Source: accountdomain.QuotaSourceUpstream, UpdatedAt: synced,
		},
		ModelState: &accountdomain.ModelState{Status: accountdomain.ModelStatusQuotaExhausted},
	}
	if got := WebPoolAt(WebLaneImage, exhaustedCtx, now); got != WebPoolNormal {
		t.Fatalf("image normal = %q", got)
	}
	if got := WebPoolAt(WebLaneChat, exhaustedCtx, now); got != WebPoolDispatch {
		t.Fatalf("chat dispatch = %q", got)
	}
}

func TestWebImagePoolDispatchRequiresHealthyState(t *testing.T) {
	now := time.Now().UTC()
	synced := now.Add(-5 * time.Minute)
	fresh := &accountdomain.QuotaWindow{
		Mode: "imagine", Total: 10, Remaining: 3,
		SyncedAt: &synced, Source: accountdomain.QuotaSourceUpstream, UpdatedAt: synced,
	}
	cred := accountdomain.Credential{ID: 3, Enabled: true, AuthStatus: accountdomain.AuthStatusActive}
	dispatchCtx := WebPoolContext{
		Credential: cred, ImagineWindow: fresh,
		ModelState: &accountdomain.ModelState{Status: accountdomain.ModelStatusAvailable},
	}
	if got := WebPoolAt(WebLaneImage, dispatchCtx, now); got != WebPoolDispatch {
		t.Fatalf("dispatch = %q", got)
	}
	pinnedCtx := WebPoolContext{Credential: cred, ImagineWindow: fresh}
	if got := WebPoolAt(WebLaneImage, pinnedCtx, now); got != WebPoolVerification {
		t.Fatalf("verification = %q", got)
	}
	softStopCtx := WebPoolContext{
		Credential: cred, ImagineWindow: fresh,
		ModelState: &accountdomain.ModelState{
			Status: accountdomain.ModelStatusSoftStop, CooldownUntil: timePointer(now.Add(-time.Minute)),
		},
	}
	if got := WebPoolAt(WebLaneImage, softStopCtx, now); got != WebPoolNormal {
		t.Fatalf("soft stop normal = %q", got)
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
