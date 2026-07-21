import { apiRequest } from "@/shared/api/client";
import { createObjectDecoder, hasShape, isArrayOf, isBoolean, isNumber, isOptional, isString } from "@/shared/api/decoder";

export type ImageTimelineWindow = "30m" | "1h" | "6h" | "12h";
export type ImageTimelineStage = "queue" | "expand" | "sse" | "download";

export type ImageTimelineSegmentDTO = {
  id: number;
  stage: ImageTimelineStage;
  sequence: number;
  startedAt: string;
  endedAt?: string;
  outcome: string;
};

export type ImageTimelineTraceDTO = {
  id: string;
  requestId: string;
  lane: number;
  status: string;
  model: string;
  accountId?: number;
  accountName: string;
  errorCode: string;
  startedAt: string;
  endedAt?: string;
  queueMs: number;
  expandMs: number;
  sseMs: number;
  downloadMs: number;
  totalMs: number;
  softStop: boolean;
  segments: ImageTimelineSegmentDTO[];
};

export type ImageTimelineSnapshotDTO = {
  pipelineSlots: number;
  activeSlots: number;
  queueDepth: number;
  queueCapacity: number;
  expandActive: number;
  expandLimit: number;
  sseActive: number;
  sseLimit: number;
  sseTarget: number;
  downloadActive: number;
  downloadLimit: number;
  successRate: number;
  sampleCount: number;
  p50TotalMs: number;
  p90TotalMs: number;
  p95TotalMs: number;
  p50ExpandMs: number;
  p90ExpandMs: number;
  p50SseMs: number;
  p90SseMs: number;
  p50DownloadMs: number;
  p90DownloadMs: number;
  updatedAt: string;
};

export type ImageTimelineDTO = {
  from: string;
  to: string;
  lanes: number;
  snapshot: ImageTimelineSnapshotDTO;
  traces: ImageTimelineTraceDTO[];
};

function isStage(value: unknown): boolean {
  return value === "queue" || value === "expand" || value === "sse" || value === "download";
}

const segmentValidator = hasShape({
  id: isNumber,
  stage: isStage,
  sequence: isNumber,
  startedAt: isString,
  endedAt: isOptional(isString),
  outcome: isString,
});

const traceValidator = hasShape({
  id: isString,
  requestId: isString,
  lane: isNumber,
  status: isString,
  model: isString,
  accountId: isOptional(isNumber),
  accountName: isString,
  errorCode: isString,
  startedAt: isString,
  endedAt: isOptional(isString),
  queueMs: isNumber,
  expandMs: isNumber,
  sseMs: isNumber,
  downloadMs: isNumber,
  totalMs: isNumber,
  softStop: isBoolean,
  segments: isArrayOf(segmentValidator),
});

const snapshotValidator = hasShape({
  pipelineSlots: isNumber,
  activeSlots: isNumber,
  queueDepth: isNumber,
  queueCapacity: isNumber,
  expandActive: isNumber,
  expandLimit: isNumber,
  sseActive: isNumber,
  sseLimit: isNumber,
  sseTarget: isNumber,
  downloadActive: isNumber,
  downloadLimit: isNumber,
  successRate: isNumber,
  sampleCount: isNumber,
  p50TotalMs: isNumber,
  p90TotalMs: isNumber,
  p95TotalMs: isNumber,
  p50ExpandMs: isNumber,
  p90ExpandMs: isNumber,
  p50SseMs: isNumber,
  p90SseMs: isNumber,
  p50DownloadMs: isNumber,
  p90DownloadMs: isNumber,
  updatedAt: isString,
});

const decodeTimeline = createObjectDecoder<ImageTimelineDTO>("image timeline", {
  from: isString,
  to: isString,
  lanes: isNumber,
  snapshot: snapshotValidator,
  traces: isArrayOf(traceValidator),
});

export function getImageTimeline(window: ImageTimelineWindow = "30m") {
  return apiRequest(`/api/admin/v1/image-timeline?window=${window}`, {}, decodeTimeline);
}
