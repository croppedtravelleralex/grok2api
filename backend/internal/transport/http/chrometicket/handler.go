package chrometicket

import (
	"net/http"
	"time"

	chrometicketapp "github.com/chenyme/grok2api/backend/internal/application/chrometicket"
	"github.com/chenyme/grok2api/backend/internal/shared/response"
	"github.com/gin-gonic/gin"
)

type Handler struct {
	pool *chrometicketapp.Pool
}

func NewHandler(pool *chrometicketapp.Pool) *Handler {
	return &Handler{pool: pool}
}

func (h *Handler) Register(router gin.IRoutes) {
	router.POST("/chrome-tickets", h.push)
	router.GET("/chrome-tickets/stats", h.stats)
	router.POST("/chrome-tickets/sweep", h.sweep)
}

type pushRequest struct {
	AccountID    uint64  `json:"account_id"`
	StatsigMeta  string  `json:"statsig_meta"`
	StatsigMeta2 string  `json:"statsigMeta"`
	DeviceCookie string  `json:"device_cookie"`
	Cookie       string  `json:"cookie"`
	UserAgent    string  `json:"user_agent"`
	SignSource   string  `json:"sign_source"`
	TTLHours     float64 `json:"ttl_hours"`
}

func (h *Handler) push(c *gin.Context) {
	if h.pool == nil {
		response.Error(c, http.StatusServiceUnavailable, "chromeTicketPoolUnavailable", "Chrome 票池未启用")
		return
	}
	var body pushRequest
	if err := c.ShouldBindJSON(&body); err != nil {
		response.Error(c, http.StatusBadRequest, "invalidPayload", "请求体无效")
		return
	}
	meta := body.StatsigMeta
	if meta == "" {
		meta = body.StatsigMeta2
	}
	deviceCookie := body.DeviceCookie
	if deviceCookie == "" {
		deviceCookie = body.Cookie
	}
	ttl := 12 * time.Hour
	if body.TTLHours > 0 {
		ttl = time.Duration(body.TTLHours * float64(time.Hour))
	}
	ticket, err := h.pool.Push(c.Request.Context(), chrometicketapp.NormalizePushInputFromFields(
		body.AccountID, meta, deviceCookie, body.UserAgent, body.SignSource, ttl,
	))
	if err != nil {
		response.Error(c, http.StatusBadRequest, "chromeTicketPushFailed", err.Error())
		return
	}
	response.Success(c, http.StatusCreated, gin.H{"id": ticket.ID, "account_id": ticket.AccountID, "expires_at": ticket.ExpiresAt})
}

func (h *Handler) stats(c *gin.Context) {
	if h.pool == nil {
		response.Error(c, http.StatusServiceUnavailable, "chromeTicketPoolUnavailable", "Chrome 票池未启用")
		return
	}
	stats, err := h.pool.Stats(c.Request.Context())
	if err != nil {
		response.Error(c, http.StatusInternalServerError, "chromeTicketStatsFailed", "读取票池统计失败")
		return
	}
	response.Success(c, http.StatusOK, stats)
}

func (h *Handler) sweep(c *gin.Context) {
	if h.pool == nil {
		response.Error(c, http.StatusServiceUnavailable, "chromeTicketPoolUnavailable", "Chrome 票池未启用")
		return
	}
	expired, err := h.pool.Sweep(c.Request.Context())
	if err != nil {
		response.Error(c, http.StatusInternalServerError, "chromeTicketSweepFailed", "清扫票池失败")
		return
	}
	response.Success(c, http.StatusOK, gin.H{"expired": expired})
}
