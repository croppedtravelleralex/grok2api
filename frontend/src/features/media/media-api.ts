import { apiRequest } from "@/shared/api/client";
import { createObjectDecoder, createPaginatedDecoder, hasShape, isNumber, isOptional, isString } from "@/shared/api/decoder";

export type MediaImageDTO = { id: string; url: string; mimeType: string; sizeBytes: number; requestId?: string; model?: string; resolution?: string; width: number; height: number; generationDurationMs: number; createdAt: string };
export type MediaImageStatsDTO = { count: number; totalBytes: number };
export type MediaImageRange = { from?: string; to?: string };
export type MediaImageDeleteResultDTO = { deleted: number; totalBytes: number };

const imageValidator = hasShape({ id: isString, url: isString, mimeType: isString, sizeBytes: isNumber, requestId: isOptional(isString), model: isOptional(isString), resolution: isOptional(isString), width: isNumber, height: isNumber, generationDurationMs: isNumber, createdAt: isString });
const decodeImages = createPaginatedDecoder<MediaImageDTO>(imageValidator);
const decodeStats = createObjectDecoder<MediaImageStatsDTO>("media image stats", { count: isNumber, totalBytes: isNumber });
const decodeDeleted = createObjectDecoder<{ deleted: boolean }>("media image delete", { deleted: (value) => value === true });
const decodeBulkDeleted = createObjectDecoder<MediaImageDeleteResultDTO>("media image bulk delete", { deleted: isNumber, totalBytes: isNumber });

export function getMediaImages(page: number, pageSize: number, range: MediaImageRange = {}) {
  const query = new URLSearchParams({ page: String(page), pageSize: String(pageSize) });
  if (range.from) query.set("from", range.from);
  if (range.to) query.set("to", range.to);
  return apiRequest(`/api/admin/v1/media/images?${query}`, {}, decodeImages);
}

export function getMediaImageStats() {
  return apiRequest("/api/admin/v1/media/images/stats", {}, decodeStats);
}

export function deleteMediaImage(id: string) {
  return apiRequest(`/api/admin/v1/media/images/${encodeURIComponent(id)}`, { method: "DELETE" }, decodeDeleted);
}

export function deleteMediaImages(range: MediaImageRange = {}) {
  const query = new URLSearchParams();
  if (range.from) query.set("from", range.from);
  if (range.to) query.set("to", range.to);
  const suffix = query.size > 0 ? `?${query}` : "";
  return apiRequest(`/api/admin/v1/media/images${suffix}`, { method: "DELETE" }, decodeBulkDeleted);
}
