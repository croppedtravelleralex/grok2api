package poolindex

import "sync"

// Lane 维护探针车道。
type Lane int

const (
	LaneVerification Lane = iota
	LaneNormal
	LaneDelete
)

// DRRScheduler 加权轮询（空车道跳过，deficit 结转语义）：
// 三池有任务时验证:普通:删除 = 5:3:2；验证空时普通:删除 = 7:3。
type DRRScheduler struct {
	mu       sync.Mutex
	pos      int
	pattern  []Lane
	fallback []Lane
}

func NewDRRScheduler() *DRRScheduler {
	return &DRRScheduler{
		pattern: []Lane{
			LaneVerification, LaneVerification, LaneVerification, LaneVerification, LaneVerification,
			LaneNormal, LaneNormal, LaneNormal,
			LaneDelete, LaneDelete,
		},
		fallback: []Lane{
			LaneNormal, LaneNormal, LaneNormal, LaneNormal, LaneNormal, LaneNormal, LaneNormal,
			LaneDelete, LaneDelete, LaneDelete,
		},
	}
}

// Next 选出下一车道。hasWork[i] 表示该车道当前有到期任务。
func (d *DRRScheduler) Next(hasWork [3]bool) (Lane, bool) {
	d.mu.Lock()
	defer d.mu.Unlock()
	pattern := d.pattern
	if !hasWork[LaneVerification] {
		pattern = d.fallback
	}
	for tried := 0; tried < len(pattern); tried++ {
		lane := pattern[d.pos%len(pattern)]
		d.pos++
		if hasWork[lane] {
			return lane, true
		}
	}
	for i := Lane(0); i < 3; i++ {
		if hasWork[i] {
			return i, true
		}
	}
	return 0, false
}
