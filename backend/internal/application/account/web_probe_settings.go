package account

import (
	"time"

	"github.com/chenyme/grok2api/backend/internal/infra/config"
)

func (s *Service) ApplyWebProbeConfig(cfg config.WebProbeConfig) {
	s.initWebProbe()
	normalized := config.NormalizeWebProbeConfig(cfg)
	s.webProbeCfgMu.Lock()
	s.webProbeCfg = normalized
	s.webProbeCfgMu.Unlock()
	s.webProbe.configure(s.now(), normalized.DispatchInterval.Value(), normalized.IdleInterval.Value(), normalized.InitialDelay.Value())
	s.webProbeBudget.applyConfig(normalized)
}

func (s *Service) WebProbeTimings() (interval, idleInterval, initialDelay time.Duration) {
	s.webProbeCfgMu.RLock()
	cfg := config.NormalizeWebProbeConfig(s.webProbeCfg)
	s.webProbeCfgMu.RUnlock()
	return cfg.DispatchInterval.Value(), cfg.IdleInterval.Value(), cfg.InitialDelay.Value()
}

func (s *Service) webProbeConfigLocked() config.WebProbeConfig {
	s.webProbeCfgMu.RLock()
	defer s.webProbeCfgMu.RUnlock()
	if s.webProbeCfg.DispatchInterval == 0 && s.webProbeCfg.IdleInterval == 0 {
		return config.DefaultWebProbeConfig()
	}
	return s.webProbeCfg
}

func (s *Service) probeUnknownQuotaEnabled() bool {
	return s.webProbeConfigLocked().ProbeUnknownQuota
}

func (s *Service) webProbeEffectiveConfig() WebProbeEffectiveConfig {
	cfg := s.webProbeConfigLocked()
	return WebProbeEffectiveConfig{
		ProbeUnknownQuota:       cfg.ProbeUnknownQuota,
		LitePerAccountPerDay:    cfg.LitePerAccountPerDay,
		ChatPerAccountPerDay:    cfg.ChatPerAccountPerDay,
		LiteGlobalPerHour:       cfg.LiteGlobalPerHour,
		DeadL2MinInterval:       cfg.DeadL2MinInterval.Value(),
		PipelineL1Threshold:     cfg.PipelineL1Threshold,
		PipelineL0OnlyThreshold: cfg.PipelineL0OnlyThreshold,
	}
}
