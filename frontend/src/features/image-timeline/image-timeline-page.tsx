import { useQuery } from "@tanstack/react-query";
import { GanttChart, Pause, Play, RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  getImageTimeline,
  segmentRow,
  type ImageTimelineLaneLayout,
  type ImageTimelineQueueDTO,
  type ImageTimelineSegmentDTO,
  type ImageTimelineSlotDTO,
  type ImageTimelineStage,
  type ImageTimelineTraceDTO,
  type ImageTimelineWindow,
} from "@/features/image-timeline/image-timeline-api";
import { ErrorState } from "@/shared/components/data-state";
import { formatDuration, formatNumber } from "@/shared/lib/format";
import { cn } from "@/shared/lib/cn";

const STAGE_COLORS: Record<ImageTimelineStage, string> = {
  queue: "#94a3b8",
  queue_upload: "#cbd5e1",
  upload: "#64748b",
  queue_ps: "#99f6e4",
  ps: "#0d9488",
  expand: "#0d9488",
  queue_ss: "#93c5fd",
  sse: "#2563eb",
  queue_download: "#fcd34d",
  download: "#d97706",
};

const WINDOWS: ImageTimelineWindow[] = ["30m", "1h", "6h", "12h"];
const CHART_MIN_SPAN_MS = 60_000;
const CHART_MAX_SPAN_MS = 15 * 60_000;
const CHART_FOCUS_LOOKBACK_MS = 15 * 60_000;
const CHART_PADDING_MS = 15_000;
const TICK_INTERVAL_MS = 60_000;

export function ImageTimelinePage() {
  const { t, i18n } = useTranslation();
  const [window, setWindow] = useState<ImageTimelineWindow>("30m");
  const [paused, setPaused] = useState(true);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const timeline = useQuery({
    queryKey: ["image-timeline", window],
    queryFn: () => getImageTimeline(window),
    refetchInterval: paused ? false : 2000,
  });
  const data = timeline.data;
  const updatedAt = timeline.dataUpdatedAt;
  const range = useMemo(() => {
    if (!data) return { from: updatedAt - 30 * 60_000, to: updatedAt };
    return { from: Date.parse(data.from), to: Date.parse(data.to) };
  }, [data, updatedAt]);
  const chartRange = useMemo(() => computeChartRange(data?.traces ?? [], range.from, range.to), [data?.traces, range.from, range.to]);
  const selected = data?.traces.find((trace) => trace.id === selectedId) ?? null;
  const lanes = data?.lanes ?? { ps: 10, ss: 10 };
  const slotPanels = useMemo(() => mergeSlotPanels(data?.snapshot), [data?.snapshot]);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-medium">{t("imageTimeline.title")}</h1>
          <p className="mt-1 text-sm text-muted-foreground">{t("imageTimeline.description")}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex rounded-lg border p-0.5">
            {WINDOWS.map((value) => (
              <Button key={value} size="sm" variant={window === value ? "secondary" : "ghost"} className="h-7 px-2.5" onClick={() => setWindow(value)}>
                {t(`imageTimeline.window.${value}`)}
              </Button>
            ))}
          </div>
          <Button size="sm" variant="outline" onClick={() => setPaused((value) => !value)}>
            {paused ? <Play /> : <Pause />}
            {paused ? t("imageTimeline.resume") : t("imageTimeline.pause")}
          </Button>
          <Button size="sm" variant="secondary" onClick={() => void timeline.refetch()} disabled={timeline.isFetching}>
            <RefreshCw className={timeline.isFetching ? "animate-spin" : ""} />
            {t("common.refresh")}
          </Button>
        </div>
      </div>

      {data ? (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <Metric
            label={t("imageTimeline.psPool")}
            value={`${data.snapshot.promptActive ?? data.snapshot.expandActive} / ${data.snapshot.promptSlots ?? data.snapshot.pipelineSlots / 2}`}
          />
          <Metric
            label={t("imageTimeline.ssPool")}
            value={`${data.snapshot.sseActive} / ${data.snapshot.sseSlots ?? data.snapshot.sseLimit}`}
          />
          <Metric
            label={t("imageTimeline.queue")}
            value={`${data.snapshot.inFlight ?? data.snapshot.queueDepth} / ${data.snapshot.queueCapacity}`}
          />
          <Metric label={t("imageTimeline.successP90")} value={`${formatPercent(data.snapshot.successRate, i18n.language)} · P90 ${formatDuration(data.snapshot.p90TotalMs)}`} />
        </div>
      ) : null}

      {data ? (
        <div className="grid gap-4 xl:grid-cols-[2fr_1fr]">
          <SlotState slots={slotPanels} lanes={lanes} />
          <QueueState
            queue={data.snapshot.queue}
            oldestQueueMs={data.snapshot.oldestQueueMs}
            stageQueued={{
              ps: data.snapshot.promptQueued ?? data.snapshot.expandQueued,
              sse: data.snapshot.sseQueued,
              download: data.snapshot.downloadQueued,
              upload: data.snapshot.uploadQueued ?? 0,
            }}
          />
        </div>
      ) : null}

      <div className="flex flex-wrap gap-4 text-xs text-muted-foreground">
        {(Object.keys(STAGE_COLORS) as ImageTimelineStage[]).map((stage) => (
          <span key={stage} className="inline-flex items-center gap-1.5">
            <span className="size-2.5 rounded-sm" style={{ background: STAGE_COLORS[stage] }} />
            {t(`imageTimeline.stage.${stage}`, { defaultValue: stage })}
          </span>
        ))}
      </div>

      {timeline.isError ? <ErrorState message={timeline.error.message} onRetry={() => void timeline.refetch()} /> : null}
      {!timeline.isPending && (data?.traces.length ?? 0) === 0 ? (
        <div className="flex min-h-72 flex-col items-center justify-center rounded-xl border border-dashed text-muted-foreground">
          <GanttChart className="mb-3 size-8" />
          <p className="text-sm">{t("imageTimeline.empty")}</p>
        </div>
      ) : null}
      {timeline.isPending ? <div className="min-h-72 animate-pulse rounded-xl bg-secondary/40" /> : null}

      {data && data.traces.length > 0 ? (
        <div className="overflow-x-auto rounded-xl border bg-card">
          <p className="border-b px-4 py-2 text-xs text-muted-foreground">{t("imageTimeline.chartFocus")}</p>
          <GanttChartView
            lanes={lanes}
            traces={data.traces}
            fromMs={chartRange.from}
            toMs={chartRange.to}
            nowMs={range.to}
            selectedId={selectedId}
            onSelect={setSelectedId}
            locale={i18n.language}
          />
        </div>
      ) : null}

      {selected ? <TraceDetail trace={selected} locale={i18n.language} /> : null}
    </div>
  );
}

function GanttChartView({
  lanes,
  traces,
  fromMs,
  toMs,
  nowMs,
  selectedId,
  onSelect,
  locale,
}: {
  lanes: ImageTimelineLaneLayout;
  traces: ImageTimelineTraceDTO[];
  fromMs: number;
  toMs: number;
  nowMs: number;
  selectedId: string | null;
  onSelect: (id: string) => void;
  locale: string;
}) {
  const { t } = useTranslation();
  const rowCount = lanes.ps + lanes.ss;
  const rowHeight = 28;
  const labelWidth = 88;
  const width = 960;
  const height = 36 + rowCount * rowHeight;
  const ticks = buildMinuteTicks(fromMs, toMs);

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="min-w-[720px] w-full" role="img" aria-label={t("imageTimeline.chartLabel")}>
      <rect x={0} y={0} width={width} height={height} fill="transparent" />
      {ticks.map((tickMs) => {
        const ratio = (tickMs - fromMs) / Math.max(1, toMs - fromMs);
        const x = labelWidth + ratio * (width - labelWidth - 12);
        const time = new Date(tickMs);
        return (
          <g key={tickMs}>
            <line x1={x} y1={24} x2={x} y2={height} stroke="currentColor" strokeOpacity={0.08} />
            <text x={x} y={16} textAnchor="middle" className="fill-muted-foreground" fontSize={10}>
              {time.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
            </text>
          </g>
        );
      })}
      {lanes.ps > 0 ? (
        <text x={8} y={20} className="fill-muted-foreground" fontSize={10}>
          {t("imageTimeline.pool.ps")}
        </text>
      ) : null}
      {Array.from({ length: lanes.ps }, (_, index) => {
        const y = 28 + index * rowHeight;
        return (
          <g key={`ps-${index}`}>
            <text x={8} y={y + 16} className="fill-muted-foreground" fontSize={11}>
              {t("imageTimeline.psSlot", { n: index + 1 })}
            </text>
            <line x1={labelWidth} y1={y + rowHeight} x2={width} y2={y + rowHeight} stroke="currentColor" strokeOpacity={0.06} />
          </g>
        );
      })}
      {lanes.ss > 0 ? (
        <text x={8} y={28 + lanes.ps * rowHeight + 8} className="fill-muted-foreground" fontSize={10}>
          {t("imageTimeline.pool.ss")}
        </text>
      ) : null}
      {Array.from({ length: lanes.ss }, (_, index) => {
        const y = 28 + lanes.ps * rowHeight + 16 + index * rowHeight;
        return (
          <g key={`ss-${index}`}>
            <text x={8} y={y + 16} className="fill-muted-foreground" fontSize={11}>
              {t("imageTimeline.ssSlot", { n: index + 1 })}
            </text>
            <line x1={labelWidth} y1={y + rowHeight} x2={width} y2={y + rowHeight} stroke="currentColor" strokeOpacity={0.06} />
          </g>
        );
      })}
      {traces.map((trace) => {
        const failed = trace.status === "failed" || trace.status === "canceled";
        return (
          <g key={trace.id} className="cursor-pointer" onClick={() => onSelect(trace.id)}>
            {trace.segments.map((segment) => {
              const row = segmentRow(segment.stage, segment.slot ?? -1, lanes);
              if (row === null || row < 0 || row >= rowCount) return null;
              const y = 28 + row * rowHeight + (row >= lanes.ps ? 16 : 0) + 4;
              const bar = segmentBar(segment, fromMs, toMs, nowMs, width, labelWidth);
              if (!bar) return null;
              return (
                <rect
                  key={`${trace.id}-${segment.id}-${segment.sequence}`}
                  x={bar.x}
                  y={y}
                  width={bar.width}
                  height={rowHeight - 8}
                  rx={3}
                  fill={STAGE_COLORS[segment.stage]}
                  opacity={selectedId && selectedId !== trace.id ? 0.35 : 0.9}
                  stroke={failed ? "#dc2626" : selectedId === trace.id ? "#111827" : "transparent"}
                  strokeWidth={failed || selectedId === trace.id ? 1.5 : 0}
                >
                  <title>{`${trace.requestId} · ${segment.stage} · ${formatDuration(bar.durationMs)}`}</title>
                </rect>
              );
            })}
          </g>
        );
      })}
    </svg>
  );
}

function mergeSlotPanels(snapshot?: { psSlots?: ImageTimelineSlotDTO[]; ssSlots?: ImageTimelineSlotDTO[]; slots: ImageTimelineSlotDTO[] }) {
  if (!snapshot) return [];
  if (snapshot.psSlots?.length || snapshot.ssSlots?.length) {
    return [...(snapshot.psSlots ?? []), ...(snapshot.ssSlots ?? [])];
  }
  return snapshot.slots;
}

function SlotState({ slots, lanes }: { slots: ImageTimelineSlotDTO[]; lanes: ImageTimelineLaneLayout }) {
  const { t } = useTranslation();
  return (
    <section className="rounded-xl border bg-card p-4">
      <h2 className="text-sm font-medium">{t("imageTimeline.slotState")}</h2>
      <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
        {slots.map((slot) => (
          <div key={`${slot.pool ?? "lane"}-${slot.lane}`} className={cn("min-w-0 rounded-lg border px-3 py-2", slot.occupied ? "border-sky-500/30 bg-sky-500/5" : "bg-muted/30")}>
            <div className="flex items-center justify-between gap-2 text-xs">
              <span className="font-medium">
                {slot.pool === "ss" ? t("imageTimeline.ssSlot", { n: slot.lane + 1 }) : t("imageTimeline.psSlot", { n: slot.lane + 1 })}
              </span>
              <span className={slot.occupied ? "text-sky-700 dark:text-sky-300" : "text-muted-foreground"}>
                {slot.occupied ? t("imageTimeline.running") : t("imageTimeline.idle")}
              </span>
            </div>
            {slot.occupied ? (
              <div className="mt-1 space-y-0.5 text-xs text-muted-foreground">
                <p className="truncate" title={slot.requestId}>
                  {slot.requestId || slot.traceId}
                </p>
                <p>
                  {slot.stage ? t(`imageTimeline.stage.${slot.stage}`, { defaultValue: slot.stage }) : "-"}
                  {slot.waitingFor ? ` → ${t(`imageTimeline.stage.${slot.waitingFor}`, { defaultValue: slot.waitingFor })}` : ""} · {formatDuration(slot.activeMs ?? 0)}
                </p>
              </div>
            ) : null}
          </div>
        ))}
        {slots.length === 0 ? <p className="text-sm text-muted-foreground sm:col-span-2">{t("imageTimeline.emptySlots", { ps: lanes.ps, ss: lanes.ss })}</p> : null}
      </div>
    </section>
  );
}

function QueueState({
  queue,
  oldestQueueMs,
  stageQueued,
}: {
  queue: ImageTimelineQueueDTO[];
  oldestQueueMs: number;
  stageQueued: { ps: number; sse: number; download: number; upload: number };
}) {
  const { t } = useTranslation();
  return (
    <section className="rounded-xl border bg-card p-4">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-sm font-medium">{t("imageTimeline.queueState")}</h2>
        <span className="text-xs text-muted-foreground">
          {t("imageTimeline.oldest")}: {formatDuration(oldestQueueMs)}
        </span>
      </div>
      <p className="mt-2 text-xs text-muted-foreground">
        {t("imageTimeline.stage.ps")} {stageQueued.ps} · {t("imageTimeline.stage.sse")} {stageQueued.sse} · {t("imageTimeline.stage.download")} {stageQueued.download}
        {stageQueued.upload > 0 ? ` · ${t("imageTimeline.stage.upload")} ${stageQueued.upload}` : ""}
      </p>
      {queue.length === 0 ? (
        <p className="mt-3 text-sm text-muted-foreground">{t("imageTimeline.queueEmpty")}</p>
      ) : (
        <ol className="mt-3 max-h-48 space-y-2 overflow-y-auto">
          {queue.map((item) => (
            <li key={item.traceId} className="flex items-center gap-3 rounded-lg bg-muted/40 px-3 py-2 text-xs">
              <span className="font-semibold tabular-nums">#{item.position}</span>
              <span className="min-w-0 flex-1 truncate" title={item.requestId}>
                {item.requestId}
              </span>
              <span className="tabular-nums text-muted-foreground">{formatDuration(item.waitMs)}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function segmentBar(segment: ImageTimelineSegmentDTO, fromMs: number, toMs: number, nowMs: number, width: number, labelWidth: number) {
  const start = Date.parse(segment.startedAt);
  const end = segment.endedAt ? Date.parse(segment.endedAt) : nowMs;
  if (Number.isNaN(start) || Number.isNaN(end)) return null;
  const span = Math.max(1, toMs - fromMs);
  const left = Math.max(fromMs, Math.min(toMs, start));
  const right = Math.max(fromMs, Math.min(toMs, Math.max(end, start + 1)));
  if (right <= fromMs || left >= toMs) return null;
  const plotWidth = width - labelWidth - 12;
  const x = labelWidth + ((left - fromMs) / span) * plotWidth;
  const w = Math.max(2, ((right - left) / span) * plotWidth);
  return { x, width: w, durationMs: Math.max(0, end - start) };
}

function TraceDetail({ trace, locale }: { trace: ImageTimelineTraceDTO; locale: string }) {
  const { t } = useTranslation();
  return (
    <div className="rounded-xl border bg-card p-4 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-medium">{t("imageTimeline.detailTitle")}</h2>
        <span
          className={cn(
            "rounded-md px-2 py-0.5 text-xs",
            trace.status === "succeeded"
              ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300"
              : trace.status === "running" || trace.status === "queued"
                ? "bg-sky-500/15 text-sky-700 dark:text-sky-300"
                : "bg-destructive/15 text-destructive",
          )}
        >
          {trace.status}
        </span>
      </div>
      <dl className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        <Detail label={t("imageTimeline.requestId")} value={trace.requestId} />
        <Detail label={t("imageTimeline.model")} value={trace.model || "-"} />
        <Detail label={t("imageTimeline.account")} value={trace.accountName || (trace.accountId ? String(trace.accountId) : "-")} />
        <Detail label={t("imageTimeline.total")} value={formatDuration(trace.totalMs)} />
        <Detail label={t("imageTimeline.stage.queue")} value={formatDuration(trace.queueMs)} />
        {trace.psQueueMs ? <Detail label={t("imageTimeline.stage.queue_ps")} value={formatDuration(trace.psQueueMs)} /> : null}
        {trace.ssQueueMs ? <Detail label={t("imageTimeline.stage.queue_ss")} value={formatDuration(trace.ssQueueMs)} /> : null}
        {trace.uploadQueueMs ? <Detail label={t("imageTimeline.stage.queue_upload")} value={formatDuration(trace.uploadQueueMs)} /> : null}
        {trace.downloadQueueMs ? <Detail label={t("imageTimeline.stage.queue_download")} value={formatDuration(trace.downloadQueueMs)} /> : null}
        <Detail label={t("imageTimeline.stage.ps")} value={formatDuration(trace.expandMs)} />
        <Detail label={t("imageTimeline.stage.sse")} value={formatDuration(trace.sseMs)} />
        <Detail label={t("imageTimeline.stage.download")} value={formatDuration(trace.downloadMs)} />
        <Detail label={t("imageTimeline.startedAt")} value={new Date(trace.startedAt).toLocaleString(locale)} />
      </dl>
      {trace.errorCode ? <p className="mt-2 text-xs text-destructive">{trace.errorCode}</p> : null}
    </div>
  );
}

function Detail({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 break-all font-medium tabular-nums">{value}</dd>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border bg-card px-4 py-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-1 text-sm font-medium tabular-nums">{value}</p>
    </div>
  );
}

function formatPercent(value: number, locale: string) {
  return `${formatNumber(value * 100, locale, 0)}%`;
}

type TraceSpan = { start: number; end: number };

function computeChartRange(traces: ImageTimelineTraceDTO[], windowFrom: number, windowTo: number) {
  if (!traces.length || Number.isNaN(windowFrom) || Number.isNaN(windowTo)) {
    return { from: windowFrom, to: windowTo };
  }

  const spans: TraceSpan[] = traces
    .map((trace) => {
      const start = Date.parse(trace.startedAt);
      const end = start + Math.max(trace.totalMs, 0);
      return Number.isNaN(start) ? null : { start, end };
    })
    .filter((value): value is TraceSpan => value !== null);

  if (!spans.length) {
    return { from: windowFrom, to: windowTo };
  }

  const latestEnd = Math.max(...spans.map((span) => span.end));
  const focusCutoff = latestEnd - CHART_FOCUS_LOOKBACK_MS;
  const focused = spans.filter((span) => span.end >= focusCutoff);
  let from = Math.min(...focused.map((span) => span.start)) - CHART_PADDING_MS;
  let to = Math.max(...focused.map((span) => span.end)) + CHART_PADDING_MS;

  if (to - from < CHART_MIN_SPAN_MS) {
    const mid = (from + to) / 2;
    from = mid - CHART_MIN_SPAN_MS / 2;
    to = mid + CHART_MIN_SPAN_MS / 2;
  }
  if (to - from > CHART_MAX_SPAN_MS) {
    from = latestEnd - CHART_MAX_SPAN_MS;
    to = latestEnd + CHART_PADDING_MS;
  }

  from = Math.max(windowFrom, from);
  to = Math.min(windowTo, to);
  if (to <= from) {
    return { from: windowFrom, to: windowTo };
  }
  return { from, to };
}

function buildMinuteTicks(fromMs: number, toMs: number) {
  const ticks: number[] = [];
  if (Number.isNaN(fromMs) || Number.isNaN(toMs) || toMs <= fromMs) {
    return ticks;
  }
  let cursor = Math.ceil(fromMs / TICK_INTERVAL_MS) * TICK_INTERVAL_MS;
  while (cursor <= toMs) {
    ticks.push(cursor);
    cursor += TICK_INTERVAL_MS;
  }
  if (!ticks.length || ticks[0] > fromMs) {
    ticks.unshift(fromMs);
  }
  if (ticks[ticks.length - 1] < toMs) {
    ticks.push(toMs);
  }
  return ticks;
}
