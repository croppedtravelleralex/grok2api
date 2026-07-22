package poolindex

import "sync"

// WebMaintenanceLane 维护探针子车道（recovery 内 priority，非独立池）。
type WebMaintenanceLane int

const (
	WebLaneRecoveryVerify WebMaintenanceLane = iota
	WebLaneRecoveryCooldown
	WebLaneDead
)

// WebDRRScheduler 加权轮询：verify 非空时 verify:cooldown:dead = 5:3:2；verify 空时 recovery:dead = 7:3。
type WebDRRScheduler struct {
	mu       sync.Mutex
	pos      int
	pattern  []WebMaintenanceLane
	fallback []WebMaintenanceLane
}

func NewWebDRRScheduler() *WebDRRScheduler {
	return &WebDRRScheduler{
		pattern: []WebMaintenanceLane{
			WebLaneRecoveryVerify, WebLaneRecoveryVerify, WebLaneRecoveryVerify, WebLaneRecoveryVerify, WebLaneRecoveryVerify,
			WebLaneRecoveryCooldown, WebLaneRecoveryCooldown, WebLaneRecoveryCooldown,
			WebLaneDead, WebLaneDead,
		},
		fallback: []WebMaintenanceLane{
			WebLaneRecoveryVerify, WebLaneRecoveryVerify, WebLaneRecoveryVerify, WebLaneRecoveryVerify, WebLaneRecoveryVerify, WebLaneRecoveryVerify, WebLaneRecoveryVerify,
			WebLaneDead, WebLaneDead, WebLaneDead,
		},
	}
}

// Next 选出下一车道。hasWork[i] 表示该车道当前有到期任务。
func (d *WebDRRScheduler) Next(hasWork [3]bool) (WebMaintenanceLane, bool) {
	d.mu.Lock()
	defer d.mu.Unlock()
	pattern := d.pattern
	if !hasWork[WebLaneRecoveryVerify] {
		pattern = d.fallback
	}
	for tried := 0; tried < len(pattern); tried++ {
		lane := pattern[d.pos%len(pattern)]
		d.pos++
		if hasWork[lane] {
			return lane, true
		}
	}
	for i := WebMaintenanceLane(0); i < 3; i++ {
		if hasWork[i] {
			return i, true
		}
	}
	return 0, false
}
