package imagepipeline

import (
	"context"
	"errors"
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

func TestNormalizeConfig(t *testing.T) {
	cfg := normalizeConfig(Config{})
	if cfg.PipelineSlots != 10 || cfg.ExpandConcurrency != 2 || cfg.SSEMin != 1 || cfg.SSEInitial != 1 {
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

func TestDefaultAIMDStartsAtOneAndFallsBackToOneOnSoftStop(t *testing.T) {
	s := NewScheduler(newMemRepo(), DefaultConfig(), nil)
	if target := s.Snapshot().SSETarget; target != 1 {
		t.Fatalf("默认 SSE target=%d，期望从单并发开始", target)
	}

	s.mu.Lock()
	s.sseTarget = 2
	s.mu.Unlock()
	s.recordOutcome(outcomeSample{ok: false, softStop: true, totalMS: 7000, at: time.Now()})
	if target := s.Snapshot().SSETarget; target != 1 {
		t.Fatalf("soft-stop 后 SSE target=%d，期望回退到 1", target)
	}
}

func TestAdmissionQueueIsFIFOAndSnapshotShowsSlotOwners(t *testing.T) {
	s := NewScheduler(newMemRepo(), Config{PipelineSlots: 1, QueueCapacity: 2, ExpandConcurrency: 1, SSEMin: 1, SSEInitial: 1, SSEMax: 1, DownloadConcurrency: 1}, nil)
	r1, err := s.Admit(context.Background(), AdmitInput{RequestID: "r1", Model: "m"})
	if err != nil {
		t.Fatal(err)
	}

	order := make(chan string, 2)
	start := func(id string) {
		go func() {
			run, admitErr := s.Admit(context.Background(), AdmitInput{RequestID: id, Model: "m"})
			if admitErr != nil {
				order <- "error:" + admitErr.Error()
				return
			}
			order <- id
			run.Finish(domain.StatusSucceeded, "", false)
		}()
	}
	start("r2")
	waitForQueueDepth(t, s, 1)
	start("r3")
	waitForQueueDepth(t, s, 2)

	snapshot := s.Snapshot()
	if len(snapshot.Slots) != 1 || !snapshot.Slots[0].Occupied || snapshot.Slots[0].RequestID != "r1" {
		t.Fatalf("槽位快照不正确: %+v", snapshot.Slots)
	}
	if len(snapshot.Queue) != 2 || snapshot.Queue[0].Position != 1 || snapshot.Queue[0].RequestID != "r2" || snapshot.Queue[1].Position != 2 || snapshot.Queue[1].RequestID != "r3" {
		t.Fatalf("队列快照不正确: %+v", snapshot.Queue)
	}

	r1.Finish(domain.StatusSucceeded, "", false)
	if got := <-order; got != "r2" {
		t.Fatalf("FIFO 第一项 = %s", got)
	}
	if got := <-order; got != "r3" {
		t.Fatalf("FIFO 第二项 = %s", got)
	}
}

func TestCanceledQueueWaiterDoesNotLeakSlot(t *testing.T) {
	s := NewScheduler(newMemRepo(), Config{PipelineSlots: 1, QueueCapacity: 1, ExpandConcurrency: 1, SSEMin: 1, SSEInitial: 1, SSEMax: 1, DownloadConcurrency: 1}, nil)
	r1, err := s.Admit(context.Background(), AdmitInput{RequestID: "r1", Model: "m"})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		_, admitErr := s.Admit(ctx, AdmitInput{RequestID: "r2", Model: "m"})
		done <- admitErr
	}()
	waitForQueueDepth(t, s, 1)
	cancel()
	if err := <-done; err == nil {
		t.Fatal("取消的排队请求意外成功")
	}
	waitForQueueDepth(t, s, 0)
	r1.Finish(domain.StatusSucceeded, "", false)

	r3, err := s.Admit(context.Background(), AdmitInput{RequestID: "r3", Model: "m"})
	if err != nil {
		t.Fatalf("取消等待者后槽位泄漏: %v", err)
	}
	r3.Finish(domain.StatusSucceeded, "", false)
}

func TestExpandStageQueueIsFIFOAndVisible(t *testing.T) {
	s := NewScheduler(newMemRepo(), Config{PipelineSlots: 3, QueueCapacity: 3, ExpandConcurrency: 1, SSEMin: 1, SSEInitial: 1, SSEMax: 1, SSEStagger: time.Millisecond, DownloadConcurrency: 1}, nil)
	runs := make([]*Run, 3)
	for i := range runs {
		run, err := s.Admit(context.Background(), AdmitInput{RequestID: string(rune('1' + i)), Model: "m"})
		if err != nil {
			t.Fatal(err)
		}
		runs[i] = run
		defer run.Finish(domain.StatusSucceeded, "", false)
	}
	if err := runs[0].AcquireExpand(context.Background()); err != nil {
		t.Fatal(err)
	}

	acquired := make(chan int, 2)
	release := []chan struct{}{make(chan struct{}), make(chan struct{})}
	for index := 1; index < 3; index++ {
		index := index
		go func() {
			if err := runs[index].AcquireExpand(context.Background()); err != nil {
				acquired <- -index
				return
			}
			acquired <- index
			<-release[index-1]
			runs[index].ReleaseExpand()
		}()
		waitForStageQueueDepth(t, s, domain.StageExpand, index)
	}

	snapshot := s.Snapshot()
	if snapshot.ExpandActive != 1 || snapshot.ExpandQueued != 2 || snapshot.Slots[runs[1].Lane()].WaitingFor != domain.StageExpand {
		t.Fatalf("扩写队列快照不正确: active=%d queued=%d slots=%+v", snapshot.ExpandActive, snapshot.ExpandQueued, snapshot.Slots)
	}
	runs[0].ReleaseExpand()
	if got := <-acquired; got != 1 {
		t.Fatalf("扩写 FIFO 第一项=%d", got)
	}
	close(release[0])
	if got := <-acquired; got != 2 {
		t.Fatalf("扩写 FIFO 第二项=%d", got)
	}
	close(release[1])
}

func TestCanceledStageWaiterDoesNotLeakCapacity(t *testing.T) {
	s := NewScheduler(newMemRepo(), Config{PipelineSlots: 2, QueueCapacity: 2, ExpandConcurrency: 1, SSEMin: 1, SSEInitial: 1, SSEMax: 1, SSEStagger: time.Millisecond, DownloadConcurrency: 1}, nil)
	r1, _ := s.Admit(context.Background(), AdmitInput{RequestID: "r1", Model: "m"})
	r2, _ := s.Admit(context.Background(), AdmitInput{RequestID: "r2", Model: "m"})
	defer r1.Finish(domain.StatusSucceeded, "", false)
	defer r2.Finish(domain.StatusSucceeded, "", false)
	if err := r1.AcquireDownload(context.Background()); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- r2.AcquireDownload(ctx) }()
	waitForStageQueueDepth(t, s, domain.StageDownload, 1)
	cancel()
	if err := <-done; err == nil {
		t.Fatal("取消的下图等待者意外成功")
	}
	r1.ReleaseDownload()
	if snapshot := s.Snapshot(); snapshot.DownloadActive != 0 || snapshot.DownloadQueued != 0 {
		t.Fatalf("取消后容量泄漏: %+v", snapshot)
	}
}

func TestFinishReleasesHeldStageCapacity(t *testing.T) {
	s := NewScheduler(newMemRepo(), Config{PipelineSlots: 2, QueueCapacity: 2, ExpandConcurrency: 1, SSEMin: 1, SSEInitial: 1, SSEMax: 1, SSEStagger: time.Millisecond, DownloadConcurrency: 1}, nil)
	r1, _ := s.Admit(context.Background(), AdmitInput{RequestID: "r1", Model: "m"})
	r2, _ := s.Admit(context.Background(), AdmitInput{RequestID: "r2", Model: "m"})
	if err := r1.AcquireSSE(context.Background()); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- r2.AcquireSSE(context.Background()) }()
	waitForStageQueueDepth(t, s, domain.StageSSE, 1)
	r1.Finish(domain.StatusFailed, "aborted", false)
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("Finish 未归还 SSE 容量")
	}
	r2.ReleaseSSE()
	r2.Finish(domain.StatusSucceeded, "", false)
}

func TestSegmentPersistenceFailureDoesNotLeakStageCapacity(t *testing.T) {
	s := NewScheduler(&failingSegmentRepo{newMemRepo()}, Config{PipelineSlots: 2, QueueCapacity: 2, ExpandConcurrency: 1, SSEMin: 1, SSEInitial: 1, SSEMax: 1, SSEStagger: time.Millisecond, DownloadConcurrency: 1}, nil)
	r1, _ := s.Admit(context.Background(), AdmitInput{RequestID: "r1", Model: "m"})
	r2, _ := s.Admit(context.Background(), AdmitInput{RequestID: "r2", Model: "m"})
	if err := r1.AcquireExpand(context.Background()); err != nil {
		t.Fatal(err)
	}
	r1.ReleaseExpand()
	if err := r2.AcquireExpand(context.Background()); err != nil {
		t.Fatalf("segment 落库失败后容量泄漏: %v", err)
	}
	r2.ReleaseExpand()
	r1.Finish(domain.StatusSucceeded, "", false)
	r2.Finish(domain.StatusSucceeded, "", false)
}

func waitForQueueDepth(t *testing.T, scheduler *Scheduler, depth int) {
	t.Helper()
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if scheduler.Snapshot().QueueDepth == depth {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("等待队列深度 %d 超时，当前 %d", depth, scheduler.Snapshot().QueueDepth)
}

func waitForStageQueueDepth(t *testing.T, scheduler *Scheduler, stage domain.Stage, depth int) {
	t.Helper()
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		snapshot := scheduler.Snapshot()
		actual := snapshot.ExpandQueued
		if stage == domain.StageSSE {
			actual = snapshot.SSEQueued
		} else if stage == domain.StageDownload {
			actual = snapshot.DownloadQueued
		}
		if actual == depth {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("等待 %s 队列深度 %d 超时", stage, depth)
}
