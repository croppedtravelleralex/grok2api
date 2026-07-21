package egress

import "time"

type Mode string

const (
	ModeDirect Mode = "direct"
	ModeSingle Mode = "single"
	ModePool   Mode = "pool"
)

type Scope string

const (
	ScopeBuild     Scope = "grok_build"
	ScopeWeb       Scope = "grok_web"
	ScopeConsole   Scope = "grok_console"
	ScopeWebAsset  Scope = "grok_web_asset"
	ScopeWebExpand Scope = "grok_web_expand" // 仅并发闸门；节点回退到 grok_web
)

type Node struct {
	ID                        uint64
	Name                      string
	Scope                     Scope
	Enabled                   bool
	EncryptedProxyURL         string
	UserAgent                 string
	EncryptedCloudflareCookie string
	Health                    float64
	FailureCount              int
	CooldownUntil             *time.Time
	LastError                 string
	CreatedAt                 time.Time
	UpdatedAt                 time.Time
}

type PublicNode struct {
	ID               uint64
	Name             string
	Scope            Scope
	Enabled          bool
	ProxyConfigured  bool
	UserAgent        string
	CookieConfigured bool
	Health           float64
	FailureCount     int
	CooldownUntil    *time.Time
	LastError        string
	CreatedAt        time.Time
	UpdatedAt        time.Time
}
