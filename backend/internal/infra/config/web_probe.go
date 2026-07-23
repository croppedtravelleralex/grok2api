package config

import (
	"os"
	"strconv"
	"strings"
	"time"
)

// WebProbeConfig 定义 Web 双轨探针调度与预算参数，可通过运行设置热更新。
type WebProbeConfig struct {
	DispatchInterval        Duration `yaml:"-"`
	IdleInterval            Duration `yaml:"-"`
	InitialDelay            Duration `yaml:"-"`
	LitePerAccountPerDay    int      `yaml:"-"`
	ChatPerAccountPerDay    int      `yaml:"-"`
	LiteGlobalPerHour       int      `yaml:"-"`
	DeadL2MinInterval       Duration `yaml:"-"`
	PipelineL1Threshold     float64  `yaml:"-"`
	PipelineL0OnlyThreshold float64  `yaml:"-"`
	ProbeUnknownQuota       bool     `yaml:"-"`
}

// DefaultWebProbeConfig 返回探针默认参数（调度间隔 30s，图轨 L2 日预算 1）。
func DefaultWebProbeConfig() WebProbeConfig {
	return WebProbeConfig{
		DispatchInterval:        Duration(30 * time.Second),
		IdleInterval:            Duration(5 * time.Minute),
		InitialDelay:            Duration(30 * time.Second),
		LitePerAccountPerDay:    1,
		ChatPerAccountPerDay:    3,
		LiteGlobalPerHour:       6,
		DeadL2MinInterval:       Duration(24 * time.Hour),
		PipelineL1Threshold:     0.7,
		PipelineL0OnlyThreshold: 0.9,
		ProbeUnknownQuota:       true,
	}
}

// NormalizeWebProbeConfig 为缺失字段回填默认值；DispatchInterval=0 表示关闭探针。
func NormalizeWebProbeConfig(cfg WebProbeConfig) WebProbeConfig {
	defaults := DefaultWebProbeConfig()
	if cfg.IdleInterval.Value() <= 0 {
		cfg.IdleInterval = defaults.IdleInterval
	}
	if cfg.InitialDelay.Value() <= 0 {
		cfg.InitialDelay = defaults.InitialDelay
	}
	if cfg.LitePerAccountPerDay <= 0 {
		cfg.LitePerAccountPerDay = defaults.LitePerAccountPerDay
	}
	if cfg.ChatPerAccountPerDay <= 0 {
		cfg.ChatPerAccountPerDay = defaults.ChatPerAccountPerDay
	}
	if cfg.LiteGlobalPerHour <= 0 {
		cfg.LiteGlobalPerHour = defaults.LiteGlobalPerHour
	}
	if cfg.DeadL2MinInterval.Value() <= 0 {
		cfg.DeadL2MinInterval = defaults.DeadL2MinInterval
	}
	if cfg.PipelineL1Threshold <= 0 {
		cfg.PipelineL1Threshold = defaults.PipelineL1Threshold
	}
	if cfg.PipelineL0OnlyThreshold <= 0 {
		cfg.PipelineL0OnlyThreshold = defaults.PipelineL0OnlyThreshold
	}
	if cfg.PipelineL0OnlyThreshold <= cfg.PipelineL1Threshold {
		cfg.PipelineL0OnlyThreshold = defaults.PipelineL0OnlyThreshold
	}
	return cfg
}

// ApplyWebProbeEnvOverrides 在加载持久化设置前，用环境变量覆盖代码默认值。
func ApplyWebProbeEnvOverrides(cfg *WebProbeConfig) {
	if cfg == nil {
		return
	}
	if value := strings.ToLower(strings.TrimSpace(os.Getenv("GROK2API_WEB_PROBE_EVERY"))); value != "" {
		if parsed := parseWebProbeEvery(value); parsed >= 0 {
			cfg.DispatchInterval = Duration(parsed)
		}
	}
	if value := strings.TrimSpace(os.Getenv("GROK2API_WEB_PROBE_IDLE_EVERY")); value != "" {
		if parsed := boundedEnvDurationValue(value, 0, time.Minute, 24*time.Hour); parsed > 0 {
			cfg.IdleInterval = Duration(parsed)
		}
	}
	if value := strings.TrimSpace(os.Getenv("GROK2API_WEB_PROBE_INITIAL_DELAY")); value != "" {
		if parsed := boundedEnvDurationValue(value, 0, 10*time.Second, 24*time.Hour); parsed > 0 {
			cfg.InitialDelay = Duration(parsed)
		}
	}
	if value := strings.TrimSpace(os.Getenv("WEB_PROBE_LITE_MAX_PER_ACCOUNT_PER_DAY")); value != "" {
		if parsed := boundedEnvIntValue(value, -1, 0, 100); parsed >= 0 {
			cfg.LitePerAccountPerDay = parsed
		}
	}
	if value := strings.TrimSpace(os.Getenv("WEB_PROBE_CHAT_MAX_PER_ACCOUNT_PER_DAY")); value != "" {
		if parsed := boundedEnvIntValue(value, -1, 0, 100); parsed >= 0 {
			cfg.ChatPerAccountPerDay = parsed
		}
	}
	if value := strings.TrimSpace(os.Getenv("WEB_PROBE_LITE_GLOBAL_PER_HOUR")); value != "" {
		if parsed := boundedEnvIntValue(value, -1, 0, 1000); parsed >= 0 {
			cfg.LiteGlobalPerHour = parsed
		}
	}
	if value := strings.TrimSpace(os.Getenv("WEB_PROBE_DEAD_L2_MIN_INTERVAL")); value != "" {
		if parsed := boundedEnvDurationValue(value, 0, time.Hour, 7*24*time.Hour); parsed > 0 {
			cfg.DeadL2MinInterval = Duration(parsed)
		}
	}
}

func parseWebProbeEvery(value string) time.Duration {
	switch value {
	case "0", "off", "disabled":
		return 0
	}
	parsed, err := time.ParseDuration(value)
	if err != nil || parsed < 15*time.Second || parsed > 24*time.Hour {
		return 30 * time.Second
	}
	return parsed
}

func boundedEnvDurationValue(value string, fallback time.Duration, min, max time.Duration) time.Duration {
	parsed, err := time.ParseDuration(value)
	if err != nil || parsed < min || parsed > max {
		return fallback
	}
	return parsed
}

func boundedEnvIntValue(value string, fallback, min, max int) int {
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed < min || parsed > max {
		return fallback
	}
	return parsed
}
