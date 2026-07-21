package poolindex

import (
	"container/heap"
	"sync"
	"time"
)

// DueItem 是到期最小堆中的条目。
type DueItem struct {
	ID    uint64
	DueAt time.Time
	index int
}

type dueHeap []*DueItem

func (h dueHeap) Len() int { return len(h) }
func (h dueHeap) Less(i, j int) bool {
	if !h[i].DueAt.Equal(h[j].DueAt) {
		return h[i].DueAt.Before(h[j].DueAt)
	}
	return h[i].ID < h[j].ID
}
func (h dueHeap) Swap(i, j int) {
	h[i], h[j] = h[j], h[i]
	h[i].index = i
	h[j].index = j
}
func (h *dueHeap) Push(x any) {
	item := x.(*DueItem)
	item.index = len(*h)
	*h = append(*h, item)
}
func (h *dueHeap) Pop() any {
	old := *h
	n := len(old)
	item := old[n-1]
	old[n-1] = nil
	item.index = -1
	*h = old[:n-1]
	return item
}

// DueHeap 按 DueAt 升序的最小堆，带 ID 旁表。
type DueHeap struct {
	mu    sync.Mutex
	items dueHeap
	byID  map[uint64]*DueItem
}

func NewDueHeap() *DueHeap {
	h := &DueHeap{byID: make(map[uint64]*DueItem)}
	heap.Init(&h.items)
	return h
}

func (h *DueHeap) Len() int {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.items.Len()
}

func (h *DueHeap) Upsert(id uint64, dueAt time.Time) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if existing, ok := h.byID[id]; ok {
		existing.DueAt = dueAt
		heap.Fix(&h.items, existing.index)
		return
	}
	item := &DueItem{ID: id, DueAt: dueAt}
	heap.Push(&h.items, item)
	h.byID[id] = item
}

func (h *DueHeap) Remove(id uint64) {
	h.mu.Lock()
	defer h.mu.Unlock()
	item, ok := h.byID[id]
	if !ok {
		return
	}
	heap.Remove(&h.items, item.index)
	delete(h.byID, id)
}

func (h *DueHeap) Contains(id uint64) bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	_, ok := h.byID[id]
	return ok
}

// PeekDue 若堆顶已到期则返回 ID；未到期或空返回 ok=false。
func (h *DueHeap) PeekDue(now time.Time) (id uint64, ok bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.items.Len() == 0 {
		return 0, false
	}
	top := h.items[0]
	if top.DueAt.After(now) {
		return 0, false
	}
	return top.ID, true
}

// PopDue 弹出已到期的堆顶；未到期不弹出。
func (h *DueHeap) PopDue(now time.Time) (id uint64, ok bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.items.Len() == 0 {
		return 0, false
	}
	top := h.items[0]
	if top.DueAt.After(now) {
		return 0, false
	}
	item := heap.Pop(&h.items).(*DueItem)
	delete(h.byID, item.ID)
	return item.ID, true
}

// PopAny 无论是否到期都弹出堆顶（删除池 FIFO 可用 epoch 0）。
func (h *DueHeap) PopAny() (id uint64, ok bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.items.Len() == 0 {
		return 0, false
	}
	item := heap.Pop(&h.items).(*DueItem)
	delete(h.byID, item.ID)
	return item.ID, true
}
