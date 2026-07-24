package egress

import (
	"context"
	"io"
	"net/http"
	"strings"

	domain "github.com/chenyme/grok2api/backend/internal/domain/egress"
	"github.com/chenyme/grok2api/backend/internal/repository"
)

type trafficMetaKey struct{}

// TrafficMeta 随 context 传递的 egress 计量元数据。
type TrafficMeta struct {
	RequestID     string
	Provider      string
	Operation     string
	PipelineStage string
	AccountID     uint64
	Transport     string
}

func WithTrafficMeta(ctx context.Context, meta TrafficMeta) context.Context {
	if ctx == nil {
		ctx = context.Background()
	}
	return context.WithValue(ctx, trafficMetaKey{}, meta)
}

func TrafficMetaFrom(ctx context.Context) (TrafficMeta, bool) {
	if ctx == nil {
		return TrafficMeta{}, false
	}
	meta, ok := ctx.Value(trafficMetaKey{}).(TrafficMeta)
	return meta, ok
}

// TrafficRecorder 异步或同步持久化 hop。
type TrafficRecorder interface {
	Record(ctx context.Context, hop domain.TrafficHop) error
}

func NewRepositoryTrafficRecorder(repo repository.EgressTrafficRepository) TrafficRecorder {
	if repo == nil {
		return nil
	}
	return repositoryTrafficRecorder{repo: repo}
}

type repositoryTrafficRecorder struct {
	repo repository.EgressTrafficRepository
}

func (r repositoryTrafficRecorder) Record(ctx context.Context, hop domain.TrafficHop) error {
	hopCopy := hop
	go func() {
		_ = r.repo.InsertHop(context.Background(), hopCopy)
	}()
	return nil
}

func estimateRequestBytes(request *http.Request) int64 {
	if request == nil {
		return 0
	}
	var total int64
	if request.ContentLength > 0 {
		total += request.ContentLength
	}
	for key, values := range request.Header {
		total += int64(len(key))
		for _, value := range values {
			total += int64(len(value))
		}
	}
	if request.URL != nil {
		total += int64(len(request.URL.String()))
	}
	return total
}

func estimateResponseBytes(response *http.Response) int64 {
	if response == nil {
		return 0
	}
	if response.ContentLength > 0 {
		return response.ContentLength
	}
	return 0
}

func (l *Lease) recordTraffic(ctx context.Context, recorder TrafficRecorder, request *http.Request, response *http.Response) {
	if recorder == nil || l == nil {
		return
	}
	meta, _ := TrafficMetaFrom(ctx)
	transport := strings.TrimSpace(meta.Transport)
	if transport == "" {
		transport = "tls_client"
	}
	hop := domain.TrafficHop{
		RequestID:     strings.TrimSpace(meta.RequestID),
		EgressNodeID:  l.NodeID,
		EgressScope:   l.Scope,
		Provider:      strings.TrimSpace(meta.Provider),
		Operation:     strings.TrimSpace(meta.Operation),
		PipelineStage: strings.TrimSpace(meta.PipelineStage),
		AccountID:     meta.AccountID,
		RequestBytes:  estimateRequestBytes(request),
		ResponseBytes: estimateResponseBytes(response),
		Transport:     transport,
	}
	if hop.RequestID == "" && hop.AccountID == 0 && hop.Operation == "" {
		return
	}
	_ = recorder.Record(ctx, hop)
}

// DrainResponseBody 在需要精确响应字节时调用（可选）。
func DrainResponseBody(response *http.Response) int64 {
	if response == nil || response.Body == nil {
		return 0
	}
	n, _ := io.Copy(io.Discard, response.Body)
	return n
}
