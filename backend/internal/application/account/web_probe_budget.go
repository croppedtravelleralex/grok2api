package account

import (
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

// WebProbeLevel 探针请求分级。
type WebProbeLevel int

const (
	WebProbeL0 WebProbeLevel = iota
	WebProbeL1
	WebProbeL2
)

type webProbeBudgetGovernor struct {
	mu sync.Mutex

	litePerAccountPerDay  int
	chatPerAccountPerDay  int
	liteGlobalPerHour     int
	deadL2MinInterval     time.Duration

	accountLite  map[string]int // lane:accountID -> count today
	accountChat  map[string]int
	globalLite   []time.Time
	deadLastL2   map[string]time.Time
	windowDate   string

	pipelineOccupancy func() (active, total int)
}

func newWebProbeBudgetGovernor() *webProbeBudgetGovernor {
	return &webProbeBudgetGovernor{
		litePerAccountPerDay: envIntDefault("WEB_PROBE_LITE_MAX_PER_ACCOUNT_PER_DAY", 1),
		chatPerAccountPerDay: envIntDefault("WEB_PROBE_CHAT_MAX_PER_ACCOUNT_PER_DAY", 3),
		liteGlobalPerHour:    envIntDefault("WEB_PROBE_LITE_GLOBAL_PER_HOUR", 6),
		deadL2MinInterval:    envDurationDefault("WEB_PROBE_DEAD_L2_MIN_INTERVAL", 24*time.Hour),
		accountLite:          make(map[string]int),
		accountChat:          make(map[string]int),
		deadLastL2:           make(map[string]time.Time),
	}
}

func (s *Service) SetWebProbePipelineOccupancy(fn func() (active, total int)) {
	s.initWebProbe()
	s.webProbeBudget.mu.Lock()
	s.webProbeBudget.pipelineOccupancy = fn
	s.webProbeBudget.mu.Unlock()
}

func (g *webProbeBudgetGovernor) rotateDay(now time.Time) {
	day := now.UTC().Format("2006-01-02")
	if g.windowDate == day {
		return
	}
	g.windowDate = day
	g.accountLite = make(map[string]int)
	g.accountChat = make(map[string]int)
}

func (g *webProbeBudgetGovernor) pipelineLoad() float64 {
	if g.pipelineOccupancy == nil {
		return 0
	}
	active, total := g.pipelineOccupancy()
	if total <= 0 {
		return 0
	}
	return float64(active) / float64(total)
}

func (g *webProbeBudgetGovernor) maxLevel(now time.Time) WebProbeLevel {
	load := g.pipelineLoad()
	switch {
	case load >= 0.9:
		return WebProbeL0
	case load >= 0.7:
		return WebProbeL1
	default:
		return WebProbeL2
	}
}

func (g *webProbeBudgetGovernor) allow(now time.Time, lane WebLane, accountID uint64, level WebProbeLevel, deadLane bool) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.rotateDay(now)
	if level > g.maxLevel(now) {
		return false
	}
	key := budgetKey(lane, accountID)
	switch level {
	case WebProbeL0:
		return true
	case WebProbeL1:
		if lane != WebLaneChat {
			return false
		}
		return g.accountChat[key] < g.chatPerAccountPerDay
	case WebProbeL2:
		if lane != WebLaneImage {
			return false
		}
		if deadLane {
			last, ok := g.deadLastL2[key]
			if ok && now.Sub(last) < g.deadL2MinInterval {
				return false
			}
		}
		if g.accountLite[key] >= g.litePerAccountPerDay {
			return false
		}
		cutoff := now.Add(-time.Hour)
		active := 0
		for _, at := range g.globalLite {
			if at.After(cutoff) {
				active++
			}
		}
		return active < g.liteGlobalPerHour
	default:
		return false
	}
}

func (g *webProbeBudgetGovernor) record(now time.Time, lane WebLane, accountID uint64, level WebProbeLevel) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.rotateDay(now)
	key := budgetKey(lane, accountID)
	switch level {
	case WebProbeL1:
		g.accountChat[key]++
	case WebProbeL2:
		g.accountLite[key]++
		g.globalLite = append(g.globalLite, now)
		g.deadLastL2[key] = now
	}
}

func (g *webProbeBudgetGovernor) snapshot(now time.Time) WebProbeBudgetSnapshot {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.rotateDay(now)
	cutoff := now.Add(-time.Hour)
	globalLite := 0
	for _, at := range g.globalLite {
		if at.After(cutoff) {
			globalLite++
		}
	}
	load := g.pipelineLoad()
	maxLevel := g.maxLevel(now)
	return WebProbeBudgetSnapshot{
		LitePerAccountPerDay:  g.litePerAccountPerDay,
		ChatPerAccountPerDay:  g.chatPerAccountPerDay,
		LiteGlobalPerHour:     g.liteGlobalPerHour,
		LiteGlobalUsedHour:    globalLite,
		PipelineActiveSlots:   pipelineActive(g),
		PipelineTotalSlots:    pipelineTotal(g),
		PipelineLoadPercent:   int(load * 100),
		MaxProbeLevel:         webProbeLevelName(maxLevel),
	}
}

func pipelineActive(g *webProbeBudgetGovernor) int {
	if g.pipelineOccupancy == nil {
		return 0
	}
	active, _ := g.pipelineOccupancy()
	return active
}

func pipelineTotal(g *webProbeBudgetGovernor) int {
	if g.pipelineOccupancy == nil {
		return 0
	}
	_, total := g.pipelineOccupancy()
	return total
}

func budgetKey(lane WebLane, accountID uint64) string {
	return string(lane) + ":" + strconv.FormatUint(accountID, 10)
}

func envIntDefault(name string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed < 0 {
		return fallback
	}
	return parsed
}

func envDurationDefault(name string, fallback time.Duration) time.Duration {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	parsed, err := time.ParseDuration(value)
	if err != nil || parsed <= 0 {
		return fallback
	}
	return parsed
}

func webProbeLevelName(level WebProbeLevel) string {
	switch level {
	case WebProbeL0:
		return "L0"
	case WebProbeL1:
		return "L1"
	case WebProbeL2:
		return "L2"
	default:
		return "unknown"
	}
}

type WebProbeBudgetSnapshot struct {
	LitePerAccountPerDay int    `json:"litePerAccountPerDay"`
	ChatPerAccountPerDay int    `json:"chatPerAccountPerDay"`
	LiteGlobalPerHour    int    `json:"liteGlobalPerHour"`
	LiteGlobalUsedHour   int    `json:"liteGlobalUsedHour"`
	PipelineActiveSlots  int    `json:"pipelineActiveSlots"`
	PipelineTotalSlots   int    `json:"pipelineTotalSlots"`
	PipelineLoadPercent  int    `json:"pipelineLoadPercent"`
	MaxProbeLevel        string `json:"maxProbeLevel"`
}
