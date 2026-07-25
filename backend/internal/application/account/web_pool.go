package account

import (
	"context"
	"sort"
	"time"

	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
	"github.com/chenyme/grok2api/backend/internal/repository"
)

const (
	webImagePoolCap = 50
	webChatPoolCap  = 50
	imagineUpstream = "grok-imagine-image"
	imagineQuotaFreshTTL = 30 * time.Minute
)

// WebPoolSnapshot 描述图池 / 对话池当前调度位。
type WebPoolSnapshot struct {
	ImagePoolIDs         []uint64                `json:"imagePoolIds"`
	ImageDispatchPoolIDs []uint64                `json:"imageDispatchPoolIds"`
	ImageSchedulableIDs  []uint64                `json:"imageSchedulableIds"`
	ImagePinIDs          []uint64                `json:"imagePinIds,omitempty"`
	ChatPoolIDs          []uint64                `json:"chatPoolIds"`
	EnabledIDs           []uint64                `json:"enabledIds"`
	ImagePoolSize        int                     `json:"imagePoolSize"`
	ChatPoolSize         int                     `json:"chatPoolSize"`
	EnabledCount         int                     `json:"enabledCount"`
	ImagePoolCap         int                     `json:"imagePoolCap"`
	ChatPoolCap          int                     `json:"chatPoolCap"`
	ReconciledAt         time.Time               `json:"reconciledAt"`
	EnabledAdded         int                     `json:"enabledAdded"`
	EnabledRemoved       int                     `json:"enabledRemoved"`
	ThreePools           WebThreePoolsPublic     `json:"threePools,omitempty"`
	FourPools            WebFourPoolsPublic      `json:"fourPools,omitempty"`
	PinNotInDispatch     []uint64                `json:"pinNotInDispatch,omitempty"`
	SelectionDiagnostics WebSelectionDiagnostics `json:"selectionDiagnostics,omitempty"`
}

type webPoolCandidate struct {
	id             uint64
	priority       int
	fastRem        int
	autoRem        int
	imagineWindow  *accountdomain.QuotaWindow
	modelState     *accountdomain.ModelState
	enabled        bool
	active         bool
	cooling        bool // 账号级 cooldown_until
	imagineBlocked bool // grok-imagine-image model block（只挡图池）
}

// ReconcileWebPools 只做「无额度/冷却出池」：在当前已启用账号里踢掉不合格号。
// 不自动 enable 任何新号（进池仍由人工/调度脚本控制），避免把未验证出口能力的账号拉进生产池。
// 图池使用独立 Imagine 额度和模型状态；对话池仍看 auto/fast；快照各最多 50。
func (s *Service) ReconcileWebPools(ctx context.Context) (WebPoolSnapshot, error) {
	now := time.Now().UTC()
	accounts, _, err := s.accounts.List(ctx, repository.AccountListQuery{
		Page:   repository.PageQuery{Offset: 0, Limit: 5000, Sort: repository.SortQuery{Field: "createdAt", Direction: repository.SortAscending}},
		Filter: repository.AccountListFilter{Provider: string(accountdomain.ProviderWeb), Now: now},
	})
	if err != nil {
		return WebPoolSnapshot{}, err
	}
	ids := make([]uint64, 0, len(accounts))
	for _, value := range accounts {
		ids = append(ids, value.ID)
	}
	windowsByAccount, err := s.accounts.GetQuotaWindows(ctx, ids)
	if err != nil {
		return WebPoolSnapshot{}, err
	}
	blocks, err := s.accounts.GetActiveModelQuotaBlocks(ctx, ids, imagineUpstream, now)
	if err != nil {
		return WebPoolSnapshot{}, err
	}
	modelStates, err := s.accounts.GetModelStates(ctx, ids)
	if err != nil {
		return WebPoolSnapshot{}, err
	}

	// 出池：已启用但 auth 失效 / 账号冷却 / 图轨与对话轨均不可用 → disable。绝不自动 enable。
	toDisable := make([]uint64, 0)
	enabledNow := make([]uint64, 0)
	for _, value := range accounts {
		fastRem, autoRem := quotaRemaining(windowsByAccount[value.ID], "fast"), quotaRemaining(windowsByAccount[value.ID], "auto")
		accountCooling := value.CooldownUntil != nil && value.CooldownUntil.After(now)
		candidate := webPoolCandidate{
			id: value.ID, priority: value.Priority, fastRem: fastRem, autoRem: autoRem,
			imagineWindow: findQuotaWindow(windowsByAccount[value.ID], "imagine"),
			modelState:    findModelState(modelStates[value.ID], imagineUpstream),
			enabled:       value.Enabled, active: value.AuthStatus == accountdomain.AuthStatusActive,
			cooling: accountCooling, imagineBlocked: blocks[value.ID],
		}
		if !candidate.enabled {
			continue
		}
		chatOK := candidate.active && !candidate.cooling && (candidate.fastRem > 0 || candidate.autoRem > 0)
		ctxInput := WebPoolContext{
			Credential: value, FastRem: candidate.fastRem, AutoRem: candidate.autoRem,
			ImagineWindow: candidate.imagineWindow, ModelState: candidate.modelState,
			ImagineBlocked: candidate.imagineBlocked,
		}
		imageOK := candidate.active && !candidate.cooling && imageAccountRetained(ctxInput, now)
		keep := candidate.active && !candidate.cooling && (chatOK || imageOK)
		if !keep {
			toDisable = append(toDisable, candidate.id)
			continue
		}
		enabledNow = append(enabledNow, candidate.id)
	}

	disabledFlag := false
	if len(toDisable) > 0 {
		if _, err := s.accounts.UpdateMany(ctx, toDisable, repository.AccountUpdates{Enabled: &disabledFlag}); err != nil {
			return WebPoolSnapshot{}, err
		}
		for _, id := range toDisable {
			_ = s.sticky.DeleteByAccount(ctx, id)
		}
	}

	sort.Slice(enabledNow, func(i, j int) bool { return enabledNow[i] < enabledNow[j] })
	if err := s.RebuildWebPoolIndex(ctx); err != nil {
		return WebPoolSnapshot{}, err
	}
	snapshot := s.webPoolSnapshotFromIndex(ctx, now, enabledNow, len(toDisable))
	return snapshot, nil
}

func (s *Service) webPoolSnapshotFromIndex(ctx context.Context, now time.Time, enabledIDs []uint64, enabledRemoved int) WebPoolSnapshot {
	s.initWebProbe()
	s.webProbeMu.Lock()
	imageIDs := s.dispatchIDsLocked(WebLaneImage, webImagePoolCap)
	chatIDs := s.dispatchIDsLocked(WebLaneChat, webChatPoolCap)
	pinNotIn := s.pinNotInDispatchLocked()
	diagnostics := s.webSelectionDiagnosticsLocked()
	pinIDs := s.imagePinIDsLocked()
	threePools, _ := s.SummarizeWebThreePoolsForSnapshot(ctx)
	fourPools, _ := s.SummarizeWebFourPoolsForSnapshot(ctx)
	s.webProbeMu.Unlock()
	imageDispatchPoolIDs, imageSchedulableIDs, _ := s.webImagePoolAccountIDs(ctx, now)
	return WebPoolSnapshot{
		ImagePoolIDs: imageIDs, ImageDispatchPoolIDs: imageDispatchPoolIDs, ImageSchedulableIDs: imageSchedulableIDs,
		ImagePinIDs: pinIDs,
		ChatPoolIDs: chatIDs, EnabledIDs: enabledIDs,
		ImagePoolSize: len(imageIDs), ChatPoolSize: len(chatIDs), EnabledCount: len(enabledIDs),
		ImagePoolCap: webImagePoolCap, ChatPoolCap: webChatPoolCap, ReconciledAt: now,
		EnabledAdded: 0, EnabledRemoved: enabledRemoved, ThreePools: threePools, FourPools: fourPools,
		PinNotInDispatch: pinNotIn, SelectionDiagnostics: diagnostics,
	}
}

func (s *Service) dispatchIDsLocked(lane WebLane, cap int) []uint64 {
	entries := s.laneIndexLocked(lane).dispatchIndex.Ascend(cap)
	ids := make([]uint64, 0, len(entries))
	for _, entry := range entries {
		ids = append(ids, entry.ID)
	}
	return ids
}

// WebPools 返回当前 dispatchIndex 投影（不改 enabled）。
func (s *Service) WebPools(ctx context.Context) (WebPoolSnapshot, error) {
	now := time.Now().UTC()
	s.ensureWebPoolIndexWarm(ctx)
	accounts, _, err := s.accounts.List(ctx, repository.AccountListQuery{
		Page:   repository.PageQuery{Offset: 0, Limit: 5000, Sort: repository.SortQuery{Field: "createdAt", Direction: repository.SortAscending}},
		Filter: repository.AccountListFilter{Provider: string(accountdomain.ProviderWeb), Now: now},
	})
	if err != nil {
		return WebPoolSnapshot{}, err
	}
	enabled := make([]uint64, 0)
	for _, value := range accounts {
		if value.Enabled {
			enabled = append(enabled, value.ID)
		}
	}
	sort.Slice(enabled, func(i, j int) bool { return enabled[i] < enabled[j] })
	return s.webPoolSnapshotFromIndex(ctx, now, enabled, 0), nil
}

func quotaRemaining(windows []accountdomain.QuotaWindow, mode string) int {
	for _, window := range windows {
		if window.Mode == mode {
			return window.Remaining
		}
	}
	return 0
}

func findQuotaWindow(windows []accountdomain.QuotaWindow, mode string) *accountdomain.QuotaWindow {
	for index := range windows {
		if windows[index].Mode == mode {
			return &windows[index]
		}
	}
	return nil
}

func findModelState(states []accountdomain.ModelState, upstreamModel string) *accountdomain.ModelState {
	for index := range states {
		if states[index].UpstreamModel == upstreamModel {
			return &states[index]
		}
	}
	return nil
}

func imagineQuotaFresh(window *accountdomain.QuotaWindow, now time.Time) bool {
	if window == nil || window.Mode != "imagine" {
		return false
	}
	if window.Source != accountdomain.QuotaSourceUpstream || window.Total <= 0 || window.Remaining <= 0 {
		return false
	}
	if window.SyncedAt == nil || now.Sub(*window.SyncedAt) > imagineQuotaFreshTTL {
		return false
	}
	return true
}

func webPoolCandidateFromContext(input WebPoolContext, now time.Time) webPoolCandidate {
	value := input.Credential
	cooling := value.CooldownUntil != nil && value.CooldownUntil.After(now)
	return webPoolCandidate{
		id: value.ID, priority: value.Priority,
		imagineWindow: input.ImagineWindow, modelState: input.ModelState,
		enabled: value.Enabled, active: value.AuthStatus == accountdomain.AuthStatusActive,
		cooling: cooling, imagineBlocked: input.ImagineBlocked,
	}
}

// imageDispatchAdmissible 与 gateway.candidateImagineQuotaAdmissible 对齐，并要求健康 modelState。
func imageDispatchAdmissible(candidate webPoolCandidate, now time.Time) bool {
	if !candidate.enabled || !candidate.active || candidate.cooling {
		return false
	}
	if candidate.imagineBlocked {
		positive := candidate.imagineWindow != nil && candidate.imagineWindow.Total > 0 && candidate.imagineWindow.Remaining > 0
		if !positive {
			return false
		}
	}
	if !imagineQuotaFresh(candidate.imagineWindow, now) {
		return false
	}
	if candidate.modelState == nil {
		return false
	}
	switch candidate.modelState.Status {
	case accountdomain.ModelStatusAvailable, accountdomain.ModelStatusQuotaAvailable:
		return true
	default:
		return false
	}
}

func imagePoolInVerification(candidate webPoolCandidate) bool {
	if candidate.modelState == nil {
		return true
	}
	switch candidate.modelState.Status {
	case accountdomain.ModelStatusUnknown, accountdomain.ModelStatusQuotaAvailable:
		return true
	default:
		return false
	}
}

func imageAccountRetained(input WebPoolContext, now time.Time) bool {
	pool := webImagePoolAt(input, now)
	return pool != "" && pool != WebPoolDelete
}

func filterImageDispatchIDsWithGenerations(ids []uint64, windowsByAccount map[uint64][]accountdomain.QuotaWindow, now time.Time) []uint64 {
	if len(ids) == 0 {
		return nil
	}
	out := make([]uint64, 0, len(ids))
	for _, id := range ids {
		imagine := findQuotaWindow(windowsByAccount[id], "imagine")
		if !imagineQuotaFresh(imagine, now) {
			continue
		}
		if gens, ok := accountdomain.ImagineGenerations(imagine.Remaining, imagine.Total); !ok || gens <= 0 {
			continue
		}
		out = append(out, id)
	}
	return out
}

func imagePoolEligible(candidate webPoolCandidate, now time.Time) bool {
	if !candidate.enabled || !candidate.active || candidate.cooling {
		return false
	}
	positiveQuota := candidate.imagineWindow != nil && candidate.imagineWindow.Total > 0 && candidate.imagineWindow.Remaining > 0
	if candidate.imagineBlocked && !positiveQuota {
		return false
	}
	if window := candidate.imagineWindow; window != nil && window.Total > 0 {
		if window.Remaining <= 0 {
			return false
		}
		// 新同步到的正额度能够解除旧的 quota_exhausted 结果。
		if candidate.modelState != nil && candidate.modelState.Status == accountdomain.ModelStatusQuotaExhausted {
			return imagineQuotaFresh(candidate.imagineWindow, now)
		}
	}
	if candidate.modelState == nil {
		return positiveQuota && imagineQuotaFresh(candidate.imagineWindow, now)
	}
	switch candidate.modelState.Status {
	case accountdomain.ModelStatusAuthFailed, accountdomain.ModelStatusSignatureFailed, accountdomain.ModelStatusQuotaExhausted:
		return false
	case accountdomain.ModelStatusSoftStop:
		if candidate.modelState.CooldownUntil != nil && candidate.modelState.CooldownUntil.After(now) {
			return false
		}
		return positiveQuota && imagineQuotaFresh(candidate.imagineWindow, now)
	case accountdomain.ModelStatusAvailable:
		if positiveQuota && imagineQuotaFresh(candidate.imagineWindow, now) {
			return true
		}
		if candidate.modelState.LastSuccessAt != nil &&
			now.Sub(*candidate.modelState.LastSuccessAt) <= imagineQuotaFreshTTL &&
			positiveQuota {
			return true
		}
		return false
	default:
		return positiveQuota && imagineQuotaFresh(candidate.imagineWindow, now)
	}
}

func imagePoolLess(a, b webPoolCandidate, now time.Time) bool {
	freshA := imagineQuotaFresh(a.imagineWindow, now)
	freshB := imagineQuotaFresh(b.imagineWindow, now)
	if freshA != freshB {
		return freshA
	}
	if a.priority != b.priority {
		return a.priority > b.priority
	}
	rankA, rankB := imagePoolRank(a, now), imagePoolRank(b, now)
	if rankA != rankB {
		return rankA > rankB
	}
	remainingA, remainingB := 0, 0
	if a.imagineWindow != nil {
		remainingA = a.imagineWindow.Remaining
	}
	if b.imagineWindow != nil {
		remainingB = b.imagineWindow.Remaining
	}
	if remainingA != remainingB {
		return remainingA > remainingB
	}
	return a.id < b.id
}

func imagePoolRank(candidate webPoolCandidate, now time.Time) int {
	if imagineQuotaFresh(candidate.imagineWindow, now) {
		if candidate.modelState != nil && candidate.modelState.Status == accountdomain.ModelStatusAvailable {
			return 3
		}
		return 2
	}
	if candidate.modelState != nil && candidate.modelState.Status == accountdomain.ModelStatusAvailable {
		return 2
	}
	if candidate.imagineWindow != nil && candidate.imagineWindow.Total > 0 && candidate.imagineWindow.Remaining > 0 {
		return 1
	}
	if candidate.modelState != nil && candidate.modelState.Status == accountdomain.ModelStatusQuotaAvailable {
		return 1
	}
	return 0
}

func selectWebPoolIDs(candidates []webPoolCandidate, cap int, eligible func(webPoolCandidate) bool, less func(a, b webPoolCandidate) bool) []uint64 {
	filtered := make([]webPoolCandidate, 0, len(candidates))
	for _, candidate := range candidates {
		if eligible(candidate) {
			filtered = append(filtered, candidate)
		}
	}
	sort.SliceStable(filtered, func(i, j int) bool { return less(filtered[i], filtered[j]) })
	if len(filtered) > cap {
		filtered = filtered[:cap]
	}
	ids := make([]uint64, 0, len(filtered))
	for _, candidate := range filtered {
		ids = append(ids, candidate.id)
	}
	return ids
}
