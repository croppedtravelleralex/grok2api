package web

import (
	"context"
	"errors"
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

// feedbackTransportError 只把真实出口/上游传输故障反馈给代理池。浏览器桥接
// 自身不可达时保留节点健康度，避免控制面停机把全部 Webshare 节点一起冷却。
func (a *Adapter) feedbackTransportError(ctx context.Context, lease *infraegress.Lease, err error) {
	if lease == nil || err == nil {
		return
	}
	if errors.Is(err, errBrowserBridgeUnavailable) {
		a.log().Warn("browser_bridge_unavailable", "error", err)
		return
	}
	a.egress.Feedback(context.WithoutCancel(ctx), lease.NodeID, 0, err)
}
