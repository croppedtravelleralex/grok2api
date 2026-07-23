import { useQuery } from "@tanstack/react-query";
import { ImageIcon, MessageSquare } from "lucide-react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { ErrorState, LoadingState } from "@/shared/components/data-state";
import { getWebLaneQuotaSummary } from "@/features/accounts/accounts-api";
import { cn } from "@/shared/lib/cn";
import { formatNumber } from "@/shared/lib/format";

type WebLaneQuotaPanelProps = {
  refreshKey?: number;
};

export function WebLaneQuotaPanel({ refreshKey = 0 }: WebLaneQuotaPanelProps) {
  const { t, i18n } = useTranslation();
  const query = useQuery({
    queryKey: ["accounts", "web-lane-quota", refreshKey],
    queryFn: getWebLaneQuotaSummary,
    refetchInterval: 60_000,
  });

  if (query.isPending) return <LoadingState />;
  if (query.isError) return <ErrorState message={query.error.message} onRetry={() => void query.refetch()} />;

  const data = query.data;
  const chatUsed = Math.max(0, data.chatTotal - data.chatRemaining);
  const schedulableUsed = Math.max(0, data.imageSchedulableTotal - data.imageSchedulableRemaining);
  const bookUsed = Math.max(0, data.imageBookTotal - data.imageBookRemaining);

  return (
    <section className="space-y-3 rounded-lg bg-card p-4">
      <div>
        <h2 className="text-sm font-medium">{t("accounts.webLaneQuotaTitle")}</h2>
        <p className="mt-0.5 text-xs text-muted-foreground">{t("accounts.webLaneQuotaDescription")}</p>
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        <LaneQuotaCard
          icon={<MessageSquare className="size-4" />}
          title={t("accounts.webLaneChatTitle")}
          remaining={data.chatRemaining}
          total={data.chatTotal}
          used={chatUsed}
          unit={t("accounts.webLaneChatUnit")}
          detail={t("accounts.webLaneChatDetail", {
            known: formatNumber(data.chatKnownAccounts, i18n.language, 0),
            enabled: formatNumber(data.enabledAccounts, i18n.language, 0),
          })}
          locale={i18n.language}
        />
        <ImageLaneQuotaCard
          schedulableRemaining={data.imageSchedulableRemaining}
          schedulableTotal={data.imageSchedulableTotal}
          schedulableUsed={schedulableUsed}
          bookRemaining={data.imageBookRemaining}
          bookTotal={data.imageBookTotal}
          bookUsed={bookUsed}
          schedulableAccounts={data.imageSchedulableAccounts}
          bookAccounts={data.imageBookAccounts}
          noCapabilityAccounts={data.imageNoCapabilityAccounts}
          locale={i18n.language}
        />
      </div>
    </section>
  );
}

function LaneQuotaCard({
  icon,
  title,
  remaining,
  total,
  used,
  unit,
  detail,
  locale,
}: {
  icon: ReactNode;
  title: string;
  remaining: number;
  total: number;
  used: number;
  unit: string;
  detail: string;
  locale: string;
}) {
  const percent = total > 0 ? Math.max(0, Math.min(100, (used / total) * 100)) : 0;
  return (
    <div className="rounded-md border p-4">
      <div className="flex items-center gap-2 text-sm font-medium">
        <span className="text-muted-foreground">{icon}</span>
        <span>{title}</span>
      </div>
      <div className="mt-3 flex items-end justify-between gap-3">
        <div>
          <div className="text-2xl font-semibold tabular-nums">{formatNumber(remaining, locale, 0)}</div>
          <div className="text-xs text-muted-foreground">{unit}</div>
        </div>
        <div className="text-right text-xs text-muted-foreground">
          <div>{formatNumber(used, locale, 0)} / {formatNumber(total, locale, 0)}</div>
          <div>{detail}</div>
        </div>
      </div>
      <div className="mt-3 h-2 overflow-hidden rounded-full bg-muted">
        <div className={cn("h-full rounded-full bg-primary transition-all")} style={{ width: `${percent}%` }} />
      </div>
    </div>
  );
}

function ImageLaneQuotaCard({
  schedulableRemaining,
  schedulableTotal,
  schedulableUsed,
  bookRemaining,
  bookTotal,
  bookUsed,
  schedulableAccounts,
  bookAccounts,
  noCapabilityAccounts,
  locale,
}: {
  schedulableRemaining: number;
  schedulableTotal: number;
  schedulableUsed: number;
  bookRemaining: number;
  bookTotal: number;
  bookUsed: number;
  schedulableAccounts: number;
  bookAccounts: number;
  noCapabilityAccounts: number;
  locale: string;
}) {
  const { t } = useTranslation();
  const schedulablePercent = schedulableTotal > 0 ? Math.max(0, Math.min(100, (schedulableUsed / schedulableTotal) * 100)) : 0;

  return (
    <div className="rounded-md border p-4">
      <div className="flex items-center gap-2 text-sm font-medium">
        <span className="text-muted-foreground"><ImageIcon className="size-4" /></span>
        <span>{t("accounts.webLaneImageTitle")}</span>
      </div>
      <div className="mt-3 flex items-end justify-between gap-3">
        <div>
          <div className="text-2xl font-semibold tabular-nums">{formatNumber(schedulableRemaining, locale, 0)}</div>
          <div className="text-xs text-muted-foreground">{t("accounts.webLaneImageSchedulableUnit")}</div>
        </div>
        <div className="text-right text-xs text-muted-foreground">
          <div>{formatNumber(schedulableUsed, locale, 0)} / {formatNumber(schedulableTotal, locale, 0)}</div>
          <div>{t("accounts.webLaneImageSchedulableAccounts", { count: formatNumber(schedulableAccounts, locale, 0) })}</div>
        </div>
      </div>
      <div className="mt-3 h-2 overflow-hidden rounded-full bg-muted">
        <div className={cn("h-full rounded-full bg-primary transition-all")} style={{ width: `${schedulablePercent}%` }} />
      </div>
      <div className="mt-3 flex items-end justify-between gap-3 border-t pt-3">
        <div>
          <div className="text-lg font-semibold tabular-nums text-muted-foreground">{formatNumber(bookRemaining, locale, 0)}</div>
          <div className="text-xs text-muted-foreground">{t("accounts.webLaneImageBookUnit")}</div>
        </div>
        <div className="text-right text-xs text-muted-foreground">
          <div>{formatNumber(bookUsed, locale, 0)} / {formatNumber(bookTotal, locale, 0)}</div>
          <div>{t("accounts.webLaneImageBookAccounts", { count: formatNumber(bookAccounts, locale, 0) })}</div>
        </div>
      </div>
      <p className="mt-2 text-xs text-muted-foreground">
        {t("accounts.webLaneImageNoCapability", { count: formatNumber(noCapabilityAccounts, locale, 0) })}
      </p>
    </div>
  );
}
