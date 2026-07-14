package relational

import (
	"context"
	"time"

	"github.com/chenyme/grok2api/backend/internal/domain/account"
	"github.com/chenyme/grok2api/backend/internal/repository"
	"gorm.io/gorm/clause"
)

const accountAnalyticsBucket = 15 * time.Minute

type accountTypeAggregate struct {
	Provider account.Provider
	Free     int64
	Paid     int64
	Unknown  int64
}

type accountTierAggregate struct {
	Provider  account.Provider
	TierAuto  int64
	TierBasic int64
	TierSuper int64
	TierHeavy int64
}

type accountQuotaAggregate struct {
	Provider       account.Provider
	QuotaRemaining float64
	QuotaTotal     float64
	QuotaKnown     int64
}

func (r *AccountRepository) CaptureAccountPoolSnapshots(ctx context.Context, at time.Time) ([]repository.AccountPoolSnapshot, error) {
	at = at.UTC()
	bucketAt := at.Truncate(accountAnalyticsBucket)
	summaries, err := r.Summarize(ctx, at)
	if err != nil {
		return nil, err
	}
	summaryByProvider := make(map[account.Provider]repository.AccountSummary, len(summaries))
	for _, row := range summaries {
		summaryByProvider[account.Provider(row.Provider)] = row
	}

	var types []accountTypeAggregate
	typeSQL := `provider,
		SUM(CASE WHEN provider = 'grok_build' AND NOT ` + accountPaidBillingPredicate + ` AND ` + accountFreeSignalPredicate + ` THEN 1 ELSE 0 END) AS free,
		SUM(CASE WHEN provider = 'grok_build' AND ` + accountPaidBillingPredicate + ` THEN 1 ELSE 0 END) AS paid,
		SUM(CASE WHEN provider = 'grok_build' AND NOT ` + accountPaidBillingPredicate + ` AND NOT ` + accountFreeSignalPredicate + ` THEN 1 WHEN provider = 'grok_console' THEN 1 ELSE 0 END) AS unknown`
	if err := r.db.db.WithContext(ctx).Model(&accountModel{}).Select(typeSQL).Group("provider").Scan(&types).Error; err != nil {
		return nil, err
	}
	typeByProvider := make(map[account.Provider]accountTypeAggregate, len(types))
	for _, row := range types {
		typeByProvider[row.Provider] = row
	}

	var tiers []accountTierAggregate
	if err := r.db.db.WithContext(ctx).Table("provider_accounts AS account").
		Select(`account.provider,
			SUM(CASE WHEN account.provider = 'grok_web' AND COALESCE(profile.tier, 'auto') = 'auto' THEN 1 ELSE 0 END) AS tier_auto,
			SUM(CASE WHEN account.provider = 'grok_web' AND profile.tier = 'basic' THEN 1 ELSE 0 END) AS tier_basic,
			SUM(CASE WHEN account.provider = 'grok_web' AND profile.tier = 'super' THEN 1 ELSE 0 END) AS tier_super,
			SUM(CASE WHEN account.provider = 'grok_web' AND profile.tier = 'heavy' THEN 1 ELSE 0 END) AS tier_heavy`).
		Joins("LEFT JOIN web_account_profiles AS profile ON profile.account_id = account.id").
		Group("account.provider").Scan(&tiers).Error; err != nil {
		return nil, err
	}
	tierByProvider := make(map[account.Provider]accountTierAggregate, len(tiers))
	for _, row := range tiers {
		tierByProvider[row.Provider] = row
	}

	var quotas []accountQuotaAggregate
	quotaSQL := `account.provider,
		SUM(CASE WHEN account.provider = 'grok_build' THEN CASE WHEN billing.monthly_limit > billing.used THEN billing.monthly_limit - billing.used ELSE 0 END ELSE COALESCE(quota.remaining, 0) END) AS quota_remaining,
		SUM(CASE WHEN account.provider = 'grok_build' THEN COALESCE(billing.monthly_limit, 0) ELSE COALESCE(quota.total, 0) END) AS quota_total,
		SUM(CASE WHEN (account.provider = 'grok_build' AND billing.account_id IS NOT NULL) OR (account.provider <> 'grok_build' AND quota.account_id IS NOT NULL) THEN 1 ELSE 0 END) AS quota_known`
	if err := r.db.db.WithContext(ctx).Table("provider_accounts AS account").Select(quotaSQL).
		Joins("LEFT JOIN account_billing_snapshots AS billing ON billing.account_id = account.id").
		Joins("LEFT JOIN (SELECT account_id, SUM(remaining) AS remaining, SUM(total) AS total FROM account_quota_windows GROUP BY account_id) AS quota ON quota.account_id = account.id").
		Group("account.provider").Scan(&quotas).Error; err != nil {
		return nil, err
	}
	quotaByProvider := make(map[account.Provider]accountQuotaAggregate, len(quotas))
	for _, row := range quotas {
		quotaByProvider[row.Provider] = row
	}

	values := make([]repository.AccountPoolSnapshot, 0, len(account.Providers()))
	models := make([]accountPoolSnapshotModel, 0, len(account.Providers()))
	for _, providerValue := range account.Providers() {
		summary := summaryByProvider[providerValue]
		types := typeByProvider[providerValue]
		tiers := tierByProvider[providerValue]
		quota := quotaByProvider[providerValue]
		value := repository.AccountPoolSnapshot{
			BucketAt: bucketAt, Provider: providerValue, Total: summary.Total, Available: summary.Available,
			Cooldown: summary.Cooldown, WaitingReset: summary.WaitingReset, Probing: summary.Probing,
			Disabled: summary.Disabled, ReauthRequired: summary.ReauthRequired,
			Free: types.Free, Paid: types.Paid, Unknown: types.Unknown,
			TierAuto: tiers.TierAuto, TierBasic: tiers.TierBasic, TierSuper: tiers.TierSuper, TierHeavy: tiers.TierHeavy,
			QuotaRemaining: quota.QuotaRemaining, QuotaTotal: quota.QuotaTotal, QuotaKnown: quota.QuotaKnown,
		}
		values = append(values, value)
		models = append(models, snapshotModelFromRepository(value, at))
	}
	if err := r.db.db.WithContext(ctx).Clauses(clause.OnConflict{
		Columns:   []clause.Column{{Name: "bucket_at"}, {Name: "provider"}},
		DoUpdates: clause.AssignmentColumns([]string{"total", "available", "cooldown", "waiting_reset", "probing", "disabled", "reauth_required", "free", "paid", "unknown", "tier_auto", "tier_basic", "tier_super", "tier_heavy", "quota_remaining", "quota_total", "quota_known", "updated_at"}),
	}).Create(&models).Error; err != nil {
		return nil, err
	}
	return values, nil
}

func (r *AccountRepository) ListAccountPoolSnapshots(ctx context.Context, from, to time.Time) ([]repository.AccountPoolSnapshot, error) {
	var rows []accountPoolSnapshotModel
	if err := r.db.db.WithContext(ctx).Where("bucket_at >= ? AND bucket_at <= ?", from.UTC(), to.UTC()).Order("bucket_at ASC, provider ASC").Find(&rows).Error; err != nil {
		return nil, err
	}
	values := make([]repository.AccountPoolSnapshot, 0, len(rows))
	for _, row := range rows {
		values = append(values, snapshotModelToRepository(row))
	}
	return values, nil
}

func (r *AccountRepository) PruneAccountPoolSnapshots(ctx context.Context, before time.Time) (int64, error) {
	result := r.db.db.WithContext(ctx).Where("bucket_at < ?", before.UTC()).Delete(&accountPoolSnapshotModel{})
	return result.RowsAffected, result.Error
}

func snapshotModelFromRepository(value repository.AccountPoolSnapshot, now time.Time) accountPoolSnapshotModel {
	return accountPoolSnapshotModel{
		BucketAt: value.BucketAt, Provider: string(value.Provider), Total: value.Total, Available: value.Available,
		Cooldown: value.Cooldown, WaitingReset: value.WaitingReset, Probing: value.Probing,
		Disabled: value.Disabled, ReauthRequired: value.ReauthRequired,
		Free: value.Free, Paid: value.Paid, Unknown: value.Unknown,
		TierAuto: value.TierAuto, TierBasic: value.TierBasic, TierSuper: value.TierSuper, TierHeavy: value.TierHeavy,
		QuotaRemaining: value.QuotaRemaining, QuotaTotal: value.QuotaTotal, QuotaKnown: value.QuotaKnown,
		CreatedAt: now, UpdatedAt: now,
	}
}

func snapshotModelToRepository(row accountPoolSnapshotModel) repository.AccountPoolSnapshot {
	return repository.AccountPoolSnapshot{
		BucketAt: row.BucketAt, Provider: account.Provider(row.Provider), Total: row.Total, Available: row.Available,
		Cooldown: row.Cooldown, WaitingReset: row.WaitingReset, Probing: row.Probing,
		Disabled: row.Disabled, ReauthRequired: row.ReauthRequired,
		Free: row.Free, Paid: row.Paid, Unknown: row.Unknown,
		TierAuto: row.TierAuto, TierBasic: row.TierBasic, TierSuper: row.TierSuper, TierHeavy: row.TierHeavy,
		QuotaRemaining: row.QuotaRemaining, QuotaTotal: row.QuotaTotal, QuotaKnown: row.QuotaKnown,
	}
}
