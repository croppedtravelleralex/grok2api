package account

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
