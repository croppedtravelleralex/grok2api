package chrometicket

import "time"

const (
	StatusAvailable = "available"
	StatusConsumed  = "consumed"
	StatusExpired   = "expired"
)

// Ticket 表示本机 Chrome 预捕获的长效 statsig_meta 资产。
type Ticket struct {
	ID           string
	AccountID    uint64
	StatsigMeta  string
	DeviceCookie string
	UserAgent    string
	SignSource   string
	CreatedAt    time.Time
	ExpiresAt    time.Time
	ConsumedAt   *time.Time
	Status       string
}

// PushInput 是入池请求载荷。
type PushInput struct {
	AccountID    uint64
	StatsigMeta  string
	DeviceCookie string
	UserAgent    string
	SignSource   string
	TTL          time.Duration
}

// Stats 汇总票池状态。
type Stats struct {
	ByStatus           map[string]int64
	AvailableByAccount []AccountCount
}

type AccountCount struct {
	AccountID uint64
	Count     int64
}
