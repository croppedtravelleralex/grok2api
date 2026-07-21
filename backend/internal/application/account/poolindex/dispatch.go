// Package poolindex 提供 Build 四池热路径索引：调度有序集、到期最小堆与 DRR。
package poolindex

import (
	"sort"
	"sync"
	"time"
)

// DispatchEntry 是调度池有序集中的一条账号记录。
type DispatchEntry struct {
	ID             uint64
	Priority       int
	QuotaRemaining float64
	QuotaKnown     bool
	LastSelectedAt time.Time
}

type dispatchKey struct {
	priority       int
	quotaRemaining float64
	quotaKnown     bool
	lastSelectedAt time.Time
	id             uint64
}

func (a dispatchKey) less(b dispatchKey) bool {
	if a.priority != b.priority {
		return a.priority > b.priority
	}
	if a.quotaKnown != b.quotaKnown {
		return a.quotaKnown && !b.quotaKnown
	}
	if a.quotaKnown && a.quotaRemaining != b.quotaRemaining {
		return a.quotaRemaining > b.quotaRemaining
	}
	if !a.lastSelectedAt.Equal(b.lastSelectedAt) {
		return a.lastSelectedAt.Before(b.lastSelectedAt)
	}
	return a.id < b.id
}

func keyOf(entry DispatchEntry) dispatchKey {
	return dispatchKey{
		priority: entry.Priority, quotaRemaining: entry.QuotaRemaining, quotaKnown: entry.QuotaKnown,
		lastSelectedAt: entry.LastSelectedAt, id: entry.ID,
	}
}

// DispatchIndex 内存有序集：复合 key 排序 + map 旁表，复杂度同平衡树。
type DispatchIndex struct {
	mu      sync.RWMutex
	ordered []DispatchEntry
	byID    map[uint64]int
}

func NewDispatchIndex() *DispatchIndex {
	return &DispatchIndex{byID: make(map[uint64]int)}
}

func (idx *DispatchIndex) Len() int {
	idx.mu.RLock()
	defer idx.mu.RUnlock()
	return len(idx.ordered)
}

func (idx *DispatchIndex) Upsert(entry DispatchEntry) {
	idx.mu.Lock()
	defer idx.mu.Unlock()
	if pos, ok := idx.byID[entry.ID]; ok {
		idx.removeAtLocked(pos)
	}
	key := keyOf(entry)
	pos := sort.Search(len(idx.ordered), func(i int) bool {
		return !keyOf(idx.ordered[i]).less(key)
	})
	idx.ordered = append(idx.ordered, DispatchEntry{})
	copy(idx.ordered[pos+1:], idx.ordered[pos:])
	idx.ordered[pos] = entry
	idx.reindexFromLocked(pos)
}

func (idx *DispatchIndex) Remove(id uint64) {
	idx.mu.Lock()
	defer idx.mu.Unlock()
	if pos, ok := idx.byID[id]; ok {
		idx.removeAtLocked(pos)
	}
}

func (idx *DispatchIndex) Contains(id uint64) bool {
	idx.mu.RLock()
	defer idx.mu.RUnlock()
	_, ok := idx.byID[id]
	return ok
}

// Ascend 按调度优先序返回至多 limit 条快照。
func (idx *DispatchIndex) Ascend(limit int) []DispatchEntry {
	idx.mu.RLock()
	defer idx.mu.RUnlock()
	if limit <= 0 || limit > len(idx.ordered) {
		limit = len(idx.ordered)
	}
	out := make([]DispatchEntry, limit)
	copy(out, idx.ordered[:limit])
	return out
}

func (idx *DispatchIndex) TouchSelected(id uint64, at time.Time) {
	idx.mu.Lock()
	defer idx.mu.Unlock()
	pos, ok := idx.byID[id]
	if !ok {
		return
	}
	entry := idx.ordered[pos]
	idx.removeAtLocked(pos)
	entry.LastSelectedAt = at
	key := keyOf(entry)
	insert := sort.Search(len(idx.ordered), func(i int) bool {
		return !keyOf(idx.ordered[i]).less(key)
	})
	idx.ordered = append(idx.ordered, DispatchEntry{})
	copy(idx.ordered[insert+1:], idx.ordered[insert:])
	idx.ordered[insert] = entry
	idx.reindexFromLocked(insert)
}

func (idx *DispatchIndex) removeAtLocked(pos int) {
	id := idx.ordered[pos].ID
	copy(idx.ordered[pos:], idx.ordered[pos+1:])
	idx.ordered = idx.ordered[:len(idx.ordered)-1]
	delete(idx.byID, id)
	idx.reindexFromLocked(pos)
}

func (idx *DispatchIndex) reindexFromLocked(start int) {
	for i := start; i < len(idx.ordered); i++ {
		idx.byID[idx.ordered[i].ID] = i
	}
}
