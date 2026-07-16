import { useQuery } from "@tanstack/react-query";
import { Activity, CheckCircle2, CircleAlert, Clock3, RefreshCw, ShieldCheck } from "lucide-react";
import { useEffect, useRef, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { Badge } from "@/components/ui/badge";
import { Spinner } from "@/components/ui/spinner";
import { getBuildProbeStatus, type BuildProbeOutcome } from "@/features/accounts/accounts-api";
import { cn } from "@/shared/lib/cn";
import { formatDateTimeSeconds, formatDuration, formatNumber } from "@/shared/lib/format";

type BuildProbePanelProps = {
  onCompleted?: () => void;
};

export function BuildProbePanel({ onCompleted }: BuildProbePanelProps) {
  const { t, i18n } = useTranslation();
  const callbackRef = useRef(onCompleted);
  const lastCompletedRef = useRef<string | undefined>(undefined);
  const query = useQuery({
    queryKey: ["accounts", "build-probe"],
    queryFn: getBuildProbeStatus,
    refetchInterval: 5_000,
    staleTime: 4_000,
  });
  const status = query.data;

  useEffect(() => {
    callbackRef.current = onCompleted;
  }, [onCompleted]);

  useEffect(() => {
    const completedAt = status?.lastCompletedAt;
    if (!completedAt) return;
    if (lastCompletedRef.current && lastCompletedRef.current !== completedAt) callbackRef.current?.();
    lastCompletedRef.current = completedAt;
  }, [status?.lastCompletedAt]);

  if (query.isPending) {
    return <section className="flex min-h-40 items-center justify-center rounded-lg bg-card"><Spinner /></section>;
  }
  if (query.isError || !status) {
    return (
      <section className="flex min-h-32 items-center justify-between gap-4 rounded-lg bg-card p-4">
        <div><p className="text-sm font-medium">{t("buildProbe.title")}</p><p className="mt-1 text-xs text-destructive">{query.error?.message ?? t("errors.generic")}</p></div>
        <button type="button" className="text-xs text-primary" onClick={() => void query.refetch()}>{t("common.retry")}</button>
      </section>
    );
  }

  const poolTotal = Object.values(status.pools).reduce((sum, value) => sum + value, 0);
  const productionPercent = poolTotal > 0 ? Math.round((status.pools.production / poolTotal) * 100) : 0;
  const handledSuccessfully = status.statistics.verified + status.statistics.recovered;
  const poolActions = status.statistics.cooledDown + status.statistics.quarantined + status.statistics.recoveryQueued + status.statistics.retired;
  const currentLabel = status.current?.accountName || status.recent[0]?.accountName || t("buildProbe.noAccount");
  const statusLabel = !status.enabled ? t("buildProbe.disabled") : status.running ? t("buildProbe.running") : t("buildProbe.waiting");

  return (
    <section className="rounded-lg bg-card p-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Activity className="size-4 text-primary" />
            <h2 className="text-sm font-medium">{t("buildProbe.title")}</h2>
            <Badge variant={status.enabled ? "default" : "destructive"} className={cn(status.running && "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300")}>
              {status.running ? <span className="mr-1 size-1.5 animate-pulse rounded-full bg-current" /> : null}{statusLabel}
            </Badge>
          </div>
          <p className="mt-1 text-xs text-muted-foreground">{t("buildProbe.description", { interval: status.intervalSeconds, idle: Math.round(status.idleIntervalSeconds / 60) })}</p>
        </div>
        <div className="text-left text-xs text-muted-foreground sm:text-right">
          <div>{status.running ? t("buildProbe.startedAt", { time: formatDateTimeSeconds(status.current?.startedAt, i18n.language) }) : status.nextRunAt ? t("buildProbe.nextRunAt", { time: formatDateTimeSeconds(status.nextRunAt, i18n.language) }) : t("buildProbe.awaitingSchedule")}</div>
          <div className="mt-1 tabular-nums">{t("buildProbe.consecutiveFailures", { count: formatNumber(status.statistics.consecutiveFailures, i18n.language, 0) })}</div>
        </div>
      </div>

      <div className="mt-4 grid gap-3 xl:grid-cols-[1.25fr_1fr]">
        <div className="rounded-md bg-muted/35 p-3">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <p className="text-[11px] text-muted-foreground">{status.running ? t("buildProbe.currentAccount") : t("buildProbe.lastAccount")}</p>
              <p className="mt-1 truncate text-sm font-medium" title={currentLabel}>{currentLabel}</p>
              <p className="mt-1 text-xs text-muted-foreground">{status.current ? t(`buildProbe.mode.${status.current.mode}`) : status.recent[0] ? t(`buildProbe.outcome.${status.recent[0].outcome}`) : t("buildProbe.notStarted")}</p>
            </div>
            <ShieldCheck className="size-5 shrink-0 text-emerald-600" />
          </div>
          <div className="mt-4 flex items-center justify-between text-[11px] text-muted-foreground">
            <span>{t("buildProbe.productionCoverage")}</span><span className="tabular-nums">{status.pools.production} / {poolTotal} · {productionPercent}%</span>
          </div>
          <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-muted">
            <div className="h-full rounded-full bg-emerald-500 transition-[width] duration-500 motion-reduce:transition-none" style={{ width: `${productionPercent}%` }} />
          </div>
        </div>

        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 xl:grid-cols-2">
          <ProbeMetric icon={<RefreshCw />} label={t("buildProbe.attempts")} value={status.statistics.attempts} locale={i18n.language} />
          <ProbeMetric icon={<CheckCircle2 />} label={t("buildProbe.successful")} value={handledSuccessfully} locale={i18n.language} tone="success" />
          <ProbeMetric icon={<CircleAlert />} label={t("buildProbe.failed")} value={status.statistics.failed} locale={i18n.language} tone="danger" />
          <ProbeMetric icon={<Clock3 />} label={t("buildProbe.poolActions")} value={poolActions} locale={i18n.language} tone="warning" />
        </div>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4 xl:grid-cols-7">
        {Object.entries(status.pools).map(([pool, count]) => (
          <div key={pool} className="rounded-md bg-muted/25 px-3 py-2">
            <div className="text-[11px] text-muted-foreground">{t(`buildProbe.pool.${pool}`)}</div>
            <div className="mt-1 text-sm font-medium tabular-nums">{formatNumber(count, i18n.language, 0)}</div>
          </div>
        ))}
      </div>

      <div className="mt-4 border-t border-border/60 pt-3">
        <div className="mb-2 flex items-center justify-between gap-3"><h3 className="text-xs font-medium">{t("buildProbe.recentTitle")}</h3><span className="text-[11px] text-muted-foreground">{t("buildProbe.runtimeStatistics")}</span></div>
        {status.recent.length === 0 ? <p className="py-3 text-xs text-muted-foreground">{t("buildProbe.noRecent")}</p> : (
          <div className="grid gap-1.5 lg:grid-cols-2">
            {status.recent.slice(0, 6).map((item) => (
              <div key={`${item.accountId}-${item.completedAt}`} title={item.error || undefined} className="flex min-w-0 items-center gap-2 rounded-md bg-muted/20 px-3 py-2 text-xs">
                <OutcomeBadge outcome={item.outcome} label={t(`buildProbe.outcome.${item.outcome}`)} />
                <span className="min-w-0 flex-1 truncate font-medium" title={item.accountName}>{item.accountName}</span>
                <span className="shrink-0 tabular-nums text-muted-foreground">{formatDuration(item.durationMs)}</span>
                <span className="hidden shrink-0 text-muted-foreground sm:inline">{formatDateTimeSeconds(item.completedAt, i18n.language)}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

function ProbeMetric({ icon, label, value, locale, tone = "default" }: { icon: ReactNode; label: string; value: number; locale: string; tone?: "default" | "success" | "warning" | "danger" }) {
  return (
    <div className="rounded-md bg-muted/25 p-3">
      <div className={cn("flex items-center justify-between text-[11px] text-muted-foreground [&_svg]:size-3.5", tone === "success" && "text-emerald-700 dark:text-emerald-300", tone === "warning" && "text-amber-700 dark:text-amber-300", tone === "danger" && "text-destructive")}><span>{label}</span>{icon}</div>
      <div className="mt-2 text-lg font-medium tabular-nums">{formatNumber(value, locale, 0)}</div>
    </div>
  );
}

function OutcomeBadge({ outcome, label }: { outcome: BuildProbeOutcome; label: string }) {
  const success = outcome === "verified" || outcome === "recovered";
  const warning = outcome === "cooldown" || outcome === "recovery";
  return <Badge variant={success ? "default" : warning ? "secondary" : "destructive"} className={cn("shrink-0", success && "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300", warning && "bg-amber-500/10 text-amber-700 dark:text-amber-300")}>{label}</Badge>;
}
