package poolindex

import (
	"context"
	"fmt"
	"strconv"
	"time"

	redisclient "github.com/redis/go-redis/v9"
)

// RedisDispatchMirror 用 ZSET 镜像调度索引序（score 为复合序编码）。
type RedisDispatchMirror struct {
	client redisclient.Cmdable
	key    string
}

func NewRedisDispatchMirror(client redisclient.Cmdable, keyPrefix string) *RedisDispatchMirror {
	if keyPrefix == "" {
		keyPrefix = "grok2api"
	}
	return &RedisDispatchMirror{client: client, key: keyPrefix + ":build:dispatch-index"}
}

func dispatchScore(entry DispatchEntry) float64 {
	// 高 priority、高额度、早 lastSelected 排前：用可排序的浮点近似。
	pri := float64(entry.Priority) * 1e12
	quota := 0.0
	if entry.QuotaKnown {
		quota = entry.QuotaRemaining * 1e3
	}
	// lastSelected 越早越小 → 用负时间戳
	last := -float64(entry.LastSelectedAt.UTC().UnixMilli()) / 1e6
	return pri + quota + last
}

func (m *RedisDispatchMirror) Upsert(entry DispatchEntry) {
	if m == nil || m.client == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	member := strconv.FormatUint(entry.ID, 10)
	_ = m.client.ZAdd(ctx, m.key, redisclient.Z{Score: dispatchScore(entry), Member: member}).Err()
}

func (m *RedisDispatchMirror) Remove(id uint64) {
	if m == nil || m.client == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	_ = m.client.ZRem(ctx, m.key, strconv.FormatUint(id, 10)).Err()
}

func (m *RedisDispatchMirror) TouchSelected(id uint64, at time.Time) {
	if m == nil || m.client == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	member := strconv.FormatUint(id, 10)
	score, err := m.client.ZScore(ctx, m.key, member).Result()
	if err != nil {
		return
	}
	// 仅刷新 lastSelected 分量：重写为带新时间的 score 需要完整 entry；简化为 ZADD 覆盖 last 分量。
	_ = m.client.ZAdd(ctx, m.key, redisclient.Z{
		Score:  score - float64(at.UTC().UnixMilli())/1e6 + float64(time.Now().UTC().UnixMilli())/1e6,
		Member: member,
	}).Err()
}

// RebuildFromAscend 用内存索引全量覆盖 Redis（启动重建后调用）。
func (m *RedisDispatchMirror) RebuildFromAscend(entries []DispatchEntry) error {
	if m == nil || m.client == nil {
		return nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	pipe := m.client.TxPipeline()
	pipe.Del(ctx, m.key)
	for _, entry := range entries {
		pipe.ZAdd(ctx, m.key, redisclient.Z{Score: dispatchScore(entry), Member: strconv.FormatUint(entry.ID, 10)})
	}
	_, err := pipe.Exec(ctx)
	if err != nil {
		return fmt.Errorf("rebuild dispatch zset: %w", err)
	}
	return nil
}
