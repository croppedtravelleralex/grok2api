package imagepipeline

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	domain "github.com/chenyme/grok2api/backend/internal/domain/imagepipeline"
)

type failingSegmentRepo struct{ *memRepo }

func (r *failingSegmentRepo) AppendSegment(context.Context, domain.Segment) (domain.Segment, error) {
	return domain.Segment{}, errors.New("segment storage unavailable")
}

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

func testConfig() Config {
	return Config{PromptSlots: 2, SSESlots: 2, UploadConcurrency: 1, QueueCapacity: 4, DownloadConcurrency: 1}
}

func TestNormalizeConfigDefaults(t *testing.T) {
	cfg := normalizeConfig(Config{})
	if cfg.PromptSlots != 10 || cfg.SSESlots != 10 || cfg.UploadConcurrency != 8 {
		t.Fatalf("unexpected defaults: %+v", cfg)
	}
}

func TestAdmitSoftCapRejectsWhenFull(t *testing.T) {
	s := NewScheduler(newMemRepo(), Config{PromptSlots: 1, SSESlots: 1, QueueCapacity: 1, DownloadConcurrency: 1}, nil)
	run, err := s.Admit(context.Background(), AdmitInput{RequestID: "r1", Model: "m", ExpandPrompt: true})
	if err != nil {
		t.Fatal(err)
	}
	_, err = s.Admit(context.Background(), AdmitInput{RequestID: "r2", Model: "m"})
	if err != ErrQueueFull {
		t.Fatalf("want queue full, got %v", err)
	}
	run.Finish(domain.StatusSucceeded, "", false)
}

func TestPSAndSSPoolsFIFO(t *testing.T) {
	s := NewScheduler(newMemRepo(), Config{PromptSlots: 1, SSESlots: 2, UploadConcurrency: 1, QueueCapacity: 4, DownloadConcurrency: 1}, nil)
	r1, err := s.Admit(context.Background(), AdmitInput{RequestID: "r1", Model: "m"})
	if err != nil {
		t.Fatal(err)
	}
	r2, err := s.Admit(context.Background(), AdmitInput{RequestID: "r2", Model: "m"})
	if err != nil {
		t.Fatal(err)
	}
	defer r1.Finish(domain.StatusSucceeded, "", false)
	defer r2.Finish(domain.StatusSucceeded, "", false)
	if _, err := r1.AcquirePS(context.Background()); err != nil {
		t.Fatal(err)
	}
	acquired := make(chan int, 1)
	go func() {
		slot, acquireErr := r2.AcquirePS(context.Background())
		if acquireErr != nil {
			acquired <- -1
			return
		}
		acquired <- slot
		r2.ReleasePS()
	}()
	waitForSnapshot(t, s, func(snap domain.Snapshot) bool { return snap.PromptQueued == 1 })
	r1.ReleasePS()
	if slot := <-acquired; slot < 0 {
		t.Fatalf("second ps acquire failed")
	}
}

func TestStageOrderPSDownload(t *testing.T) {
	s := NewScheduler(newMemRepo(), testConfig(), nil)
	run, err := s.Admit(context.Background(), AdmitInput{RequestID: "req", Model: "imagine", ExpandPrompt: true})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if _, err := run.AcquirePS(ctx); err != nil {
		t.Fatal(err)
	}
	time.Sleep(2 * time.Millisecond)
	run.ReleasePS()
	if _, err := run.AcquireSS(ctx); err != nil {
		t.Fatal(err)
	}
	time.Sleep(2 * time.Millisecond)
	run.ReleaseSS()
	if err := run.AcquireDownload(ctx); err != nil {
		t.Fatal(err)
	}
	time.Sleep(2 * time.Millisecond)
	run.ReleaseDownload()
	run.Finish(domain.StatusSucceeded, "", false)
	trace := run.snapshotTrace()
	if trace.ExpandMS < 0 || trace.SSEMS < 0 || trace.DownloadMS < 0 {
		t.Fatalf("unexpected negative stage timings: %+v", trace)
	}
	if len(trace.Segments) == 0 && trace.ExpandMS == 0 && trace.SSEMS == 0 && trace.DownloadMS == 0 {
		t.Fatalf("expected stage activity: %+v", trace)
	}
}

func TestNeedsPSRespectsExpandPromptFlag(t *testing.T) {
	s := NewScheduler(newMemRepo(), testConfig(), nil)
	run, err := s.Admit(context.Background(), AdmitInput{RequestID: "r1", Model: "m", ExpandPrompt: false})
	if err != nil {
		t.Fatal(err)
	}
	defer run.Finish(domain.StatusSucceeded, "", false)
	if run.NeedsPS("hi") {
		t.Fatal("expand_prompt=false should skip pS")
	}
}

func TestFinishReleasesHeldStageCapacity(t *testing.T) {
	s := NewScheduler(newMemRepo(), Config{PromptSlots: 2, SSESlots: 1, UploadConcurrency: 1, QueueCapacity: 4, DownloadConcurrency: 1}, nil)
	r1, _ := s.Admit(context.Background(), AdmitInput{RequestID: "r1", Model: "m"})
	r2, _ := s.Admit(context.Background(), AdmitInput{RequestID: "r2", Model: "m"})
	if _, err := r1.AcquireSS(context.Background()); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { _, err := r2.AcquireSS(context.Background()); done <- err }()
	waitForSnapshot(t, s, func(snap domain.Snapshot) bool { return snap.SSEQueued == 1 })
	r1.Finish(domain.StatusFailed, "aborted", false)
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("Finish 未归还 sS 容量")
	}
	r2.ReleaseSS()
	r2.Finish(domain.StatusSucceeded, "", false)
}

func TestTenthParallelPSBlocksEleventhFIFO(t *testing.T) {
	s := NewScheduler(newMemRepo(), Config{PromptSlots: 10, SSESlots: 10, QueueCapacity: 20, DownloadConcurrency: 8}, nil)
	ctx := context.Background()
	runs := make([]*Run, 10)
	for i := range runs {
		run, err := s.Admit(ctx, AdmitInput{RequestID: fmt.Sprintf("r%d", i), Model: "grok-imagine-image"})
		if err != nil {
			t.Fatal(err)
		}
		if _, err := run.AcquirePS(ctx); err != nil {
			t.Fatal(err)
		}
		runs[i] = run
	}
	r11, err := s.Admit(ctx, AdmitInput{RequestID: "r11", Model: "grok-imagine-image"})
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() {
		_, acquireErr := r11.AcquirePS(ctx)
		done <- acquireErr
	}()
	waitForSnapshot(t, s, func(snap domain.Snapshot) bool { return snap.PromptQueued == 1 })
	runs[0].ReleasePS()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("第 11 个 pS 请求未在释放槽位后获得执行权")
	}
	for _, run := range runs {
		run.ReleasePS()
		run.Finish(domain.StatusSucceeded, "", false)
	}
	r11.ReleasePS()
	r11.Finish(domain.StatusSucceeded, "", false)
}

func TestTenthParallelSSBlocksEleventhFIFO(t *testing.T) {
	s := NewScheduler(newMemRepo(), Config{PromptSlots: 10, SSESlots: 10, QueueCapacity: 20, DownloadConcurrency: 8}, nil)
	ctx := context.Background()
	runs := make([]*Run, 10)
	for i := range runs {
		run, err := s.Admit(ctx, AdmitInput{RequestID: fmt.Sprintf("s%d", i), Model: "grok-imagine-image"})
		if err != nil {
			t.Fatal(err)
		}
		if _, err := run.AcquireSS(ctx); err != nil {
			t.Fatal(err)
		}
		runs[i] = run
	}
	r11, err := s.Admit(ctx, AdmitInput{RequestID: "s11", Model: "grok-imagine-image"})
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() {
		_, acquireErr := r11.AcquireSS(ctx)
		done <- acquireErr
	}()
	waitForSnapshot(t, s, func(snap domain.Snapshot) bool { return snap.SSEQueued == 1 })
	runs[0].ReleaseSS()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("第 11 个 sS 请求未在释放槽位后获得执行权")
	}
	for _, run := range runs {
		run.ReleaseSS()
		run.Finish(domain.StatusSucceeded, "", false)
	}
	r11.ReleaseSS()
	r11.Finish(domain.StatusSucceeded, "", false)
}

func TestNeedsPSFalseWhenPromptLong(t *testing.T) {
	s := NewScheduler(newMemRepo(), testConfig(), nil)
	run, err := s.Admit(context.Background(), AdmitInput{RequestID: "r1", Model: "m", ExpandPrompt: true})
	if err != nil {
		t.Fatal(err)
	}
	defer run.Finish(domain.StatusSucceeded, "", false)
	longPrompt := strings.Repeat("word ", 10)
	if run.NeedsPS(longPrompt) {
		t.Fatal("long prompt should skip pS even when expand_prompt=true")
	}
}

func waitForSnapshot(t *testing.T, scheduler *Scheduler, ok func(domain.Snapshot) bool) {
	t.Helper()
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if ok(scheduler.Snapshot()) {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("等待快照条件超时")
}

func TestPrepareRetryClearsSoftStop(t *testing.T) {
	run := &Run{}
	run.MarkSoftStop()
	run.PrepareRetry()
	if run.trace.SoftStop {
		t.Fatal("PrepareRetry should clear soft stop marker")
	}
}
