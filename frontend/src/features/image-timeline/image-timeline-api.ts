import { apiRequest } from "@/shared/api/client";
import { createObjectDecoder, hasShape, isArrayOf, isBoolean, isNumber, isOptional, isString } from "@/shared/api/decoder";

export type ImageTimelineWindow = "30m" | "1h" | "6h" | "12h";
export type ImageTimelineStage =
  | "queue"
  | "queue_upload"
  | "upload"
  | "queue_ps"
  | "ps"
  | "expand"
  | "queue_ss"
  | "sse"
  | "queue_download"
  | "download";

export type ImageTimelineLaneLayout = {
  ps: number;
  ss: number;
};

export type ImageTimelineSegmentDTO = {
  id: number;
  stage: ImageTimelineStage;
  slot: number;
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
  uploadQueueMs?: number;
  psQueueMs?: number;
  ssQueueMs?: number;
  downloadQueueMs?: number;
  expandMs: number;
  sseMs: number;
  downloadMs: number;
  totalMs: number;
  softStop: boolean;
  segments: ImageTimelineSegmentDTO[];
};

export type ImageTimelineSlotDTO = {
  lane: number;
  pool?: string;
  occupied: boolean;
  traceId?: string;
  requestId?: string;
  model?: string;
  accountName?: string;
  stage?: ImageTimelineStage;
  waitingFor?: ImageTimelineStage;
  status?: string;
  startedAt?: string;
  activeMs?: number;
};

export type ImageTimelineQueueDTO = {
  position: number;
  pool?: string;
  traceId: string;
  requestId: string;
  model: string;
  enqueuedAt: string;
  waitMs: number;
};

export type ImageTimelineSnapshotDTO = {
  promptSlots?: number;
  promptActive?: number;
  promptQueued?: number;
  sseSlots?: number;
  uploadActive?: number;
  uploadLimit?: number;
  uploadQueued?: number;
  inFlight?: number;
  psSlots?: ImageTimelineSlotDTO[];
  ssSlots?: ImageTimelineSlotDTO[];
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
  expandQueued: number;
  sseQueued: number;
  downloadQueued: number;
  oldestQueueMs: number;
  slots: ImageTimelineSlotDTO[];
  queue: ImageTimelineQueueDTO[];
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
  lanes: ImageTimelineLaneLayout;
  snapshot: ImageTimelineSnapshotDTO;
  traces: ImageTimelineTraceDTO[];
};

const STAGE_VALUES: ImageTimelineStage[] = [
  "queue",
  "queue_upload",
  "upload",
  "queue_ps",
  "ps",
  "expand",
  "queue_ss",
  "sse",
  "queue_download",
  "download",
];

function isStage(value: unknown): boolean {
  return typeof value === "string" && STAGE_VALUES.includes(value as ImageTimelineStage);
}

const segmentValidator = hasShape({
  id: isNumber,
  stage: isStage,
  slot: isNumber,
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
  uploadQueueMs: isOptional(isNumber),
  psQueueMs: isOptional(isNumber),
  ssQueueMs: isOptional(isNumber),
  downloadQueueMs: isOptional(isNumber),
  expandMs: isNumber,
  sseMs: isNumber,
  downloadMs: isNumber,
  totalMs: isNumber,
  softStop: isBoolean,
  segments: isArrayOf(segmentValidator),
});

const slotValidator = hasShape({
  lane: isNumber,
  pool: isOptional(isString),
  occupied: isBoolean,
  traceId: isOptional(isString),
  requestId: isOptional(isString),
  model: isOptional(isString),
  accountName: isOptional(isString),
  stage: isOptional(isStage),
  waitingFor: isOptional(isStage),
  status: isOptional(isString),
  startedAt: isOptional(isString),
  activeMs: isOptional(isNumber),
});

const queueValidator = hasShape({
  position: isNumber,
  pool: isOptional(isString),
  traceId: isString,
  requestId: isString,
  model: isString,
  enqueuedAt: isString,
  waitMs: isNumber,
});

const snapshotValidator = hasShape({
  promptSlots: isOptional(isNumber),
  promptActive: isOptional(isNumber),
  promptQueued: isOptional(isNumber),
  sseSlots: isOptional(isNumber),
  uploadActive: isOptional(isNumber),
  uploadLimit: isOptional(isNumber),
  uploadQueued: isOptional(isNumber),
  inFlight: isOptional(isNumber),
  psSlots: isOptional(isArrayOf(slotValidator)),
  ssSlots: isOptional(isArrayOf(slotValidator)),
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
  expandQueued: isNumber,
  sseQueued: isNumber,
  downloadQueued: isNumber,
  oldestQueueMs: isNumber,
  slots: isArrayOf(slotValidator),
  queue: isArrayOf(queueValidator),
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

const lanesValidator = hasShape({ ps: isNumber, ss: isNumber });

const decodeTimeline = createObjectDecoder<ImageTimelineDTO>("image timeline", {
  from: isString,
  to: isString,
  lanes: lanesValidator,
  snapshot: snapshotValidator,
  traces: isArrayOf(traceValidator),
});

export function getImageTimeline(window: ImageTimelineWindow = "30m") {
  return apiRequest(`/api/admin/v1/image-timeline?window=${window}`, {}, decodeTimeline);
}

export function segmentRow(stage: ImageTimelineStage, slot: number, lanes: ImageTimelineLaneLayout): number | null {
  const normalized = stage === "expand" ? "ps" : stage;
  if (normalized === "ps" || normalized === "queue_ps") {
    if (slot < 0) return 0;
    return Math.min(slot, Math.max(0, lanes.ps - 1));
  }
  if (normalized === "sse" || normalized === "queue_ss") {
    if (slot < 0) return lanes.ps;
    return lanes.ps + Math.min(slot, Math.max(0, lanes.ss - 1));
  }
  if (normalized === "upload" || normalized === "queue_upload") {
    return 0;
  }
  if (normalized === "download" || normalized === "queue_download") {
    return lanes.ps + Math.max(0, lanes.ss - 1);
  }
  return null;
}
