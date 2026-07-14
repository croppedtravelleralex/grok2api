package cli

import (
	"context"
	"io"
	"net/http"
	"strconv"
	"strings"

	domainegress "github.com/chenyme/grok2api/backend/internal/domain/egress"
	infraegress "github.com/chenyme/grok2api/backend/internal/infra/egress"
)

type egressTransport struct {
	manager  *infraegress.Manager
	fallback http.RoundTripper
}

type buildEgressAffinityContextKey struct{}

func withBuildEgressAccountAffinity(ctx context.Context, accountID uint64) context.Context {
	if accountID == 0 {
		return ctx
	}
	return context.WithValue(ctx, buildEgressAffinityContextKey{}, "account:"+strconv.FormatUint(accountID, 10))
}

func buildEgressAffinity(request *http.Request) string {
	if value, ok := request.Context().Value(buildEgressAffinityContextKey{}).(string); ok && value != "" {
		return value
	}
	if value := strings.TrimSpace(request.Header.Get("x-grok-agent-id")); value != "" {
		return "agent:" + value
	}
	if value := strings.TrimSpace(request.Header.Get("x-userid")); value != "" {
		return "user:" + value
	}
	return ""
}

func (t *egressTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	lease, configured, err := t.manager.AcquireIfConfigured(request.Context(), domainegress.ScopeBuild, buildEgressAffinity(request))
	if err != nil {
		return nil, err
	}
	if !configured {
		return t.fallback.RoundTrip(request)
	}
	if lease.UserAgent != "" {
		request.Header.Set("User-Agent", lease.UserAgent)
	}
	response, err := lease.Do(request)
	if err != nil {
		t.manager.FeedbackForScope(context.WithoutCancel(request.Context()), domainegress.ScopeBuild, lease.NodeID, 0, err)
		lease.Release()
		return nil, err
	}
	t.manager.FeedbackForScope(context.WithoutCancel(request.Context()), domainegress.ScopeBuild, lease.NodeID, response.StatusCode, nil)
	if response.Body == nil {
		lease.Release()
		return response, nil
	}
	response.Body = &egressResponseBody{ReadCloser: response.Body, release: lease.Release}
	return response, nil
}

type egressResponseBody struct {
	io.ReadCloser
	release func()
}

func (b *egressResponseBody) Close() error {
	err := b.ReadCloser.Close()
	if b.release != nil {
		b.release()
		b.release = nil
	}
	return err
}
