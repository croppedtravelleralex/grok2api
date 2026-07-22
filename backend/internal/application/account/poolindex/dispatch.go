package poolindex

import (
	"sync"
	"time"

	"github.com/google/btree"
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

type dispatchItem struct {
	key   dispatchKey
	entry DispatchEntry
}

func (a dispatchItem) Less(b btree.Item) bool {
	other, ok := b.(dispatchItem)
	if !ok {
		return false
	}
	return a.key.less(other.key)
}

// DispatchMirror 可选镜像（如 Redis ZSET）；失败不影响内存索引。
type DispatchMirror interface {
	Upsert(entry DispatchEntry)
	Remove(id uint64)
	TouchSelected(id uint64, at time.Time)
}

// DispatchIndex 内存 BTree 有序集 + map 旁表；可选 Mirror。
type DispatchIndex struct {
	mu     sync.RWMutex
	tree   *btree.BTree
	byID   map[uint64]dispatchItem
	mirror DispatchMirror
}

func NewDispatchIndex() *DispatchIndex {
	return &DispatchIndex{tree: btree.New(32), byID: make(map[uint64]dispatchItem)}
}

// SetMirror 设置可选持久/分布式镜像。
func (idx *DispatchIndex) SetMirror(mirror DispatchMirror) {
	idx.mu.Lock()
	idx.mirror = mirror
	idx.mu.Unlock()
}

func (idx *DispatchIndex) Len() int {
	idx.mu.RLock()
	defer idx.mu.RUnlock()
	return len(idx.byID)
}

func (idx *DispatchIndex) Upsert(entry DispatchEntry) {
	idx.mu.Lock()
	if old, ok := idx.byID[entry.ID]; ok {
		idx.tree.Delete(old)
	}
	item := dispatchItem{key: keyOf(entry), entry: entry}
	idx.tree.ReplaceOrInsert(item)
	idx.byID[entry.ID] = item
	mirror := idx.mirror
	idx.mu.Unlock()
	if mirror != nil {
		mirror.Upsert(entry)
	}
}

func (idx *DispatchIndex) Remove(id uint64) {
	idx.mu.Lock()
	if old, ok := idx.byID[id]; ok {
		idx.tree.Delete(old)
		delete(idx.byID, id)
	}
	mirror := idx.mirror
	idx.mu.Unlock()
	if mirror != nil {
		mirror.Remove(id)
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
	if limit <= 0 {
		limit = len(idx.byID)
	}
	out := make([]DispatchEntry, 0, min(limit, len(idx.byID)))
	idx.tree.Ascend(func(item btree.Item) bool {
		out = append(out, item.(dispatchItem).entry)
		return len(out) < limit
	})
	return out
}

// IDs 返回当前成员 ID 集合（对账用）。
func (idx *DispatchIndex) IDs() map[uint64]struct{} {
	idx.mu.RLock()
	defer idx.mu.RUnlock()
	out := make(map[uint64]struct{}, len(idx.byID))
	for id := range idx.byID {
		out[id] = struct{}{}
	}
	return out
}

func (idx *DispatchIndex) TouchSelected(id uint64, at time.Time) {
	idx.mu.Lock()
	old, ok := idx.byID[id]
	if !ok {
		idx.mu.Unlock()
		return
	}
	idx.tree.Delete(old)
	entry := old.entry
	entry.LastSelectedAt = at
	item := dispatchItem{key: keyOf(entry), entry: entry}
	idx.tree.ReplaceOrInsert(item)
	idx.byID[id] = item
	mirror := idx.mirror
	idx.mu.Unlock()
	if mirror != nil {
		mirror.TouchSelected(id, at)
	}
}
