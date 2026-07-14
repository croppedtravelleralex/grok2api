import { useQuery } from "@tanstack/react-query";
import { useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Area, AreaChart, CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts";

import { Button } from "@/components/ui/button";
import { ChartContainer, ChartLegend, ChartLegendContent, ChartTooltip, ChartTooltipContent, type ChartConfig } from "@/components/ui/chart";
import { ErrorState, LoadingState } from "@/shared/components/data-state";
import { formatDateTime } from "@/shared/lib/format";
import { getAccountAnalytics, type AccountAnalyticsPeriod, type AccountProvider } from "@/features/accounts/accounts-api";

export function AccountTrends({ provider }: { provider: AccountProvider }) {
  const { t, i18n } = useTranslation();
  const [period, setPeriod] = useState<AccountAnalyticsPeriod>("24h");
  const query = useQuery({
    queryKey: ["accounts", "analytics", period],
    queryFn: () => getAccountAnalytics(period),
    refetchInterval: 5 * 60 * 1000,
  });
  const points = useMemo(() => (query.data?.points ?? []).filter((point) => point.provider === provider).map((point) => ({
    ...point,
    timeLabel: new Intl.DateTimeFormat(i18n.language, period === "24h" ? { hour: "2-digit", minute: "2-digit" } : { month: "2-digit", day: "2-digit", hour: "2-digit" }).format(new Date(point.bucketAt)),
    tooltipLabel: formatDateTime(point.bucketAt, i18n.language),
  })), [i18n.language, period, provider, query.data?.points]);

  const statusConfig = {
    available: { label: t("accounts.trendAvailable"), color: "hsl(142 71% 45%)" },
    cooldown: { label: t("accounts.statusCooldown"), color: "hsl(38 92% 50%)" },
    waitingReset: { label: t("accounts.waitingReset"), color: "hsl(25 95% 53%)" },
    reauthRequired: { label: t("accounts.statusReauthRequired"), color: "hsl(0 72% 51%)" },
  } satisfies ChartConfig;
  const typeConfig: ChartConfig = provider === "grok_build" ? {
    free: { label: t("accounts.quotaFree"), color: "hsl(142 71% 45%)" },
    paid: { label: t("accounts.quotaSuper"), color: "hsl(217 91% 60%)" },
    unknown: { label: t("dashboard.unknown"), color: "hsl(215 16% 47%)" },
  } : provider === "grok_web" ? {
    tierAuto: { label: "Auto", color: "hsl(215 16% 47%)" },
    tierBasic: { label: "Basic", color: "hsl(142 71% 45%)" },
    tierSuper: { label: "Super", color: "hsl(217 91% 60%)" },
    tierHeavy: { label: "Heavy", color: "hsl(271 81% 56%)" },
  } : {
    unknown: { label: t("dashboard.unknown"), color: "hsl(215 16% 47%)" },
  };
  const quotaConfig = {
    quotaRemaining: { label: t("accounts.trendQuotaRemaining"), color: "hsl(217 91% 60%)" },
    quotaTotal: { label: t("accounts.trendQuotaTotal"), color: "hsl(215 16% 65%)" },
  } satisfies ChartConfig;

  return (
    <section className="space-y-3 rounded-lg bg-card p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-sm font-medium">{t("accounts.trendsTitle")}</h2>
          <p className="mt-0.5 text-xs text-muted-foreground">{t("accounts.trendsDescription", { minutes: query.data?.intervalMinutes ?? 15 })}</p>
        </div>
        <div className="flex items-center gap-1">
          {(["24h", "7d", "30d"] as const).map((value) => <Button key={value} type="button" size="sm" variant={period === value ? "default" : "ghost"} onClick={() => setPeriod(value)}>{value}</Button>)}
        </div>
      </div>
      {query.isPending ? <LoadingState /> : null}
      {query.isError ? <ErrorState message={query.error.message} onRetry={() => void query.refetch()} /> : null}
      {!query.isPending && !query.isError ? (
        <div className="grid gap-4 xl:grid-cols-3">
          <TrendPanel title={t("accounts.trendStatusTitle")}>
            <ChartContainer config={statusConfig} className="h-56 w-full aspect-auto">
              <LineChart data={points} margin={{ left: -20, right: 8, top: 8 }}>
                <CartesianGrid vertical={false} />
                <XAxis dataKey="timeLabel" tickLine={false} axisLine={false} minTickGap={24} />
                <YAxis allowDecimals={false} tickLine={false} axisLine={false} />
                <ChartTooltip content={<ChartTooltipContent labelFormatter={(_label, payload) => payload?.[0]?.payload?.tooltipLabel ?? ""} />} />
                <ChartLegend content={<ChartLegendContent />} />
                <Line dataKey="available" type="monotone" stroke="var(--color-available)" strokeWidth={2} dot={false} />
                <Line dataKey="cooldown" type="monotone" stroke="var(--color-cooldown)" strokeWidth={2} dot={false} />
                <Line dataKey="waitingReset" type="monotone" stroke="var(--color-waitingReset)" strokeWidth={2} dot={false} />
                <Line dataKey="reauthRequired" type="monotone" stroke="var(--color-reauthRequired)" strokeWidth={2} dot={false} />
              </LineChart>
            </ChartContainer>
          </TrendPanel>
          <TrendPanel title={t("accounts.trendTypeTitle")}>
            <ChartContainer config={typeConfig} className="h-56 w-full aspect-auto">
              <LineChart data={points} margin={{ left: -20, right: 8, top: 8 }}>
                <CartesianGrid vertical={false} />
                <XAxis dataKey="timeLabel" tickLine={false} axisLine={false} minTickGap={24} />
                <YAxis allowDecimals={false} tickLine={false} axisLine={false} />
                <ChartTooltip content={<ChartTooltipContent labelFormatter={(_label, payload) => payload?.[0]?.payload?.tooltipLabel ?? ""} />} />
                <ChartLegend content={<ChartLegendContent />} />
                {Object.keys(typeConfig).map((key) => <Line key={key} dataKey={key} type="monotone" stroke={`var(--color-${key})`} strokeWidth={2} dot={false} />)}
              </LineChart>
            </ChartContainer>
          </TrendPanel>
          <TrendPanel title={t("accounts.trendQuotaTitle")}>
            <ChartContainer config={quotaConfig} className="h-56 w-full aspect-auto">
              <AreaChart data={points} margin={{ left: -12, right: 8, top: 8 }}>
                <CartesianGrid vertical={false} />
                <XAxis dataKey="timeLabel" tickLine={false} axisLine={false} minTickGap={24} />
                <YAxis tickLine={false} axisLine={false} />
                <ChartTooltip content={<ChartTooltipContent labelFormatter={(_label, payload) => payload?.[0]?.payload?.tooltipLabel ?? ""} />} />
                <ChartLegend content={<ChartLegendContent />} />
                <Area dataKey="quotaTotal" type="monotone" stroke="var(--color-quotaTotal)" fill="var(--color-quotaTotal)" fillOpacity={0.12} />
                <Area dataKey="quotaRemaining" type="monotone" stroke="var(--color-quotaRemaining)" fill="var(--color-quotaRemaining)" fillOpacity={0.24} />
              </AreaChart>
            </ChartContainer>
          </TrendPanel>
        </div>
      ) : null}
    </section>
  );
}

function TrendPanel({ title, children }: { title: string; children: ReactNode }) {
  return <div className="min-w-0 rounded-md border p-3"><h3 className="mb-1 text-xs font-medium text-muted-foreground">{title}</h3>{children}</div>;
}
