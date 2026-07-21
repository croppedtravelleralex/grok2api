package imagepipeline

import (
	"context"
	"sync"
	"testing"
	"time"

	domain "github.com/chenyme/grok2api/backend/internal/domain/imagepipeline"
)

type memRepo struct {
	mu       sync.Mutex
	traces   map[string]domain.Trace
	segments map[uint64]domain.Segment
	nextID   uint64
}

func newMemRepo() *memRepo {
	return &memRepo{traces: make(map[string]domain.Trace), segments: make(map[uint64]domain.Segment)}
}

func (r *memRepo) CreateTrace(_ context.Context, value domain.Trace) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.traces[value.ID] = value
	return nil
}

func (r *memRepo) UpdateTrace(_ context.Context, value domain.Trace) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.traces[value.ID] = value
	return nil
}

func (r *memRepo) AppendSegment(_ context.Context, value domain.Segment) (domain.Segment, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.nextID++
	value.ID = r.nextID
	r.segments[value.ID] = value
	return value, nil
}

func (r *memRepo) CloseSegment(_ context.Context, id uint64, endedAt time.Time, outcome string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	seg := r.segments[id]
	seg.EndedAt = &endedAt
	seg.Outcome = outcome
	r.segments[id] = seg
	return nil
}

func (r *memRepo) ListTraces(_ context.Context, from, to time.Time, _ int) ([]domain.Trace, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]domain.Trace, 0, len(r.traces))
	for _, trace := range r.traces {
		if trace.StartedAt.After(to) {
			continue
		}
		if trace.EndedAt != nil && trace.EndedAt.Before(from) {
			continue
		}
		copyTrace := trace
		for _, seg := range r.segments {
			if seg.TraceID == trace.ID {
				copyTrace.Segments = append(copyTrace.Segments, seg)
			}
		}
		out = append(out, copyTrace)
	}
	return out, nil
}

func (r *memRepo) DeleteOlderThan(_ context.Context, before time.Time) (int64, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	var n int64
	for id, trace := range r.traces {
		if trace.EndedAt != nil && trace.StartedAt.Before(before) {
			delete(r.traces, id)
			n++
		}
	}
	return n, nil
}

func TestNormalizeConfig(t *testing.T) {
	cfg := normalizeConfig(Config{})
	if cfg.PipelineSlots != 10 || cfg.ExpandConcurrency != 2 || cfg.SSEInitial != 3 {
		t.Fatalf("unexpected defaults: %+v", cfg)
	}
}

func TestAdmitLaneReuseAndQueueFull(t *testing.T) {
	s := NewScheduler(newMemRepo(), Config{PipelineSlots: 2, QueueCapacity: 1, ExpandConcurrency: 1, SSEMin: 1, SSEInitial: 1, SSEMax: 1, DownloadConcurrency: 1}, nil)
	ctx := context.Background()
	r1, err := s.Admit(ctx, AdmitInput{RequestID: "r1", Model: "m"})
	if err != nil {
		t.Fatal(err)
	}
	r2, err := s.Admit(ctx, AdmitInput{RequestID: "r2", Model: "m"})
	if err != nil {
		t.Fatal(err)
	}
	if r1.Lane() == r2.Lane() {
		t.Fatalf("expected distinct lanes, got %d", r1.Lane())
	}
	done := make(chan error, 1)
	go func() {
		_, err := s.Admit(ctx, AdmitInput{RequestID: "r3", Model: "m"})
		done <- err
	}()
	select {
	case err := <-done:
		if err != ErrQueueFull {
			t.Fatalf("want queue full, got %v", err)
		}
	case <-time.After(200 * time.Millisecond):
		// still waiting in queue — capacity 1 means one waiter; a second waiter should fail
		_, err := s.Admit(ctx, AdmitInput{RequestID: "r4", Model: "m"})
		if err != ErrQueueFull {
			t.Fatalf("second waiter want queue full, got %v", err)
		}
		r1.Finish(domain.StatusSucceeded, "", false)
		select {
		case err := <-done:
			if err != nil {
				t.Fatalf("waiter after release: %v", err)
			}
		case <-time.After(time.Second):
			t.Fatal("waiter did not acquire after slot release")
		}
	}
	r2.Finish(domain.StatusSucceeded, "", false)
}

func TestStageOrderExpandSSEDownload(t *testing.T) {
	repo := newMemRepo()
	s := NewScheduler(repo, DefaultConfig(), nil)
	run, err := s.Admit(context.Background(), AdmitInput{RequestID: "req", Model: "imagine"})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if err := run.AcquireExpand(ctx); err != nil {
		t.Fatal(err)
	}
	time.Sleep(5 * time.Millisecond)
	run.ReleaseExpand()
	if err := run.AcquireSSE(ctx); err != nil {
		t.Fatal(err)
	}
	time.Sleep(5 * time.Millisecond)
	run.ReleaseSSE()
	if err := run.AcquireDownload(ctx); err != nil {
		t.Fatal(err)
	}
	time.Sleep(5 * time.Millisecond)
	run.ReleaseDownload()
	run.Finish(domain.StatusSucceeded, "", false)
	trace := run.snapshotTrace()
	if trace.ExpandMS <= 0 || trace.SSEMS <= 0 || trace.DownloadMS <= 0 {
		t.Fatalf("expected positive stage timings: %+v", trace)
	}
	if trace.Status != domain.StatusSucceeded {
		t.Fatalf("status=%s", trace.Status)
	}
}

func TestAIMDDecreaseOnSoftStop(t *testing.T) {
	s := NewScheduler(newMemRepo(), Config{PipelineSlots: 4, QueueCapacity: 10, ExpandConcurrency: 1, SSEMin: 2, SSEInitial: 4, SSEMax: 6, DownloadConcurrency: 1}, nil)
	for i := 0; i < 3; i++ {
		s.recordOutcome(outcomeSample{ok: false, softStop: true, totalMS: 10000, at: time.Now()})
	}
	snap := s.Snapshot()
	if snap.SSETarget >= 4 {
		t.Fatalf("expected AIMD decrease, target=%d", snap.SSETarget)
	}
}
