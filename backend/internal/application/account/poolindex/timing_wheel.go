package poolindex

import (
	"sync"
	"time"
)

// TimingWheel 简易层级时间轮：短冷却到期回调。
// 单层环形槽，tick 粒度默认 1s，覆盖约 wheelSize*tick 窗口；超出则放入 overflow 最小堆语义列表。
type TimingWheel struct {
	mu        sync.Mutex
	tick      time.Duration
	slots     []map[uint64]time.Time
	cursor    int
	startedAt time.Time
	overflow  map[uint64]time.Time
	pending   map[uint64]int // id -> slot index, -1 = overflow
}

func NewTimingWheel(tick time.Duration, slotCount int) *TimingWheel {
	if tick <= 0 {
		tick = time.Second
	}
	if slotCount < 8 {
		slotCount = 64
	}
	slots := make([]map[uint64]time.Time, slotCount)
	for i := range slots {
		slots[i] = make(map[uint64]time.Time)
	}
	return &TimingWheel{
		tick: tick, slots: slots, startedAt: time.Now().UTC(),
		overflow: make(map[uint64]time.Time), pending: make(map[uint64]int),
	}
}

// Schedule 在 dueAt 到期时弹出 id（覆盖同 id 旧闹钟）。
func (w *TimingWheel) Schedule(id uint64, dueAt time.Time) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.removeLocked(id)
	now := time.Now().UTC()
	if !dueAt.After(now) {
		dueAt = now.Add(w.tick)
	}
	delay := dueAt.Sub(now)
	steps := int(delay / w.tick)
	if steps < 1 {
		steps = 1
	}
	if steps >= len(w.slots) {
		w.overflow[id] = dueAt
		w.pending[id] = -1
		return
	}
	slot := (w.cursor + steps) % len(w.slots)
	w.slots[slot][id] = dueAt
	w.pending[id] = slot
}

// Cancel 取消 id 的闹钟。
func (w *TimingWheel) Cancel(id uint64) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.removeLocked(id)
}

func (w *TimingWheel) removeLocked(id uint64) {
	slot, ok := w.pending[id]
	if !ok {
		return
	}
	delete(w.pending, id)
	if slot < 0 {
		delete(w.overflow, id)
		return
	}
	delete(w.slots[slot], id)
}

// Advance 推进到 now，返回到期账号 ID。
func (w *TimingWheel) Advance(now time.Time) []uint64 {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.startedAt.IsZero() {
		w.startedAt = now
	}
	elapsed := int(now.Sub(w.startedAt) / w.tick)
	targetCursor := elapsed % len(w.slots)
	if elapsed < 0 {
		return nil
	}
	var due []uint64
	// 推进不超过一整圈，避免长时间停滞后一次扫爆。
	steps := 0
	for w.cursor != targetCursor && steps < len(w.slots) {
		w.cursor = (w.cursor + 1) % len(w.slots)
		steps++
		for id, at := range w.slots[w.cursor] {
			if !at.After(now) {
				due = append(due, id)
				delete(w.pending, id)
			} else {
				// 尚未真正到期，挪到 overflow 或更后槽（简化：放回 overflow）
				w.overflow[id] = at
				w.pending[id] = -1
			}
			delete(w.slots[w.cursor], id)
		}
	}
	for id, at := range w.overflow {
		if !at.After(now) {
			due = append(due, id)
			delete(w.overflow, id)
			delete(w.pending, id)
		}
	}
	return due
}
