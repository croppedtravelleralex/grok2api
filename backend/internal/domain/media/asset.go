package media

import "time"

// Asset 表示已归档到本地媒体存储的不可变资源。
type Asset struct {
	ID                   string
	Kind                 string
	StorageKey           string
	MIMEType             string
	SizeBytes            int64
	SHA256               string
	RequestID            string
	Model                string
	Resolution           string
	Width                int
	Height               int
	GenerationDurationMS int64
	CreatedAt            time.Time
}

// AssetMetadata 保存生图请求的可展示信息。尺寸由图片正文解析得到，避免信任客户端传入值。
type AssetMetadata struct {
	RequestID  string
	Model      string
	Resolution string
	StartedAt  time.Time
}
