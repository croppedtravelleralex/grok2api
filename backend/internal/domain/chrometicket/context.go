package chrometicket

import "context"

type leaseKey struct{}

// WithLease 将已取出的票注入请求上下文，供 Statsig 签名与 Cookie 合并使用。
func WithLease(ctx context.Context, ticket Ticket) context.Context {
	if ctx == nil {
		ctx = context.Background()
	}
	return context.WithValue(ctx, leaseKey{}, ticket)
}

// LeaseFromContext 读取当前请求绑定的 Chrome 票。
func LeaseFromContext(ctx context.Context) (Ticket, bool) {
	if ctx == nil {
		return Ticket{}, false
	}
	value, ok := ctx.Value(leaseKey{}).(Ticket)
	return value, ok && value.StatsigMeta != ""
}
