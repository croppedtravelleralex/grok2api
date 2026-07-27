package account

import "time"

// ImagineQuotaFreshTTL 是 Imagine 闸门同步与 Lite 成功证据的有效窗口。
const ImagineQuotaFreshTTL = 30 * time.Minute

// ImagineQuotaLimitUnknown 表示 free-usage-gates 返回 0/0：闸门不适用或上限未知，不等于耗尽。
func ImagineQuotaLimitUnknown(total, remaining int) bool {
	return total == 0 && remaining == 0
}

// ImagineQuotaExhausted 表示上游明确返回正总量且剩余为 0。
func ImagineQuotaExhausted(total, remaining int) bool {
	return total > 0 && remaining <= 0
}

// ImagineQuotaKnownPositive 表示闸门返回可解析的正剩余次数（含 micro-credit）。
func ImagineQuotaKnownPositive(total, remaining int) bool {
	return total > 0 && remaining > 0
}

// ImagineWindowUpstreamFresh 只检查 imagine 窗口是否为近期上游同步。
func ImagineWindowUpstreamFresh(window *QuotaWindow, now time.Time) bool {
	if window == nil || window.Mode != "imagine" {
		return false
	}
	if window.Source != QuotaSourceUpstream {
		return false
	}
	if window.SyncedAt == nil || now.Sub(*window.SyncedAt) > ImagineQuotaFreshTTL {
		return false
	}
	return true
}

// ImagineKnownQuotaFresh 表示闸门返回了可信的正额度且同步未过期。
func ImagineKnownQuotaFresh(window *QuotaWindow, now time.Time) bool {
	if !ImagineWindowUpstreamFresh(window, now) {
		return false
	}
	return ImagineQuotaKnownPositive(window.Total, window.Remaining)
}

// ImagineQuotaUnknownFresh 表示近期同步的 0/0 未知闸门。
func ImagineQuotaUnknownFresh(window *QuotaWindow, now time.Time) bool {
	if !ImagineWindowUpstreamFresh(window, now) {
		return false
	}
	return ImagineQuotaLimitUnknown(window.Total, window.Remaining)
}

// ImagineRecentLiteSuccess 表示模型状态记录近期 Lite 真实成功。
func ImagineRecentLiteSuccess(state *ModelState, now time.Time) bool {
	if state == nil || state.Status != ModelStatusAvailable || state.LastSuccessAt == nil {
		return false
	}
	return now.Sub(*state.LastSuccessAt) <= ImagineQuotaFreshTTL
}

// ImagineRemainingGenerations 将 imagine 窗口换算为可读剩余次数；未知时 known=false。
func ImagineRemainingGenerations(window *QuotaWindow) (generations int, known bool) {
	if window == nil || window.Mode != "imagine" {
		return 0, false
	}
	if ImagineQuotaLimitUnknown(window.Total, window.Remaining) {
		return 0, false
	}
	if ImagineQuotaExhausted(window.Total, window.Remaining) {
		return 0, true
	}
	return ImagineGenerations(window.Remaining, window.Total)
}

// ImagineDispatchQuotaAdmissible 判断仅凭额度窗口与模型状态是否允许进入 Lite 调度。
// Lite 本身不返回剩余次数；0/0 时依赖近期 Lite 成功或待探测状态。
func ImagineDispatchQuotaAdmissible(window *QuotaWindow, state *ModelState, now time.Time) bool {
	if !ImagineWindowUpstreamFresh(window, now) {
		return false
	}
	if ImagineQuotaExhausted(window.Total, window.Remaining) {
		return false
	}
	if ImagineKnownQuotaFresh(window, now) {
		return true
	}
	if !ImagineQuotaUnknownFresh(window, now) {
		return false
	}
	if state == nil {
		return false
	}
	switch state.Status {
	case ModelStatusAvailable:
		// available 表示历史上 Lite 真实成功；闸门 0/0 不可靠，勿因无近期成功而踢出。
		return true
	case ModelStatusQuotaAvailable, ModelStatusUnknown:
		return true
	default:
		return false
	}
}

// ImagineGenerations converts upstream imagine quota counters into human-readable generation counts.
// Grok may return small integers (e.g. 12/7) or large micro-credit values (e.g. 3850000000).
func ImagineGenerations(remaining, total int) (generations int, known bool) {
	if total <= 0 || remaining < 0 {
		return 0, false
	}
	if total <= 1000 {
		return remaining, true
	}
	unit := total / 10
	if unit < 1 {
		unit = 1
	}
	return (remaining + unit - 1) / unit, true
}

// ImagineGenerationsTotal returns the total generation allowance for an imagine window.
func ImagineGenerationsTotal(total int) (generations int, known bool) {
	if total <= 0 {
		return 0, false
	}
	if total <= 1000 {
		return total, true
	}
	unit := total / 10
	if unit < 1 {
		unit = 1
	}
	return (total + unit - 1) / unit, true
}
