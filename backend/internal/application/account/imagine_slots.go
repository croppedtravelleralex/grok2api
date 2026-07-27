package account

// ImagineSlotRegistryIDs 返回 BE-024 SlotRegistry 视图。
// configured 为空时默认整个 dispatch 池都在槽位内（向后兼容，不收窄 pin）。
func ImagineSlotRegistryIDs(configured, dispatch []uint64) []uint64 {
	if len(configured) == 0 {
		return append([]uint64(nil), dispatch...)
	}
	return intersectSortedUint64(configured, dispatch)
}

// TicketReadyAccountIDs 计算 TicketReady = slotRegistry ∩ dispatch ∩ pin ∩ 有可用票。
// ticketCounts 为 nil 时视为无票约束（返回 slot∩dispatch∩pin）。
func TicketReadyAccountIDs(slotRegistry, dispatch, pinIDs []uint64, ticketCounts map[uint64]int64) []uint64 {
	base := intersectSortedUint64(dispatch, slotRegistry)
	if len(pinIDs) > 0 {
		base = intersectSortedUint64(base, pinIDs)
	}
	if len(ticketCounts) == 0 {
		return base
	}
	ready := make([]uint64, 0, len(base))
	for _, id := range base {
		if ticketCounts[id] > 0 {
			ready = append(ready, id)
		}
	}
	return ready
}

func intersectSortedUint64(a, b []uint64) []uint64 {
	if len(a) == 0 || len(b) == 0 {
		return nil
	}
	out := make([]uint64, 0, len(a))
	i, j := 0, 0
	for i < len(a) && j < len(b) {
		switch {
		case a[i] < b[j]:
			i++
		case a[i] > b[j]:
			j++
		default:
			out = append(out, a[i])
			i++
			j++
		}
	}
	return out
}
