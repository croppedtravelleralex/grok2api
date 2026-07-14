import { apiRequest } from "@/shared/api/client";
import { createObjectDecoder, createPaginatedDecoder, hasShape, isNumber, isString } from "@/shared/api/decoder";

export type MediaImageDTO = { id: string; url: string; mimeType: string; sizeBytes: number; createdAt: string };
export type MediaImageStatsDTO = { count: number; totalBytes: number };

const imageValidator = hasShape({ id: isString, url: isString, mimeType: isString, sizeBytes: isNumber, createdAt: isString });
const decodeImages = createPaginatedDecoder<MediaImageDTO>(imageValidator);
const decodeStats = createObjectDecoder<MediaImageStatsDTO>("media image stats", { count: isNumber, totalBytes: isNumber });
const decodeDeleted = createObjectDecoder<{ deleted: boolean }>("media image delete", { deleted: (value) => value === true });

export function getMediaImages(page: number, pageSize: number) {
  return apiRequest(`/api/admin/v1/media/images?page=${page}&pageSize=${pageSize}`, {}, decodeImages);
}

export function getMediaImageStats() {
  return apiRequest("/api/admin/v1/media/images/stats", {}, decodeStats);
}

export function deleteMediaImage(id: string) {
  return apiRequest(`/api/admin/v1/media/images/${encodeURIComponent(id)}`, { method: "DELETE" }, decodeDeleted);
}
