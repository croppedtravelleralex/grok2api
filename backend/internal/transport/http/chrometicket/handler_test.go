package chrometicket

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	chrometicketapp "github.com/chenyme/grok2api/backend/internal/application/chrometicket"
	domain "github.com/chenyme/grok2api/backend/internal/domain/chrometicket"
	"github.com/chenyme/grok2api/backend/internal/infra/persistence/relational"
	"github.com/gin-gonic/gin"
)

func TestHandlerPushStatsSweep(t *testing.T) {
	t.Parallel()
	gin.SetMode(gin.TestMode)
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "chrome-ticket-http.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = database.Close() })
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	pool := chrometicketapp.NewPool(relational.NewChromeTicketRepository(database), nil)
	router := gin.New()
	router.Group("/api/admin/v1").Use(func(c *gin.Context) {
		c.Set("requestId", "req-test")
		c.Next()
	})
	NewHandler(pool).Register(router.Group("/api/admin/v1"))

	pushBody := `{"account_id":1467,"statsig_meta":"meta-abc","cookie":"grok_device_id=dev1","sign_source":"chrome","ttl_hours":0.001}`
	push := httptest.NewRecorder()
	pushReq := httptest.NewRequest(http.MethodPost, "/api/admin/v1/chrome-tickets", strings.NewReader(pushBody))
	pushReq.Header.Set("Content-Type", "application/json")
	router.ServeHTTP(push, pushReq)
	if push.Code != http.StatusCreated {
		t.Fatalf("push status=%d body=%s", push.Code, push.Body.String())
	}
	var pushEnvelope struct {
		Data struct {
			ID        string `json:"id"`
			AccountID uint64 `json:"account_id"`
		} `json:"data"`
	}
	if err := json.Unmarshal(push.Body.Bytes(), &pushEnvelope); err != nil {
		t.Fatal(err)
	}
	if pushEnvelope.Data.ID == "" || pushEnvelope.Data.AccountID != 1467 {
		t.Fatalf("unexpected push response: %s", push.Body.String())
	}

	stats := httptest.NewRecorder()
	router.ServeHTTP(stats, httptest.NewRequest(http.MethodGet, "/api/admin/v1/chrome-tickets/stats", nil))
	if stats.Code != http.StatusOK || !strings.Contains(stats.Body.String(), `"available"`) {
		t.Fatalf("stats status=%d body=%s", stats.Code, stats.Body.String())
	}
	if !strings.Contains(stats.Body.String(), `"ttlDistribution"`) {
		t.Fatalf("expected ttlDistribution in stats body=%s", stats.Body.String())
	}

	invalid := httptest.NewRecorder()
	router.ServeHTTP(invalid, httptest.NewRequest(http.MethodPost, "/api/admin/v1/chrome-tickets", strings.NewReader(`{"account_id":0}`)))
	if invalid.Code != http.StatusBadRequest {
		t.Fatalf("invalid push status=%d body=%s", invalid.Code, invalid.Body.String())
	}

	time.Sleep(5 * time.Millisecond)
	sweep := httptest.NewRecorder()
	router.ServeHTTP(sweep, httptest.NewRequest(http.MethodPost, "/api/admin/v1/chrome-tickets/sweep", nil))
	if sweep.Code != http.StatusOK || !strings.Contains(sweep.Body.String(), `"expired"`) {
		t.Fatalf("sweep status=%d body=%s", sweep.Code, sweep.Body.String())
	}
}

func TestHandlerUnavailableWhenPoolNil(t *testing.T) {
	t.Parallel()
	gin.SetMode(gin.TestMode)
	router := gin.New()
	NewHandler(nil).Register(router.Group("/api/admin/v1"))

	rec := httptest.NewRecorder()
	router.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/admin/v1/chrome-tickets/stats", nil))
	if rec.Code != http.StatusServiceUnavailable || !strings.Contains(rec.Body.String(), "chromeTicketPoolUnavailable") {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
}

func TestHandlerPushAcceptsCamelCaseMeta(t *testing.T) {
	t.Parallel()
	gin.SetMode(gin.TestMode)
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "chrome-ticket-camel.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = database.Close() })
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	pool := chrometicketapp.NewPool(relational.NewChromeTicketRepository(database), nil)
	router := gin.New()
	NewHandler(pool).Register(router.Group("/api/admin/v1"))

	body := `{"account_id":42,"statsigMeta":"meta-camel","device_cookie":"grok_device_id=x"}`
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/admin/v1/chrome-tickets", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	router.ServeHTTP(rec, req)
	if rec.Code != http.StatusCreated {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}

	stats, err := pool.Stats(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if stats.ByStatus[domain.StatusAvailable] != 1 {
		t.Fatalf("expected 1 available ticket, got %+v", stats.ByStatus)
	}
}
