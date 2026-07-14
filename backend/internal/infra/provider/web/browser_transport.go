package web

import (
	"context"
	"net/http"
	"time"

	infraegress "github.com/chenyme/grok2api/backend/internal/infra/egress"
)

// doModelRequest 在配置浏览器桥接时强制通过真实 Chromium 发起模型请求；未配置时保留原传输，方便本地开发。
func (a *Adapter) doModelRequest(ctx context.Context, lease *infraegress.Lease, request *http.Request, timeout time.Duration) (*http.Response, error) {
	if a.bridge != nil {
		return a.bridge.Do(ctx, lease, request, timeout)
	}
	return lease.Do(request)
}
