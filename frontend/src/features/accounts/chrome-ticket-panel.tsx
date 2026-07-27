import { useQuery } from "@tanstack/react-query";
import { Ticket } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Badge } from "@/components/ui/badge";
import { Spinner } from "@/components/ui/spinner";
import { getChromeTicketStats, getWebProbeStatus } from "@/features/accounts/accounts-api";
import { ApiError } from "@/shared/api/client";
import { cn } from "@/shared/lib/cn";
import { formatDateTime, formatDurationSeconds } from "@/shared/lib/format";

type ChromeTicketPanelProps = {
  sseSlots?: number;
};

const TTL_ORDER = ["<1h", "1-3h", "3-6h", "6-12h", ">12h"] as const;

export function ChromeTicketPanel({ sseSlots: sseSlotsProp }: ChromeTicketPanelProps) {
  const { t, i18n } = useTranslation();
  const webProbeQuery = useQuery({
    queryKey: ["accounts", "web-probe"],
    queryFn: getWebProbeStatus,
    staleTime: 10_000,
  });
  const query = useQuery({
    queryKey: ["accounts", "chrome-tickets", "stats"],
    queryFn: getChromeTicketStats,
    refetchInterval: 30_000,
    staleTime: 20_000,
    retry: (failureCount, error) => {
      if (error instanceof ApiError && error.code === "chromeTicketPoolUnavailable") return false;
      return failureCount < 2;
    },
  });
  const sseSlots = sseSlotsProp ?? webProbeQuery.data?.budget.pipelineTotalSlots;

  if (query.isPending) {
    return <section className="flex min-h-32 items-center justify-center rounded-lg bg-card"><Spinner /></section>;
  }

  if (query.isError) {
    const unavailable = query.error instanceof ApiError && query.error.code === "chromeTicketPoolUnavailable";
    return (
      <section className="rounded-lg bg-card p-4">
        <div className="flex items-center gap-2">
          <Ticket className="size-4 text-primary" />
          <h2 className="text-sm font-medium">{t("chromeTicket.title")}</h2>
        </div>
        <p className="mt-2 text-xs text-muted-foreground">
          {unavailable ? t("chromeTicket.unavailable") : (query.error?.message ?? t("errors.generic"))}
        </p>
        {!unavailable ? (
          <button type="button" className="mt-2 text-xs text-primary" onClick={() => void query.refetch()}>{t("common.retry")}</button>
        ) : null}
      </section>
    );
  }

  const stats = query.data;
  const available = stats.byStatus.available ?? 0;
  const consumed = stats.byStatus.consumed ?? 0;
  const expired = stats.byStatus.expired ?? 0;
  const totalTracked = available + consumed + expired;
  const maxAccountCount = stats.availableByAccount.reduce((max, row) => Math.max(max, row.count), 0);
  const targetDepth = sseSlots && sseSlots > 0 ? Math.max(1, Math.ceil(sseSlots * 1.5)) : undefined;
  const depthOk = targetDepth === undefined || available >= targetDepth;
  const ttlTotal = TTL_ORDER.reduce((sum, key) => sum + (stats.ttlDistribution[key] ?? 0), 0);
  const earliestCountdown = stats.earliestExpiresInSec ?? 0;

  return (
    <section className="rounded-lg bg-card p-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Ticket className="size-4 text-primary" />
            <h2 className="text-sm font-medium">{t("chromeTicket.title")}</h2>
            <Badge variant={depthOk ? "default" : "secondary"} className={cn(depthOk ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300" : "bg-amber-500/10 text-amber-700 dark:text-amber-300")}>
              {t("chromeTicket.availableTotal", { count: available })}
            </Badge>
          </div>
          <p className="mt-1 text-xs text-muted-foreground">{t("chromeTicket.description")}</p>
        </div>
        {targetDepth !== undefined ? (
          <div className="text-xs text-muted-foreground sm:text-right">
            <div>{t("chromeTicket.depthTarget", { target: targetDepth, slots: sseSlots ?? 0 })}</div>
            <div className={cn("mt-0.5 tabular-nums", !depthOk && "text-amber-700 dark:text-amber-300")}>
              {depthOk ? t("chromeTicket.depthOk") : t("chromeTicket.depthLow")}
            </div>
          </div>
        ) : null}
      </div>

      {(stats.earliestExpiresAt || earliestCountdown > 0) ? (
        <div className="mt-3 rounded-md bg-muted/25 px-3 py-2 text-xs">
          <div className="font-medium">{t("chromeTicket.earliestExpiry")}</div>
          <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-muted-foreground">
            {stats.earliestExpiresAt ? (
              <span>{formatDateTime(stats.earliestExpiresAt, i18n.language)}</span>
            ) : null}
            {earliestCountdown > 0 ? (
              <span className="tabular-nums text-amber-700 dark:text-amber-300">
                {t("chromeTicket.countdown", { duration: formatDurationSeconds(earliestCountdown, i18n.language) })}
              </span>
            ) : null}
          </div>
        </div>
      ) : null}

      <div className="mt-4 grid grid-cols-3 gap-2">
        <StatusCard label={t("chromeTicket.status.available")} value={available} tone="success" />
        <StatusCard label={t("chromeTicket.status.consumed")} value={consumed} />
        <StatusCard label={t("chromeTicket.status.expired")} value={expired} tone="muted" />
      </div>

      {totalTracked > 0 ? (
        <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-muted">
          <div className="flex h-full">
            <div className="bg-emerald-500 transition-[width] duration-500 motion-reduce:transition-none" style={{ width: `${Math.round((available / totalTracked) * 100)}%` }} />
            <div className="bg-sky-500/80 transition-[width] duration-500 motion-reduce:transition-none" style={{ width: `${Math.round((consumed / totalTracked) * 100)}%` }} />
            <div className="bg-muted-foreground/35 transition-[width] duration-500 motion-reduce:transition-none" style={{ width: `${Math.round((expired / totalTracked) * 100)}%` }} />
          </div>
        </div>
      ) : null}

      {ttlTotal > 0 ? (
        <div className="mt-4">
          <h3 className="text-xs font-medium">{t("chromeTicket.ttlDistribution")}</h3>
          <div className="mt-2 grid gap-1.5">
            {TTL_ORDER.map((bucket) => {
              const count = stats.ttlDistribution[bucket] ?? 0;
              if (count === 0) return null;
              const width = Math.max(8, Math.round((count / ttlTotal) * 100));
              return (
                <div key={bucket} className="grid grid-cols-[4.5rem_1fr_auto] items-center gap-2 text-xs">
                  <span className="text-muted-foreground">{t(`chromeTicket.ttlBucket.${bucket}`)}</span>
                  <div className="h-2 overflow-hidden rounded-full bg-muted">
                    <div className="h-full rounded-full bg-primary/70" style={{ width: `${width}%` }} />
                  </div>
                  <span className="tabular-nums font-medium">{count}</span>
                </div>
              );
            })}
          </div>
        </div>
      ) : null}

      <div className="mt-4">
        <h3 className="text-xs font-medium">{t("chromeTicket.byAccountTitle")}</h3>
        {stats.availableByAccount.length === 0 ? (
          <p className="mt-2 text-xs text-muted-foreground">{t("chromeTicket.noAvailable")}</p>
        ) : (
          <div className="mt-2 grid gap-1.5">
            {stats.availableByAccount.map((row) => {
              const width = maxAccountCount > 0 ? Math.max(8, Math.round((row.count / maxAccountCount) * 100)) : 0;
              return (
                <div key={row.accountId} className="grid grid-cols-[5.5rem_1fr_auto] items-center gap-2 text-xs">
                  <span className="truncate tabular-nums text-muted-foreground" title={row.accountId}>#{row.accountId}</span>
                  <div className="h-2 overflow-hidden rounded-full bg-muted">
                    <div className="h-full rounded-full bg-primary/80" style={{ width: `${width}%` }} />
                  </div>
                  <span className="tabular-nums font-medium">{row.count}</span>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {stats.availableTickets.length > 0 ? (
        <div className="mt-4">
          <h3 className="text-xs font-medium">{t("chromeTicket.ticketListTitle")}</h3>
          <div className="mt-2 max-h-48 overflow-auto rounded-md border border-border/60">
            <table className="w-full text-left text-[11px]">
              <thead className="sticky top-0 bg-card text-muted-foreground">
                <tr>
                  <th className="px-2 py-1 font-medium">{t("chromeTicket.columns.account")}</th>
                  <th className="px-2 py-1 font-medium">{t("chromeTicket.columns.expiresAt")}</th>
                  <th className="px-2 py-1 font-medium text-right">{t("chromeTicket.columns.ttlLeft")}</th>
                </tr>
              </thead>
              <tbody>
                {stats.availableTickets.map((ticket) => (
                  <tr key={ticket.id} className="border-t border-border/40">
                    <td className="px-2 py-1 tabular-nums">#{ticket.accountId}</td>
                    <td className="px-2 py-1 tabular-nums text-muted-foreground">{formatDateTime(ticket.expiresAt, i18n.language)}</td>
                    <td className="px-2 py-1 text-right tabular-nums">{formatDurationSeconds(ticket.ttlRemainingSeconds, i18n.language)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </section>
  );
}

function StatusCard({ label, value, tone = "default" }: { label: string; value: number; tone?: "default" | "success" | "muted" }) {
  return (
    <div className="rounded-md bg-muted/25 px-3 py-2">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className={cn("mt-1 text-sm font-medium tabular-nums", tone === "success" && "text-emerald-700 dark:text-emerald-300", tone === "muted" && "text-muted-foreground")}>
        {value}
      </div>
    </div>
  );
}
