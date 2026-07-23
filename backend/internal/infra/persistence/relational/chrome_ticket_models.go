package relational

import "time"

type chromeTicketModel struct {
	ID           string `gorm:"size:64;primaryKey;check:chk_chrome_tickets_id,length(trim(id)) BETWEEN 16 AND 64"`
	AccountID    uint64 `gorm:"not null;index:idx_chrome_tickets_account"`
	StatsigMeta  string `gorm:"type:text;not null;check:chk_chrome_tickets_meta,length(trim(statsig_meta)) > 0"`
	DeviceCookie string `gorm:"type:text;not null;default:''"`
	UserAgent    string `gorm:"size:512;not null;default:'';check:chk_chrome_tickets_user_agent,length(user_agent) <= 512"`
	SignSource   string `gorm:"size:64;not null;default:'';check:chk_chrome_tickets_sign_source,length(sign_source) <= 64"`
	CreatedAt    time.Time `gorm:"not null"`
	ExpiresAt    time.Time `gorm:"not null;index:idx_chrome_tickets_avail,priority:2"`
	ConsumedAt   *time.Time
	Status       string `gorm:"size:32;not null;default:available;index:idx_chrome_tickets_avail,priority:1;check:chk_chrome_tickets_status,status IN ('available','consumed','expired')"`
}

func (chromeTicketModel) TableName() string { return "chrome_tickets" }
