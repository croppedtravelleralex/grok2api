import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, ImageIcon, RefreshCw, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { deleteMediaImage, getMediaImages, getMediaImageStats, type MediaImageDTO } from "@/features/media/media-api";
import { ErrorState } from "@/shared/components/data-state";
import { Pagination } from "@/shared/components/pagination";
import { formatDateTime, formatNumber } from "@/shared/lib/format";

export function MediaImagesPage() {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [deleting, setDeleting] = useState<MediaImageDTO | null>(null);
  const images = useQuery({ queryKey: ["media-images", page, pageSize], queryFn: () => getMediaImages(page, pageSize) });
  const stats = useQuery({ queryKey: ["media-images", "stats"], queryFn: getMediaImageStats });
  const remove = useMutation({
    mutationFn: (id: string) => deleteMediaImage(id),
    onSuccess: async () => {
      setDeleting(null);
      toast.success(t("mediaImages.deleted"));
      await queryClient.invalidateQueries({ queryKey: ["media-images"] });
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : t("mediaImages.deleteFailed")),
  });
  const result = images.data;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div><h1 className="text-xl font-medium">{t("mediaImages.title")}</h1><p className="mt-1 text-sm text-muted-foreground">{t("mediaImages.description")}</p></div>
        <Button variant="secondary" size="sm" onClick={() => void Promise.all([images.refetch(), stats.refetch()])} disabled={images.isFetching || stats.isFetching}><RefreshCw className={images.isFetching ? "animate-spin" : ""} />{t("common.refresh")}</Button>
      </div>
      <div className="grid gap-3 sm:grid-cols-2"><Metric label={t("mediaImages.imageCount")} value={formatNumber(stats.data?.count ?? 0, i18n.language, 0)} /><Metric label={t("mediaImages.storageUsed")} value={formatBytes(stats.data?.totalBytes ?? 0, i18n.language)} /></div>
      {images.isError ? <ErrorState message={images.error.message} onRetry={() => void images.refetch()} /> : null}
      {!images.isPending && result?.items.length === 0 ? <div className="flex min-h-64 flex-col items-center justify-center rounded-xl border border-dashed text-center text-muted-foreground"><ImageIcon className="mb-3 size-8" /><p className="text-sm">{t("mediaImages.empty")}</p></div> : null}
      {images.isPending ? <div className="min-h-64 animate-pulse rounded-xl bg-secondary/40" /> : null}
      {result?.items.length ? <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">{result.items.map((item) => (
        <article key={item.id} className="overflow-hidden rounded-xl border bg-card">
          <a href={item.url} target="_blank" rel="noreferrer" className="block aspect-square bg-secondary/30"><img src={item.url} alt={t("mediaImages.imageAlt", { time: formatDateTime(item.createdAt, i18n.language) })} loading="lazy" className="size-full object-contain" /></a>
          <div className="flex items-end gap-3 p-3"><div className="min-w-0 flex-1 text-xs text-muted-foreground"><p className="truncate text-foreground">{formatDateTime(item.createdAt, i18n.language)}</p><p className="mt-1">{formatBytes(item.sizeBytes, i18n.language)} · {item.mimeType}</p></div><Button variant="ghost" size="icon" className="size-8" asChild aria-label={t("mediaImages.download")}><a href={item.url} download><Download /></a></Button><Button variant="ghost" size="icon" className="size-8 text-destructive" onClick={() => setDeleting(item)} aria-label={t("common.delete")}><Trash2 /></Button></div>
        </article>
      ))}</div> : null}
      {result ? <Pagination page={result.page} pageSize={result.pageSize} total={result.total} onPageChange={setPage} onPageSizeChange={(value) => { setPage(1); setPageSize(value); }} /> : null}
      <AlertDialog open={deleting !== null} onOpenChange={(open) => { if (!open) setDeleting(null); }}><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>{t("mediaImages.deleteTitle")}</AlertDialogTitle><AlertDialogDescription>{t("mediaImages.deleteDescription")}</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel><AlertDialogAction onClick={() => deleting && remove.mutate(deleting.id)} disabled={remove.isPending}>{t("common.delete")}</AlertDialogAction></AlertDialogFooter></AlertDialogContent></AlertDialog>
    </div>
  );
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
