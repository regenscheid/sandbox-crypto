// SPDX-License-Identifier: MIT
/** Measured, per-answer context and cache telemetry; no estimates or interpolation. */
import type { ModelUsage } from './api';

export interface Point {
  call: number;
  input: number;
  output: number | null;
  read: number | null;
  write: number | null;
}

export interface UsageView {
  points: Point[];
  segments: Point[][];
  maximum: number;
  eligible: number;
  cacheKnown: number;
  readShare: number | null;
  cacheRead: number | null;
  cacheWrite: number | null;
}

export function contextUsage(usage: ModelUsage[] | undefined): UsageView {
  const rows = usage ?? [];
  const points: Point[] = [];
  const segments: Point[][] = [];
  let segment: Point[] = [];
  let eligible = 0;
  let cacheKnown = 0;
  let knownInput = 0;
  let cacheRead = 0;
  let cacheWrite = 0;
  let writeKnown = 0;
  for (const [index, row] of rows.entries()) {
    if (row.input_tokens === null || row.input_tokens === undefined) {
      if (segment.length) segments.push(segment);
      segment = [];
      continue;
    }
    eligible++;
    const point: Point = {
      call: index + 1,
      input: row.input_tokens,
      output: row.output_tokens,
      read: row.cache_read_input_tokens ?? null,
      write: row.cache_creation_input_tokens ?? null,
    };
    points.push(point);
    segment.push(point);
    if (point.read !== null) {
      cacheKnown++;
      knownInput += point.input;
      cacheRead += point.read;
    }
    if (point.write !== null) {
      writeKnown++;
      cacheWrite += point.write;
    }
  }
  if (segment.length) segments.push(segment);
  return {
    points,
    segments,
    maximum: Math.max(1, ...points.map((point) => point.input)),
    eligible,
    cacheKnown,
    readShare: knownInput ? cacheRead / knownInput : null,
    cacheRead: cacheKnown ? cacheRead : null,
    cacheWrite: writeKnown ? cacheWrite : null,
  };
}

export function plotX(call: number, total: number): number {
  return 40 + ((call - 1) / Math.max(1, total - 1)) * 570;
}

export function plotY(input: number, maximum: number): number {
  return 210 - (input / maximum) * 180;
}
