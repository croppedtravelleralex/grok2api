import { useQuery } from "@tanstack/react-query";
import { Activity, CheckCircle2, CircleAlert, Clock3, ImageIcon, MessageSquare, RefreshCw } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { Badge } from "@/components/ui/badge";
import { Spinner } from "@/components/ui/spinner";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { getWebProbeStatus, type WebProbeOutcome } from "@/features/accounts/accounts-api";
import { cn } from "@/shared/lib/cn";
import { formatDateTimeSeconds, formatDuration, formatNumber } from "@/shared/lib/format";

type WebProbePanelProps = {
  onCompleted?: () => void;
};

export function WebProbePanel({ onCompleted }: WebProbePanelProps) {
  const { t, i18n } = useTranslation();
  const callbackRef = useRef(onCompleted);
  const lastCompletedRef = useRef<string | undefined>(undefined);
  const [laneTab, setLaneTab] = useState<"image" | "chat">("image");
  const query = useQuery({
    queryKey: ["accounts", "web-probe"],
    queryFn: getWebProbeStatus,
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
        <div><p className="text-sm font-medium">{t("webProbe.title")}</p><p className="mt-1 text-xs text-destructive">{query.error?.message ?? t("errors.generic")}</p></div>
        <button type="button" className="text-xs text-primary" onClick={() => void query.refetch()}>{t("common.retry")}</button>
      </section>
    );
  }

  const lanePools = status.pools[laneTab];
  const laneTotal = lanePools.dispatch + lanePools.recovery + lanePools.dead;
  const dispatchPercent = laneTotal > 0 ? Math.round((lanePools.dispatch / laneTotal) * 100) : 0;
  const laneStats = laneTab === "image" ? status.statistics.image : status.statistics.chat;
  const laneAttempts = laneStats?.attempts ?? 0;
  const laneSucceeded = laneStats?.succeeded ?? 0;
  const laneFailed = laneStats?.failed ?? 0;
  const laneRecent = status.recent.filter((item) => item.lane === laneTab);
  const currentLaneItem = status.current?.lane === laneTab ? status.current : laneRecent[0];
  const currentLabel = currentLaneItem?.accountName || t("webProbe.noAccount");
  const probeUnknownQuota = status.config?.probeUnknownQuota ?? true;
  const statusLabel = !status.enabled ? t("webProbe.disabled") : status.running ? t("webProbe.running") : t("webProbe.waiting");

  return (
    <section className="rounded-lg bg-card p-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Activity className="size-4 text-primary" />
            <h2 className="text-sm font-medium">{t("webProbe.title")}</h2>
            <Badge variant={status.enabled ? "default" : "destructive"} className={cn(status.running && "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300")}>
              {status.running ? <span className="mr-1 size-1.5 animate-pulse rounded-full bg-current" /> : null}{statusLabel}
            </Badge>
          </div>
          <p className="mt-1 text-xs text-muted-foreground">{t("webProbe.description", { interval: status.intervalSeconds, idle: Math.round(status.idleIntervalSeconds / 60), level: status.budget.maxProbeLevel, probeUnknownQuota: probeUnknownQuota ? t("common.enabled") : t("common.disabled") })}</p>
        </div>
        <div className="text-left text-xs text-muted-foreground sm:text-right">
          <div>{status.running ? t("webProbe.startedAt", { time: formatDateTimeSeconds(status.current?.startedAt, i18n.language) }) : status.nextRunAt ? t("webProbe.nextRunAt", { time: formatDateTimeSeconds(status.nextRunAt, i18n.language) }) : t("webProbe.awaitingSchedule")}</div>
          <div className="mt-1 tabular-nums">{t("webProbe.pipelineLoad", { active: status.budget.pipelineActiveSlots, total: status.budget.pipelineTotalSlots, percent: status.budget.pipelineLoadPercent })}</div>
        </div>
      </div>

      <Tabs value={laneTab} onValueChange={(value) => setLaneTab(value as "image" | "chat")} className="mt-4">
        <TabsList>
          <TabsTrigger value="image"><ImageIcon className="mr-1 size-3.5" />{t("webProbe.lane.image")}</TabsTrigger>
          <TabsTrigger value="chat"><MessageSquare className="mr-1 size-3.5" />{t("webProbe.lane.chat")}</TabsTrigger>
        </TabsList>
        <TabsContent value={laneTab} className="mt-3">
          <div className="grid gap-3 xl:grid-cols-[1.25fr_1fr]">
            <div className="rounded-md bg-muted/35 p-3">
              <p className="text-[11px] text-muted-foreground">{status.running ? t("webProbe.currentAccount") : t("webProbe.lastAccount")}</p>
              <p className="mt-1 truncate text-sm font-medium" title={currentLabel}>{currentLabel}</p>
              <div className="mt-4 flex items-center justify-between text-[11px] text-muted-foreground">
                <span>{t("webProbe.dispatchCoverage")}</span><span className="tabular-nums">{lanePools.dispatch} / {laneTotal} · {dispatchPercent}%</span>
              </div>
              <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-muted">
                <div className="h-full rounded-full bg-emerald-500 transition-[width] duration-500 motion-reduce:transition-none" style={{ width: `${dispatchPercent}%` }} />
              </div>
            </div>
            <div className="grid grid-cols-2 gap-2">
              <ProbeMetric icon={<RefreshCw />} label={t("webProbe.attempts")} value={laneAttempts} locale={i18n.language} />
              <ProbeMetric icon={<CheckCircle2 />} label={t("webProbe.successful")} value={laneSucceeded} locale={i18n.language} tone="success" />
              <ProbeMetric icon={<CircleAlert />} label={t("webProbe.failed")} value={laneFailed} locale={i18n.language} tone="danger" />
              <ProbeMetric icon={<Clock3 />} label={t("webProbe.pipelineLoadShort")} value={status.budget.pipelineLoadPercent} locale={i18n.language} suffix="%" />
            </div>
            {laneTab === "image" ? (
              <p className="text-[11px] text-muted-foreground xl:col-span-2">{t("webProbe.budgetLite")}: {formatNumber(status.budget.liteGlobalUsedHour, i18n.language, 0)} / {formatNumber(status.budget.liteGlobalPerHour, i18n.language, 0)} · {t("webProbe.maxLevel")} {status.budget.maxProbeLevel}</p>
            ) : null}
          </div>
          <div className="mt-3 grid grid-cols-3 gap-2">
            {(["dispatch", "recovery", "dead"] as const).map((pool) => (
              <div key={pool} className="rounded-md bg-muted/25 px-3 py-2">
                <div className="text-[11px] text-muted-foreground">{t(`webProbe.pool.${pool}`)}</div>
                <div className="mt-1 text-sm font-medium tabular-nums">{formatNumber(lanePools[pool], i18n.language, 0)}</div>
              </div>
            ))}
          </div>
        </TabsContent>
      </Tabs>

      <div className="mt-4 border-t border-border/60 pt-3">
        <h3 className="mb-2 text-xs font-medium">{t("webProbe.recentTitle")}</h3>
        {laneRecent.length === 0 ? <p className="py-3 text-xs text-muted-foreground">{t("webProbe.noRecent")}</p> : (
          <div className="grid gap-1.5 lg:grid-cols-2">
            {laneRecent.slice(0, 6).map((item) => (
              <div key={`${item.accountId}-${item.completedAt}`} title={item.error || undefined} className="flex min-w-0 items-center gap-2 rounded-md bg-muted/20 px-3 py-2 text-xs">
                <OutcomeBadge outcome={item.outcome} label={t(`webProbe.outcome.${item.outcome}`)} />
                <span className="shrink-0 text-muted-foreground">{t(`webProbe.lane.${item.lane}`)}</span>
                <span className="min-w-0 flex-1 truncate font-medium" title={item.accountName}>{item.accountName}</span>
                <span className="shrink-0 tabular-nums text-muted-foreground">{formatDuration(item.durationMs)}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

function ProbeMetric({ icon, label, value, locale, tone = "default", suffix }: { icon?: ReactNode; label: string; value: number; locale: string; tone?: "default" | "success" | "warning" | "danger"; suffix?: string }) {
  return (
    <div className="rounded-md bg-muted/25 p-3">
      <div className={cn("flex items-center justify-between text-[11px] text-muted-foreground [&_svg]:size-3.5", tone === "success" && "text-emerald-700 dark:text-emerald-300", tone === "danger" && "text-destructive")}><span>{label}</span>{icon}</div>
      <div className="mt-2 text-lg font-medium tabular-nums">{formatNumber(value, locale, 0)}{suffix ? <span className="ml-1 text-xs text-muted-foreground">{suffix}</span> : null}</div>
    </div>
  );
}

function OutcomeBadge({ outcome, label }: { outcome: WebProbeOutcome; label: string }) {
  const success = outcome === "dispatchOk" || outcome === "recoveryOk" || outcome === "deadOk";
  const warning = outcome === "cooldown";
  return <Badge variant={success ? "default" : warning ? "secondary" : "destructive"} className={cn("shrink-0", success && "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300", warning && "bg-amber-500/10 text-amber-700 dark:text-amber-300")}>{label}</Badge>;
}
