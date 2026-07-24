package account

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/chenyme/grok2api/backend/internal/application/account/poolindex"
	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
	"github.com/chenyme/grok2api/backend/internal/repository"
)

const (
	WebPoolDispatch = "dispatch"
	WebPoolRecovery = "recovery"
	WebPoolDead     = "dead"
	webDeadPrefix   = "web_dead:"
)

// WebLane 调度/维护双轨。
type WebLane string

const (
	WebLaneImage WebLane = "image"
	WebLaneChat  WebLane = "chat"
)

type webLaneIndex struct {
	dispatchIndex     *poolindex.DispatchIndex
	dispatchProbeHeap *poolindex.DueHeap
	recoveryVerify    *poolindex.DueHeap
	recoveryCooldown  *poolindex.DueHeap
	deadHeap          *poolindex.DueHeap
	maintenanceDRR    *poolindex.WebDRRScheduler
}

// WebPoolContext 双轨三池谓词输入。
type WebPoolContext struct {
	Credential     accountdomain.Credential
	FastRem        int
	AutoRem        int
	ImagineWindow  *accountdomain.QuotaWindow
	ModelState     *accountdomain.ModelState
	ImagineBlocked bool
}

type WebLanePoolCountsPublic struct {
	Dispatch int `json:"dispatch"`
	Recovery int `json:"recovery"`
	Dead     int `json:"dead"`
}

type WebThreePoolsPublic struct {
	Image WebLanePoolCountsPublic `json:"image"`
	Chat  WebLanePoolCountsPublic `json:"chat"`
}

func ResolveWebAcquireLane(upstreamModel, quotaMode string) WebLane {
	mode := strings.TrimSpace(quotaMode)
	model := strings.TrimSpace(upstreamModel)
	if mode == "imagine" || strings.HasPrefix(model, "grok-imagine") {
		return WebLaneImage
	}
	return WebLaneChat
}

func buildWebPoolContext(value accountdomain.Credential, windows []accountdomain.QuotaWindow, states []accountdomain.ModelState, imagineBlocked bool, now time.Time) WebPoolContext {
	accountCooling := value.CooldownUntil != nil && value.CooldownUntil.After(now)
	_ = accountCooling
	return WebPoolContext{
		Credential:     value,
		FastRem:        quotaRemaining(windows, "fast"),
		AutoRem:        quotaRemaining(windows, "auto"),
		ImagineWindow:  findQuotaWindow(windows, "imagine"),
		ModelState:     findModelState(states, imagineUpstream),
		ImagineBlocked: imagineBlocked,
	}
}

// WebPoolAt 返回双轨三池之一：dispatch / recovery / dead；未入索引返回空串。
func WebPoolAt(lane WebLane, input WebPoolContext, now time.Time) string {
	value := input.Credential
	errText := strings.ToLower(strings.TrimSpace(value.LastError))
	if strings.HasPrefix(errText, webDeadPrefix) {
		return WebPoolDead
	}
	if lane == WebLaneImage {
		if value.AuthStatus == accountdomain.AuthStatusReauthRequired {
			return WebPoolDead
		}
		if input.ModelState != nil && input.ModelState.Status == accountdomain.ModelStatusSignatureFailed {
			return WebPoolDead
		}
	} else if lane == WebLaneChat {
		if value.AuthStatus == accountdomain.AuthStatusReauthRequired {
			return WebPoolDead
		}
		if input.ModelState != nil && input.ModelState.Status == accountdomain.ModelStatusAuthFailed {
			return WebPoolDead
		}
	}
	if !value.Enabled || value.AuthStatus != accountdomain.AuthStatusActive {
		return ""
	}
	if webLaneInRecovery(lane, input, now) {
		return WebPoolRecovery
	}
	if webLaneInDispatch(lane, input, now) {
		return WebPoolDispatch
	}
	return WebPoolRecovery
}

func webLaneInRecovery(lane WebLane, input WebPoolContext, now time.Time) bool {
	value := input.Credential
	if value.CooldownUntil != nil && value.CooldownUntil.After(now) {
		return true
	}
	switch lane {
	case WebLaneImage:
		if input.ImagineBlocked {
			positive := input.ImagineWindow != nil && input.ImagineWindow.Total > 0 && input.ImagineWindow.Remaining > 0
			if !positive {
				return true
			}
		}
		if window := input.ImagineWindow; window != nil && window.Total > 0 && window.Remaining <= 0 {
			if input.ModelState == nil || input.ModelState.Status != accountdomain.ModelStatusAvailable {
				return true
			}
		}
		if input.ModelState != nil {
			switch input.ModelState.Status {
			case accountdomain.ModelStatusSoftStop:
				if input.ModelState.CooldownUntil == nil || input.ModelState.CooldownUntil.After(now) {
					return true
				}
			case accountdomain.ModelStatusQuotaExhausted:
				positive := input.ImagineWindow != nil && input.ImagineWindow.Total > 0 && input.ImagineWindow.Remaining > 0
				if !positive {
					return true
				}
			case accountdomain.ModelStatusAuthFailed, accountdomain.ModelStatusSignatureFailed:
				return true
			}
		}
		if input.ModelState == nil && input.ImagineWindow != nil && input.ImagineWindow.Total == 0 && input.ImagineWindow.Remaining == 0 {
			return true
		}
	case WebLaneChat:
		if input.FastRem <= 0 && input.AutoRem <= 0 {
			return true
		}
	}
	return false
}

func webLaneInDispatch(lane WebLane, input WebPoolContext, now time.Time) bool {
	switch lane {
	case WebLaneImage:
		return imagePoolEligible(webPoolCandidate{
			id: input.Credential.ID, priority: input.Credential.Priority,
			imagineWindow: input.ImagineWindow, modelState: input.ModelState,
			enabled: true, active: true, cooling: false, imagineBlocked: input.ImagineBlocked,
		}, now)
	case WebLaneChat:
		return input.FastRem > 0 || input.AutoRem > 0
	default:
		return false
	}
}

func webRecoverySubLane(lane WebLane, input WebPoolContext, now time.Time) poolindex.WebMaintenanceLane {
	if lane == WebLaneImage {
		if input.ModelState == nil || (input.ModelState.Status != accountdomain.ModelStatusAvailable && input.ModelState.LastSuccessAt == nil) {
			return poolindex.WebLaneRecoveryVerify
		}
	} else if input.FastRem <= 0 && input.AutoRem <= 0 {
		return poolindex.WebLaneRecoveryVerify
	}
	if input.Credential.CooldownUntil != nil && input.Credential.CooldownUntil.After(now) {
		return poolindex.WebLaneRecoveryCooldown
	}
	if input.ModelState != nil && input.ModelState.Status == accountdomain.ModelStatusSoftStop {
		return poolindex.WebLaneRecoveryCooldown
	}
	return poolindex.WebLaneRecoveryVerify
}

func (s *Service) initWebProbe() {
	s.webProbeMu.Lock()
	defer s.webProbeMu.Unlock()
	if s.webProbe != nil {
		return
	}
	s.webProbe = &webProbeMonitor{}
	s.webProbeBudget = newWebProbeBudgetGovernor()
	s.webImageLane = newWebLaneIndex()
	s.webChatLane = newWebLaneIndex()
}

func newWebLaneIndex() webLaneIndex {
	return webLaneIndex{
		dispatchIndex: poolindex.NewDispatchIndex(), dispatchProbeHeap: poolindex.NewDueHeap(),
		recoveryVerify: poolindex.NewDueHeap(), recoveryCooldown: poolindex.NewDueHeap(),
		deadHeap: poolindex.NewDueHeap(), maintenanceDRR: poolindex.NewWebDRRScheduler(),
	}
}

func (s *Service) laneIndex(lane WebLane) *webLaneIndex {
	s.initWebProbe()
	if lane == WebLaneChat {
		return &s.webChatLane
	}
	return &s.webImageLane
}

func (s *Service) ensureWebPoolIndexWarm(ctx context.Context) {
	s.initWebProbe()
	s.webProbeMu.Lock()
	empty := s.webImageLane.dispatchIndex.Len()+s.webChatLane.dispatchIndex.Len() == 0
	s.webProbeMu.Unlock()
	if empty {
		_ = s.RebuildWebPoolIndex(ctx)
	}
}

// EnsureWebPoolIndexWarm Selector 热路径兜底。
func (s *Service) EnsureWebPoolIndexWarm(ctx context.Context) {
	s.ensureWebPoolIndexWarm(ctx)
}

// RebuildWebPoolIndex 从数据库重建双轨三池索引。
func (s *Service) RebuildWebPoolIndex(ctx context.Context) error {
	s.initWebProbe()
	values, _, err := s.accounts.List(ctx, repository.AccountListQuery{
		Page:   repository.PageQuery{Limit: maxCredentialExportAccounts},
		Filter: repository.AccountListFilter{Provider: string(accountdomain.ProviderWeb), Now: s.now()},
	})
	if err != nil {
		return mapRepositoryError(err)
	}
	ids := make([]uint64, 0, len(values))
	for _, value := range values {
		ids = append(ids, value.ID)
	}
	windowsByAccount, err := s.accounts.GetQuotaWindows(ctx, ids)
	if err != nil {
		return mapRepositoryError(err)
	}
	blocks, err := s.accounts.GetActiveModelQuotaBlocks(ctx, ids, imagineUpstream, s.now())
	if err != nil {
		return mapRepositoryError(err)
	}
	modelStates, err := s.accounts.GetModelStates(ctx, ids)
	if err != nil {
		return mapRepositoryError(err)
	}
	s.webProbeMu.Lock()
	s.webImageLane = newWebLaneIndex()
	s.webChatLane = newWebLaneIndex()
	s.refreshWebRoutePinSets(ctx)
	now := s.now()
	for _, value := range values {
		ctxInput := buildWebPoolContext(value, windowsByAccount[value.ID], modelStates[value.ID], blocks[value.ID], now)
		s.indexWebAccountLocked(WebLaneImage, value, ctxInput, now)
		s.indexWebAccountLocked(WebLaneChat, value, ctxInput, now)
	}
	s.webProbeMu.Unlock()
	return nil
}

func (s *Service) indexWebAccountLocked(lane WebLane, value accountdomain.Credential, input WebPoolContext, now time.Time) {
	idx := s.laneIndexLocked(lane)
	idx.recoveryVerify.Remove(value.ID)
	idx.recoveryCooldown.Remove(value.ID)
	idx.deadHeap.Remove(value.ID)
	idx.dispatchProbeHeap.Remove(value.ID)
	idx.dispatchIndex.Remove(value.ID)
	pool := WebPoolAt(lane, input, now)
	if pool == "" {
		return
	}
	due := now
	if value.CooldownUntil != nil && value.CooldownUntil.After(now) {
		due = *value.CooldownUntil
	}
	switch pool {
	case WebPoolRecovery:
		sub := webRecoverySubLane(lane, input, now)
		if sub == poolindex.WebLaneRecoveryCooldown {
			idx.recoveryCooldown.Upsert(value.ID, due)
		} else {
			idx.recoveryVerify.Upsert(value.ID, value.CreatedAt)
		}
	case WebPoolDead:
		idx.deadHeap.Upsert(value.ID, value.UpdatedAt)
	case WebPoolDispatch:
		if !s.webLanePinAllowedLocked(lane, value.ID) {
			return
		}
		lastSelected := time.Time{}
		if value.LastUsedAt != nil {
			lastSelected = *value.LastUsedAt
		}
		known, remaining := webDispatchQuota(lane, input)
		idx.dispatchIndex.Upsert(poolindex.DispatchEntry{
			ID: value.ID, Priority: value.Priority, QuotaKnown: known, QuotaRemaining: remaining, LastSelectedAt: lastSelected,
		})
		probeAt := value.UpdatedAt
		if probeAt.IsZero() {
			probeAt = now.Add(-time.Hour)
		}
		idx.dispatchProbeHeap.Upsert(value.ID, probeAt)
	}
}

func (s *Service) laneIndexLocked(lane WebLane) *webLaneIndex {
	if lane == WebLaneChat {
		return &s.webChatLane
	}
	return &s.webImageLane
}

func webDispatchQuota(lane WebLane, input WebPoolContext) (known bool, remaining float64) {
	switch lane {
	case WebLaneImage:
		if window := input.ImagineWindow; window != nil && window.Total > 0 {
			return true, float64(window.Remaining)
		}
		return false, 0
	case WebLaneChat:
		if input.FastRem > 0 || input.AutoRem > 0 {
			return true, float64(input.FastRem + input.AutoRem)
		}
		return false, 0
	default:
		return false, 0
	}
}

// SyncWebAccountIndex 在账号状态、额度或 pin 变更后增量同步双轨调度索引。
func (s *Service) SyncWebAccountIndex(ctx context.Context, id uint64) {
	s.syncWebAccountIndex(ctx, id)
}

func (s *Service) syncWebAccountIndex(ctx context.Context, id uint64) {
	s.initWebProbe()
	value, err := s.accounts.Get(ctx, id)
	if err != nil {
		s.webProbeMu.Lock()
		for _, lane := range []WebLane{WebLaneImage, WebLaneChat} {
			idx := s.laneIndexLocked(lane)
			idx.recoveryVerify.Remove(id)
			idx.recoveryCooldown.Remove(id)
			idx.deadHeap.Remove(id)
			idx.dispatchProbeHeap.Remove(id)
			idx.dispatchIndex.Remove(id)
		}
		s.webProbeMu.Unlock()
		return
	}
	windows, _ := s.accounts.GetQuotaWindows(ctx, []uint64{id})
	blocks, _ := s.accounts.GetActiveModelQuotaBlocks(ctx, []uint64{id}, imagineUpstream, s.now())
	modelStates, _ := s.accounts.GetModelStates(ctx, []uint64{id})
	now := s.now()
	ctxInput := buildWebPoolContext(value, windows[id], modelStates[id], blocks[id], now)
	s.webProbeMu.Lock()
	s.indexWebAccountLocked(WebLaneImage, value, ctxInput, now)
	s.indexWebAccountLocked(WebLaneChat, value, ctxInput, now)
	s.webProbeMu.Unlock()
}

// OrderedWebDispatchIDs 返回指定轨调度池有序 ID。
func (s *Service) OrderedWebDispatchIDs(lane WebLane, limit int) []uint64 {
	s.initWebProbe()
	s.webProbeMu.Lock()
	defer s.webProbeMu.Unlock()
	entries := s.laneIndexLocked(lane).dispatchIndex.Ascend(limit)
	ids := make([]uint64, 0, len(entries))
	for _, entry := range entries {
		ids = append(ids, entry.ID)
	}
	return ids
}

// NoteWebDispatchSelected 成功租约后更新调度公平序。
func (s *Service) NoteWebDispatchSelected(lane WebLane, id uint64, at time.Time) {
	s.initWebProbe()
	s.webProbeMu.Lock()
	defer s.webProbeMu.Unlock()
	s.laneIndexLocked(lane).dispatchIndex.TouchSelected(id, at)
}

func (s *Service) nextWebProbeLane() WebLane {
	s.webProbeMu.Lock()
	defer s.webProbeMu.Unlock()
	if s.webProbeLaneCursor%2 == 0 {
		s.webProbeLaneCursor++
		return WebLaneImage
	}
	s.webProbeLaneCursor++
	return WebLaneChat
}

// WebDispatchProbeTick 调度探针：默认 L0；额度未知且开启 probeUnknownQuota 时可升 L2。
func (s *Service) WebDispatchProbeTick(ctx context.Context) (uint64, bool, error) {
	s.ensureWebPoolIndexWarm(ctx)
	lane := s.nextWebProbeLane()
	now := s.now()
	s.webProbeMu.Lock()
	idx := s.laneIndexLocked(lane)
	id, ok := idx.dispatchProbeHeap.PopDue(now)
	if !ok {
		id, ok = idx.dispatchProbeHeap.PopAny()
	}
	s.webProbeMu.Unlock()
	if !ok {
		return 0, false, nil
	}
	candidate, err := s.accounts.Get(ctx, id)
	if err != nil {
		s.syncWebAccountIndex(ctx, id)
		return 0, false, mapRepositoryError(err)
	}
	windows, _ := s.accounts.GetQuotaWindows(ctx, []uint64{id})
	blocks, _ := s.accounts.GetActiveModelQuotaBlocks(ctx, []uint64{id}, imagineUpstream, now)
	modelStates, _ := s.accounts.GetModelStates(ctx, []uint64{id})
	ctxInput := buildWebPoolContext(candidate, windows[id], modelStates[id], blocks[id], now)
	if WebPoolAt(lane, ctxInput, now) != WebPoolDispatch {
		s.syncWebAccountIndex(ctx, id)
		return id, true, nil
	}
	return s.observeWebProbe(ctx, candidate, lane, WebProbeModeDispatch, func() (uint64, bool, error) {
		return s.runWebDispatchProbe(ctx, candidate, lane)
	})
}

func (s *Service) runWebDispatchProbe(ctx context.Context, candidate accountdomain.Credential, lane WebLane) (uint64, bool, error) {
	now := s.now()
	level := WebProbeL0
	if lane == WebLaneImage && s.probeUnknownQuotaEnabled() {
		windows, _ := s.accounts.GetQuotaWindows(ctx, []uint64{candidate.ID})
		modelStates, _ := s.accounts.GetModelStates(ctx, []uint64{candidate.ID})
		blocks, _ := s.accounts.GetActiveModelQuotaBlocks(ctx, []uint64{candidate.ID}, imagineUpstream, now)
		ctxInput := buildWebPoolContext(candidate, windows[candidate.ID], modelStates[candidate.ID], blocks[candidate.ID], now)
		if imagineNeedsL2Probe(ctxInput) && s.webProbeBudget.allow(now, lane, candidate.ID, WebProbeL2, false) {
			level = WebProbeL2
		}
	}
	var probeErr error
	switch level {
	case WebProbeL0:
		probeErr = s.probeWebQuotaL0(ctx, candidate, lane)
	case WebProbeL2:
		probeErr = s.probeWebLiteL2(ctx, candidate)
	}
	if probeErr != nil {
		until := now.Add(15 * time.Minute)
		_ = s.accounts.UpdateHealth(ctx, candidate.ID, candidate.FailureCount+1, &until, "web dispatch probe: "+probeErr.Error(), true)
		s.syncWebAccountIndex(ctx, candidate.ID)
		return candidate.ID, true, probeErr
	}
	if level == WebProbeL2 {
		s.webProbeBudget.record(now, lane, candidate.ID, WebProbeL2)
	}
	_ = s.accounts.UpdateHealth(ctx, candidate.ID, 0, nil, "", true)
	s.syncWebAccountIndex(ctx, candidate.ID)
	s.webProbeMu.Lock()
	s.laneIndexLocked(lane).dispatchProbeHeap.Upsert(candidate.ID, s.now())
	s.webProbeMu.Unlock()
	return candidate.ID, true, nil
}

func imagineNeedsL2Probe(input WebPoolContext) bool {
	if input.ModelState != nil && input.ModelState.Status == accountdomain.ModelStatusAvailable {
		return false
	}
	if input.ModelState == nil {
		return true
	}
	switch input.ModelState.Status {
	case accountdomain.ModelStatusUnknown, accountdomain.ModelStatusQuotaAvailable:
		return true
	default:
		return false
	}
}

// WebMaintenanceProbeTick 维护探针：DRR + L0/L1/L2。
func (s *Service) WebMaintenanceProbeTick(ctx context.Context) (uint64, bool, error) {
	s.ensureWebPoolIndexWarm(ctx)
	lane := s.nextWebProbeLane()
	now := s.now()
	s.webProbeMu.Lock()
	idx := s.laneIndexLocked(lane)
	_, verifyReady := idx.recoveryVerify.PeekDue(now)
	_, cooldownReady := idx.recoveryCooldown.PeekDue(now)
	deadReady := idx.deadHeap.Len() > 0
	hasWork := [3]bool{verifyReady, cooldownReady, deadReady}
	maintenanceLane, ok := idx.maintenanceDRR.Next(hasWork)
	var id uint64
	if ok {
		switch maintenanceLane {
		case poolindex.WebLaneRecoveryVerify:
			id, ok = idx.recoveryVerify.PopDue(now)
			if !ok {
				id, ok = idx.recoveryVerify.PopAny()
			}
		case poolindex.WebLaneRecoveryCooldown:
			id, ok = idx.recoveryCooldown.PopDue(now)
			if !ok {
				id, ok = idx.recoveryCooldown.PopAny()
			}
		case poolindex.WebLaneDead:
			id, ok = idx.deadHeap.PopAny()
		}
	}
	s.webProbeMu.Unlock()
	if !ok || id == 0 {
		return 0, false, nil
	}
	candidate, err := s.accounts.Get(ctx, id)
	if err != nil {
		s.syncWebAccountIndex(ctx, id)
		return 0, false, mapRepositoryError(err)
	}
	mode := webMaintenanceMode(maintenanceLane)
	return s.observeWebProbe(ctx, candidate, lane, mode, func() (uint64, bool, error) {
		return s.runWebMaintenanceProbe(ctx, candidate, lane, maintenanceLane)
	})
}

func webMaintenanceMode(lane poolindex.WebMaintenanceLane) WebProbeMode {
	switch lane {
	case poolindex.WebLaneRecoveryVerify:
		return WebProbeModeRecoveryVerify
	case poolindex.WebLaneRecoveryCooldown:
		return WebProbeModeRecoveryCooldown
	default:
		return WebProbeModeDead
	}
}

func (s *Service) runWebMaintenanceProbe(ctx context.Context, candidate accountdomain.Credential, lane WebLane, maintenanceLane poolindex.WebMaintenanceLane) (uint64, bool, error) {
	now := s.now()
	deadLane := maintenanceLane == poolindex.WebLaneDead
	level := s.chooseWebProbeLevel(lane, maintenanceLane)
	if !s.webProbeBudget.allow(now, lane, candidate.ID, level, deadLane) {
		level = WebProbeL0
	}
	ready, err := s.ensureCredential(ctx, candidate, false, false, false)
	if err != nil {
		s.syncWebAccountIndex(ctx, candidate.ID)
		return candidate.ID, true, err
	}
	var probeErr error
	switch level {
	case WebProbeL0:
		probeErr = s.probeWebQuotaL0(ctx, ready, lane)
	case WebProbeL1:
		probeErr = s.probeWebChatL1(ctx, ready)
	case WebProbeL2:
		probeErr = s.probeWebLiteL2(ctx, ready)
	}
	if probeErr == nil {
		s.webProbeBudget.record(now, lane, candidate.ID, level)
		_ = s.accounts.UpdateHealth(ctx, ready.ID, 0, nil, "", true)
	} else if maintenanceLane == poolindex.WebLaneRecoveryCooldown {
		until := now.Add(15 * time.Minute)
		_ = s.accounts.UpdateHealth(ctx, ready.ID, ready.FailureCount+1, &until, probeErr.Error(), true)
	}
	s.syncWebAccountIndex(ctx, ready.ID)
	return ready.ID, true, probeErr
}

func (s *Service) chooseWebProbeLevel(lane WebLane, maintenanceLane poolindex.WebMaintenanceLane) WebProbeLevel {
	switch maintenanceLane {
	case poolindex.WebLaneRecoveryVerify:
		if lane == WebLaneImage {
			return WebProbeL2
		}
		return WebProbeL1
	case poolindex.WebLaneRecoveryCooldown:
		return WebProbeL0
	case poolindex.WebLaneDead:
		return WebProbeL2
	default:
		return WebProbeL0
	}
}

func (s *Service) markWebDead(ctx context.Context, id uint64, reason string) error {
	value, err := s.accounts.Get(ctx, id)
	if err != nil {
		return mapRepositoryError(err)
	}
	reason = strings.TrimSpace(reason)
	if reason == "" {
		reason = "marked web dead"
	}
	value.Enabled = false
	value.LastError = webDeadPrefix + " " + reason
	if len(value.LastError) > 512 {
		value.LastError = value.LastError[:512]
	}
	if _, err := s.accounts.Update(ctx, value); err != nil {
		return mapRepositoryError(err)
	}
	if s.sticky != nil {
		_ = s.sticky.DeleteByAccount(ctx, id)
	}
	s.syncWebAccountIndex(ctx, id)
	return nil
}

// SummarizeWebThreePoolsForSnapshot 为 WebPools API 填充六池计数。
func (s *Service) SummarizeWebThreePoolsForSnapshot(ctx context.Context) (WebThreePoolsPublic, error) {
	summary, err := s.summarizeWebThreePools(ctx)
	if err != nil {
		return WebThreePoolsPublic{}, err
	}
	return WebThreePoolsPublic{
		Image: WebLanePoolCountsPublic{Dispatch: int(summary.Image.Dispatch), Recovery: int(summary.Image.Recovery), Dead: int(summary.Image.Dead)},
		Chat:  WebLanePoolCountsPublic{Dispatch: int(summary.Chat.Dispatch), Recovery: int(summary.Chat.Recovery), Dead: int(summary.Chat.Dead)},
	}, nil
}

func (s *Service) webPoolIndexStats() string {
	s.initWebProbe()
	s.webProbeMu.Lock()
	defer s.webProbeMu.Unlock()
	return fmt.Sprintf("image_dispatch=%d chat_dispatch=%d", s.webImageLane.dispatchIndex.Len(), s.webChatLane.dispatchIndex.Len())
}
