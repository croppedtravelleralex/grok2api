import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarDays, Download, ImageIcon, RefreshCw, Ruler, Timer, Trash2, X } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { deleteMediaImage, deleteMediaImages, getMediaImages, getMediaImageStats, type MediaImageDTO, type MediaImageRange } from "@/features/media/media-api";
import { ErrorState } from "@/shared/components/data-state";
import { Pagination } from "@/shared/components/pagination";
import { formatDateTimeSeconds, formatDuration, formatNumber } from "@/shared/lib/format";

type DeleteTarget = { kind: "image"; image: MediaImageDTO } | { kind: "date"; date: string; range: MediaImageRange } | { kind: "all" };

export function MediaImagesPage() {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [date, setDate] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<DeleteTarget | null>(null);
  const range = useMemo(() => localDateRange(date), [date]);
  const images = useQuery({ queryKey: ["media-images", page, pageSize, date], queryFn: () => getMediaImages(page, pageSize, range) });
  const stats = useQuery({ queryKey: ["media-images", "stats"], queryFn: getMediaImageStats });
  const remove = useMutation({
    mutationFn: (id: string) => deleteMediaImage(id),
    onSuccess: () => finishDelete(t("mediaImages.deleted")),
    onError: (error) => toast.error(error instanceof Error ? error.message : t("mediaImages.deleteFailed")),
  });
  const bulkRemove = useMutation({
    mutationFn: (input: MediaImageRange) => deleteMediaImages(input),
    onSuccess: (result) => finishDelete(t("mediaImages.bulkDeleted", { count: result.deleted })),
    onError: (error) => toast.error(error instanceof Error ? error.message : t("mediaImages.deleteFailed")),
  });
  const result = images.data;
  const groups = useMemo(() => groupImagesByDate(result?.items ?? [], i18n.language), [result?.items, i18n.language]);

  function finishDelete(message: string) {
    setDeleteTarget(null);
    setPage(1);
    toast.success(message);
    void queryClient.invalidateQueries({ queryKey: ["media-images"] });
  }

  function confirmDelete() {
    if (!deleteTarget) return;
    if (deleteTarget.kind === "image") remove.mutate(deleteTarget.image.id);
    else bulkRemove.mutate(deleteTarget.kind === "date" ? deleteTarget.range : {});
  }

  const deleting = remove.isPending || bulkRemove.isPending;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div><h1 className="text-xl font-medium">{t("mediaImages.title")}</h1><p className="mt-1 text-sm text-muted-foreground">{t("mediaImages.description")}</p></div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <div className="relative"><CalendarDays className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" /><Input type="date" className="h-8 w-40 pl-8" value={date} aria-label={t("mediaImages.dateFilter")} onChange={(event) => { setDate(event.target.value); setPage(1); }} /></div>
          {date ? <Button variant="ghost" size="sm" onClick={() => { setDate(""); setPage(1); }}><X />{t("mediaImages.clearDate")}</Button> : null}
          {date ? <Button variant="outline" size="sm" className="text-destructive" disabled={(result?.total ?? 0) === 0} onClick={() => setDeleteTarget({ kind: "date", date, range })}><Trash2 />{t("mediaImages.deleteDate")}</Button> : null}
          <Button variant="outline" size="sm" className="text-destructive" disabled={(stats.data?.count ?? 0) === 0} onClick={() => setDeleteTarget({ kind: "all" })}><Trash2 />{t("mediaImages.deleteAll")}</Button>
          <Button variant="secondary" size="sm" onClick={() => void Promise.all([images.refetch(), stats.refetch()])} disabled={images.isFetching || stats.isFetching}><RefreshCw className={images.isFetching ? "animate-spin" : ""} />{t("common.refresh")}</Button>
        </div>
      </div>
      <div className="grid gap-3 sm:grid-cols-2"><Metric label={t("mediaImages.imageCount")} value={formatNumber(stats.data?.count ?? 0, i18n.language, 0)} /><Metric label={t("mediaImages.storageUsed")} value={formatBytes(stats.data?.totalBytes ?? 0, i18n.language)} /></div>
      {images.isError ? <ErrorState message={images.error.message} onRetry={() => void images.refetch()} /> : null}
      {!images.isPending && result?.items.length === 0 ? <div className="flex min-h-64 flex-col items-center justify-center rounded-xl border border-dashed text-center text-muted-foreground"><ImageIcon className="mb-3 size-8" /><p className="text-sm">{date ? t("mediaImages.emptyDate") : t("mediaImages.empty")}</p></div> : null}
      {images.isPending ? <div className="min-h-64 animate-pulse rounded-xl bg-secondary/40" /> : null}
      {groups.map((group) => <section key={group.key} className="space-y-3"><div className="flex items-center gap-2"><CalendarDays className="size-4 text-muted-foreground" /><h2 className="text-sm font-medium">{group.label}</h2><span className="text-xs text-muted-foreground">{t("mediaImages.groupCount", { count: group.items.length })}</span></div><div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">{group.items.map((item) => <ImageCard key={item.id} item={item} locale={i18n.language} onDelete={() => setDeleteTarget({ kind: "image", image: item })} />)}</div></section>)}
      {result ? <Pagination page={result.page} pageSize={result.pageSize} total={result.total} onPageChange={setPage} onPageSizeChange={(value) => { setPage(1); setPageSize(value); }} /> : null}
      <AlertDialog open={deleteTarget !== null} onOpenChange={(open) => { if (!open && !deleting) setDeleteTarget(null); }}><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>{deleteTarget?.kind === "all" ? t("mediaImages.deleteAllTitle") : deleteTarget?.kind === "date" ? t("mediaImages.deleteDateTitle", { date: deleteTarget.date }) : t("mediaImages.deleteTitle")}</AlertDialogTitle><AlertDialogDescription>{deleteTarget?.kind === "all" ? t("mediaImages.deleteAllDescription") : deleteTarget?.kind === "date" ? t("mediaImages.deleteDateDescription") : t("mediaImages.deleteDescription")}</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel disabled={deleting}>{t("common.cancel")}</AlertDialogCancel><AlertDialogAction onClick={confirmDelete} disabled={deleting}>{deleting ? t("common.loading") : t("common.delete")}</AlertDialogAction></AlertDialogFooter></AlertDialogContent></AlertDialog>
    </div>
  );
}

function ImageCard({ item, locale, onDelete }: { item: MediaImageDTO; locale: string; onDelete: () => void }) {
  const { t } = useTranslation();
  const dimensions = item.width > 0 && item.height > 0 ? `${item.width} × ${item.height}` : t("mediaImages.unknown");
  const duration = item.generationDurationMs > 0 ? formatDuration(item.generationDurationMs) : t("mediaImages.unknown");
  return <article className="overflow-hidden rounded-xl border bg-card"><a href={item.url} target="_blank" rel="noreferrer" className="block aspect-square bg-secondary/30"><img src={item.url} alt={t("mediaImages.imageAlt", { time: formatDateTimeSeconds(item.createdAt, locale) })} loading="lazy" className="size-full object-contain" /></a><div className="space-y-2 p-3"><p className="text-xs font-medium text-foreground">{formatDateTimeSeconds(item.createdAt, locale)}</p><div className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs text-muted-foreground"><span className="inline-flex items-center gap-1"><Timer className="size-3" />{t("mediaImages.generationDuration")}</span><span className="text-right tabular-nums">{duration}</span><span className="inline-flex items-center gap-1"><Ruler className="size-3" />{t("mediaImages.dimensions")}</span><span className="text-right tabular-nums">{dimensions}</span>{item.resolution ? <><span>{t("mediaImages.requestedResolution")}</span><span className="text-right">{item.resolution}</span></> : null}<span>{t("mediaImages.file")}</span><span className="text-right">{formatBytes(item.sizeBytes, locale)} · {item.mimeType.replace("image/", "")}</span></div>{item.model ? <p className="truncate text-[11px] text-muted-foreground" title={item.model}>{t("mediaImages.model")}: {item.model}</p> : null}<div className="flex justify-end gap-1"><Button variant="ghost" size="icon" className="size-8" asChild aria-label={t("mediaImages.download")}><a href={item.url} download><Download /></a></Button><Button variant="ghost" size="icon" className="size-8 text-destructive" onClick={onDelete} aria-label={t("common.delete")}><Trash2 /></Button></div></div></article>;
}

function localDateRange(value: string): MediaImageRange {
  if (!value) return {};
  const from = new Date(`${value}T00:00:00`);
  if (Number.isNaN(from.getTime())) return {};
  const to = new Date(from);
  to.setDate(to.getDate() + 1);
  return { from: from.toISOString(), to: to.toISOString() };
}

function groupImagesByDate(items: MediaImageDTO[], locale: string) {
  const groups = new Map<string, { key: string; label: string; items: MediaImageDTO[] }>();
  const formatter = new Intl.DateTimeFormat(locale, { dateStyle: "full" });
  for (const item of items) {
    const date = new Date(item.createdAt);
    const key = Number.isNaN(date.getTime()) ? "unknown" : `${date.getFullYear()}-${date.getMonth() + 1}-${date.getDate()}`;
    const group = groups.get(key) ?? { key, label: Number.isNaN(date.getTime()) ? "-" : formatter.format(date), items: [] };
    group.items.push(item);
    groups.set(key, group);
  }
  return [...groups.values()];
}

function Metric({ label, value }: { label: string; value: string }) { return <div className="rounded-xl border bg-card p-4"><p className="text-xs text-muted-foreground">{label}</p><p className="mt-2 text-2xl font-semibold">{value}</p></div>; }

function formatBytes(value: number, locale: string): string {
  if (value < 1024) return `${formatNumber(value, locale, 0)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = value / 1024;
  let unit = units[0];
  for (let index = 1; index < units.length && size >= 1024; index += 1) { size /= 1024; unit = units[index]; }
  return `${formatNumber(size, locale, 2)} ${unit}`;
}
